from django.db import IntegrityError, transaction
from django.utils import timezone

from approvals.models import ApprovalEvent, ApprovalStep, ApprovalStepCandidate
from approvals.services.rules import plan_chain
from common.api import DomainConflict
from expenses.models import Expense, ExpenseReport, WarningRule
from expenses.services.expenses import _replace_warnings
from expenses.services.money import calculate_tax
from users.models import User


def _admin(actor):
    return actor.role == User.Role.ADMIN


def _conflict(code, detail):
    raise DomainConflict(code, detail)


def _lock(report_id):
    return ExpenseReport.objects.select_for_update().select_related("user").get(pk=report_id)


def _finalise_expenses(report, expenses):
    """Recalculate tax and refresh warning snapshots; returns True if any is blocking."""
    blocking = False
    for expense in expenses:
        calculation = calculate_tax(
            expense.amount_minor,
            expense.tax_rate_bps,
            tax_included=expense.tax_included,
        )
        expense.tax_amount_minor = calculation.tax_minor
        expense.total_amount_minor = calculation.total_minor
        expense.save(update_fields=["tax_amount_minor", "total_amount_minor", "updated_at"])
        _replace_warnings(expense)
        blocking = (
            blocking or expense.warnings.filter(severity=WarningRule.Severity.BLOCKING).exists()
        )
    return blocking


def _validate_manual_chain(report, steps):
    if [step.step_order for step in steps] != list(range(1, len(steps) + 1)):
        _conflict("invalid_approval_chain", "Approval steps must be contiguous starting at one.")
    candidates = User.objects.filter(approval_candidacies__step__in=steps).distinct()
    if any(
        not candidate.is_active
        or candidate.role not in {User.Role.APPROVER, User.Role.ADMIN}
        or candidate.id == report.user_id
        for candidate in candidates
    ):
        _conflict("invalid_approver", "Every approver must be active, eligible, and not the owner.")
    if any(not step.candidates.exists() for step in steps):
        _conflict("invalid_approval_chain", "Every approval step needs at least one approver.")


def _generate_chain(report, expenses):
    """Materialise the rule-driven chain. Returns the number of steps created."""
    planned = plan_chain(report, expenses=expenses)
    for step_order, entry in enumerate(planned, start=1):
        step = ApprovalStep.objects.create(
            report=report,
            step_order=step_order,
            origin=ApprovalStep.Origin.RULE,
            rule=entry.rule,
        )
        ApprovalStepCandidate.objects.bulk_create(
            ApprovalStepCandidate(step=step, user=approver) for approver in entry.approvers
        )
    return len(planned)


@transaction.atomic
def submit(*, report_id, actor):
    report = _lock(report_id)
    if report.user_id != actor.id and not _admin(actor):
        _conflict("submit_forbidden", "Only the owner or an admin can submit this report.")
    if report.status != ExpenseReport.Status.DRAFT:
        _conflict("invalid_state_transition", "Only draft reports can be submitted.")
    expenses = list(report.expenses.select_for_update().all())
    if not expenses:
        _conflict("empty_report", "A report must contain at least one expense.")
    if any(e.user_id != report.user_id or e.status != e.Status.DRAFT for e in expenses):
        _conflict("invalid_expenses", "All expenses must belong to the owner and be draft.")
    if _finalise_expenses(report, expenses):
        _conflict(
            "blocking_warning", "Blocking expense warnings must be resolved before submission."
        )

    manual_steps = list(report.approval_steps.select_for_update().order_by("step_order"))
    if manual_steps:
        _validate_manual_chain(report, manual_steps)
        report.approval_steps.update(
            status=ApprovalStep.Status.PENDING,
            decided_at=None,
            decided_by=None,
            comment=None,
        )
        step_count = len(manual_steps)
    else:
        step_count = _generate_chain(report, expenses)

    now = timezone.now()
    report.submitted_at = now
    # Nothing to route to means nobody has to act: the report is approved on the spot.
    report.status = ExpenseReport.Status.SUBMITTED if step_count else ExpenseReport.Status.APPROVED
    report.save(update_fields=["status", "submitted_at", "updated_at"])
    report.expenses.update(
        status=Expense.Status.SUBMITTED if step_count else Expense.Status.APPROVED
    )
    ApprovalEvent.objects.create(report=report, actor=actor, action=ApprovalEvent.Action.SUBMITTED)
    if not step_count:
        ApprovalEvent.objects.create(
            report=report,
            actor=actor,
            action=ApprovalEvent.Action.AUTO_APPROVED,
            comment="No approval rule matched this report.",
        )
    return report


def _current_step(report):
    step = (
        report.approval_steps.select_for_update()
        .filter(status=ApprovalStep.Status.PENDING)
        .order_by("step_order")
        .first()
    )
    if step is None:
        _conflict("no_pending_step", "There is no pending approval step.")
    if (
        report.approval_steps.filter(step_order__lt=step.step_order)
        .exclude(status=ApprovalStep.Status.APPROVED)
        .exists()
    ):
        _conflict("invalid_approval_chain", "Previous approval steps must be approved.")
    return step


def _assert_can_decide(report, step, actor, verb):
    is_candidate = step.candidates.filter(user_id=actor.id).exists()
    if not is_candidate or not actor.is_active or actor.id == report.user_id:
        _conflict("not_current_approver", f"Only a current active approver may {verb}.")


@transaction.atomic
def approve(*, report_id, actor, comment=None):
    report = _lock(report_id)
    if report.status != ExpenseReport.Status.SUBMITTED:
        _conflict("invalid_state_transition", "Only submitted reports can be approved.")
    step = _current_step(report)
    _assert_can_decide(report, step, actor, "approve")
    step.status = ApprovalStep.Status.APPROVED
    step.decided_at = timezone.now()
    step.decided_by = actor
    step.comment = comment or None
    step.save(update_fields=["status", "decided_at", "decided_by", "comment", "updated_at"])
    ApprovalEvent.objects.create(
        report=report, actor=actor, action=ApprovalEvent.Action.APPROVED, comment=comment or None
    )
    if not report.approval_steps.filter(status=ApprovalStep.Status.PENDING).exists():
        report.status = ExpenseReport.Status.APPROVED
        report.save(update_fields=["status", "updated_at"])
        report.expenses.update(status=Expense.Status.APPROVED)
    return report


@transaction.atomic
def reject(*, report_id, actor, comment):
    report = _lock(report_id)
    if report.status != ExpenseReport.Status.SUBMITTED:
        _conflict("invalid_state_transition", "Only submitted reports can be rejected.")
    step = _current_step(report)
    _assert_can_decide(report, step, actor, "reject")
    step.status = ApprovalStep.Status.REJECTED
    step.decided_at = timezone.now()
    step.decided_by = actor
    step.comment = comment.strip()
    step.save(update_fields=["status", "decided_at", "decided_by", "comment", "updated_at"])
    report.approval_steps.filter(
        status=ApprovalStep.Status.PENDING, step_order__gt=step.step_order
    ).update(status=ApprovalStep.Status.SKIPPED)
    report.status = ExpenseReport.Status.REJECTED
    report.save(update_fields=["status", "updated_at"])
    report.expenses.update(status=Expense.Status.REJECTED)
    ApprovalEvent.objects.create(
        report=report, actor=actor, action=ApprovalEvent.Action.REJECTED, comment=comment.strip()
    )
    return report


@transaction.atomic
def return_to_draft(*, report_id, actor):
    report = _lock(report_id)
    if report.user_id != actor.id and not _admin(actor):
        _conflict("return_forbidden", "Only the owner or an admin can return this report.")
    if report.status != ExpenseReport.Status.REJECTED:
        _conflict("invalid_state_transition", "Only rejected reports can return to draft.")
    report.status = ExpenseReport.Status.DRAFT
    report.submitted_at = None
    report.save(update_fields=["status", "submitted_at", "updated_at"])
    report.expenses.update(status=Expense.Status.DRAFT)
    # Rule-driven steps are regenerated on the next submit, so the chain reflects the
    # rules and expenses as they stand then. Only a manual override survives.
    report.approval_steps.exclude(origin=ApprovalStep.Origin.MANUAL).delete()
    report.approval_steps.update(
        status=ApprovalStep.Status.PENDING,
        decided_at=None,
        decided_by=None,
        comment=None,
    )
    ApprovalEvent.objects.create(
        report=report, actor=actor, action=ApprovalEvent.Action.RETURNED_TO_DRAFT
    )
    return report


@transaction.atomic
def delegate(*, report_id, actor, approver, comment=None):
    """Hand the current pending step to a different approver, keeping the rest of the chain."""
    report = _lock(report_id)
    if report.status != ExpenseReport.Status.SUBMITTED:
        _conflict("invalid_state_transition", "Only submitted reports can be delegated.")
    step = _current_step(report)
    if not _admin(actor):
        _assert_can_decide(report, step, actor, "delegate")
    _assert_approver(report, approver)
    if approver.id == actor.id:
        _conflict("invalid_approver", "Choose a different approver to delegate to.")
    step.candidates.all().delete()
    ApprovalStepCandidate.objects.create(step=step, user=approver)
    step.origin = ApprovalStep.Origin.DELEGATED
    step.save(update_fields=["origin", "updated_at"])
    ApprovalEvent.objects.create(
        report=report,
        actor=actor,
        action=ApprovalEvent.Action.DELEGATED,
        comment=(comment or "").strip() or f"Delegated to {approver.full_name}.",
    )
    return report


@transaction.atomic
def add_comment(*, report_id, actor, comment):
    report = _lock(report_id)
    assigned = report.approval_steps.filter(candidates__user=actor).exists()
    if report.user_id != actor.id and not assigned and not _admin(actor):
        _conflict("comment_forbidden", "You cannot comment on this report.")
    ApprovalEvent.objects.create(
        report=report, actor=actor, action=ApprovalEvent.Action.COMMENTED, comment=comment.strip()
    )
    return report


@transaction.atomic
def mark_paid(*, report_id, actor):
    report = _lock(report_id)
    if not _admin(actor):
        _conflict("mark_paid_forbidden", "Only an admin can mark reports paid.")
    if report.status != ExpenseReport.Status.APPROVED:
        _conflict("invalid_state_transition", "Only approved reports can be marked paid.")
    report.status = ExpenseReport.Status.PAID
    report.save(update_fields=["status", "updated_at"])
    report.expenses.update(status=Expense.Status.PAID)
    ApprovalEvent.objects.create(report=report, actor=actor, action=ApprovalEvent.Action.PAID)
    return report


def _assert_steps_editable(report, actor):
    if not _admin(actor) or report.status != ExpenseReport.Status.DRAFT:
        _conflict("approval_steps_locked", "Only admins may edit draft approval steps.")


def _assert_approver(report, approver):
    if (
        not approver.is_active
        or approver.role not in {User.Role.APPROVER, User.Role.ADMIN}
        or approver.id == report.user_id
    ):
        _conflict(
            "invalid_approver",
            "The approver must be active, eligible, and not the report owner.",
        )


@transaction.atomic
def create_step(*, report_id, actor, approver, step_order):
    report = _lock(report_id)
    _assert_steps_editable(report, actor)
    _assert_approver(report, approver)
    try:
        step = ApprovalStep.objects.create(
            report=report,
            step_order=step_order,
            origin=ApprovalStep.Origin.MANUAL,
        )
    except IntegrityError as exc:
        raise DomainConflict("duplicate_step_order", "Step order must be unique.") from exc
    ApprovalStepCandidate.objects.create(step=step, user=approver)
    return step


@transaction.atomic
def update_step(*, report_id, step_id, actor, data):
    report = _lock(report_id)
    _assert_steps_editable(report, actor)
    step = report.approval_steps.select_for_update().filter(pk=step_id).first()
    if step is None:
        return None
    approver = data.get("approver")
    if approver is not None:
        _assert_approver(report, approver)
        step.candidates.all().delete()
        ApprovalStepCandidate.objects.create(step=step, user=approver)
    if "step_order" in data:
        step.step_order = data["step_order"]
        try:
            step.save(update_fields=["step_order", "updated_at"])
        except IntegrityError as exc:
            raise DomainConflict("duplicate_step_order", "Step order must be unique.") from exc
    return step


@transaction.atomic
def delete_step(*, report_id, step_id, actor):
    report = _lock(report_id)
    _assert_steps_editable(report, actor)
    step = report.approval_steps.select_for_update().filter(pk=step_id).first()
    if step is None:
        return False
    step.delete()
    return True
