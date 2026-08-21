import threading

import pytest
from django.db import close_old_connections
from django.test import TransactionTestCase

from approvals import services
from approvals.models import ApprovalEvent, ApprovalStep, ApprovalStepCandidate
from common.api import DomainConflict
from conftest import (
    AdminFactory,
    ApprovalStepFactory,
    ApproverFactory,
    ExpenseFactory,
    ReportFactory,
    UserFactory,
)
from expenses.models import Expense, ExpenseReport
from users.models import User

pytestmark = pytest.mark.django_db


def assert_conflict(code, function, **kwargs):
    with pytest.raises(DomainConflict) as exc:
        function(**kwargs)
    assert exc.value.domain_code == code


def valid_draft(step_orders=(1,)):
    owner = UserFactory()
    report = ReportFactory(user=owner)
    expense = ExpenseFactory(report=report)
    steps = [
        ApprovalStepFactory(report=report, approver=ApproverFactory(), step_order=order)
        for order in step_orders
    ]
    return owner, report, expense, steps


def test_empty_report_cannot_be_submitted():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    assert_conflict("empty_report", services.submit, report_id=report.id, actor=owner)


def test_report_matching_no_rule_is_auto_approved_on_submit():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    expense = ExpenseFactory(report=report)
    services.submit(report_id=report.id, actor=owner)
    report.refresh_from_db()
    expense.refresh_from_db()
    assert report.status == ExpenseReport.Status.APPROVED
    assert report.submitted_at is not None
    assert expense.status == Expense.Status.APPROVED
    assert not report.approval_steps.exists()
    assert list(report.approval_events.values_list("action", flat=True)) == [
        ApprovalEvent.Action.SUBMITTED,
        ApprovalEvent.Action.AUTO_APPROVED,
    ]


def test_noncontiguous_approval_steps_cannot_be_submitted():
    owner, report, _, _ = valid_draft(step_orders=(2,))
    assert_conflict("invalid_approval_chain", services.submit, report_id=report.id, actor=owner)


@pytest.mark.parametrize("problem", ["owner", "inactive", "employee"])
def test_invalid_approver_cannot_be_used_on_submit(problem):
    owner = UserFactory()
    report = ReportFactory(user=owner)
    ExpenseFactory(report=report)
    if problem == "owner":
        approver = owner
    elif problem == "inactive":
        approver = ApproverFactory(is_active=False)
    else:
        approver = UserFactory()
    step = ApprovalStep.objects.create(report=report, step_order=1)
    ApprovalStepCandidate.objects.create(step=step, user=approver)
    assert_conflict("invalid_approver", services.submit, report_id=report.id, actor=owner)


def test_draft_submit_synchronizes_report_expenses_steps_and_event():
    owner, report, expense, steps = valid_draft()
    services.submit(report_id=report.id, actor=owner)
    report.refresh_from_db()
    expense.refresh_from_db()
    steps[0].refresh_from_db()
    assert report.status == ExpenseReport.Status.SUBMITTED
    assert report.submitted_at is not None
    assert expense.status == Expense.Status.SUBMITTED
    assert steps[0].status == ApprovalStep.Status.PENDING
    event = report.approval_events.get()
    assert (event.action, event.actor) == (ApprovalEvent.Action.SUBMITTED, owner)


def test_submitted_report_cannot_be_resubmitted():
    owner, report, _, _ = valid_draft()
    services.submit(report_id=report.id, actor=owner)
    assert_conflict(
        "invalid_state_transition",
        services.submit,
        report_id=report.id,
        actor=owner,
    )


def test_only_current_approver_can_act_and_future_step_cannot_approve():
    owner, report, _, steps = valid_draft(step_orders=(1, 2))
    services.submit(report_id=report.id, actor=owner)
    assert_conflict(
        "not_current_approver",
        services.approve,
        report_id=report.id,
        actor=steps[1].approvers[0],
    )
    assert_conflict(
        "not_current_approver",
        services.approve,
        report_id=report.id,
        actor=owner,
    )


def test_intermediate_approval_stays_submitted_and_final_approval_synchronizes():
    owner, report, expense, steps = valid_draft(step_orders=(1, 2))
    services.submit(report_id=report.id, actor=owner)
    services.approve(report_id=report.id, actor=steps[0].approvers[0], comment="first")
    report.refresh_from_db()
    expense.refresh_from_db()
    assert report.status == ExpenseReport.Status.SUBMITTED
    assert expense.status == Expense.Status.SUBMITTED
    services.approve(report_id=report.id, actor=steps[1].approvers[0])
    report.refresh_from_db()
    expense.refresh_from_db()
    assert report.status == ExpenseReport.Status.APPROVED
    assert expense.status == Expense.Status.APPROVED
    assert list(report.approval_events.values_list("action", flat=True)) == [
        ApprovalEvent.Action.SUBMITTED,
        ApprovalEvent.Action.APPROVED,
        ApprovalEvent.Action.APPROVED,
    ]


def test_reject_marks_later_steps_skipped_and_records_comment():
    owner, report, expense, steps = valid_draft(step_orders=(1, 2, 3))
    services.submit(report_id=report.id, actor=owner)
    services.reject(
        report_id=report.id,
        actor=steps[0].approvers[0],
        comment="  missing receipt  ",
    )
    report.refresh_from_db()
    expense.refresh_from_db()
    for step in steps:
        step.refresh_from_db()
    assert report.status == ExpenseReport.Status.REJECTED
    assert expense.status == Expense.Status.REJECTED
    assert steps[0].status == ApprovalStep.Status.REJECTED
    assert [step.status for step in steps[1:]] == [
        ApprovalStep.Status.SKIPPED,
        ApprovalStep.Status.SKIPPED,
    ]
    assert report.approval_events.last().comment == "missing receipt"


def test_return_to_draft_resets_state_and_preserves_prior_events():
    owner, report, expense, steps = valid_draft(step_orders=(1, 2))
    services.submit(report_id=report.id, actor=owner)
    services.reject(report_id=report.id, actor=steps[0].approvers[0], comment="Fix it")
    prior_event_ids = list(report.approval_events.values_list("id", flat=True))
    services.return_to_draft(report_id=report.id, actor=owner)
    report.refresh_from_db()
    expense.refresh_from_db()
    assert report.status == ExpenseReport.Status.DRAFT
    assert report.submitted_at is None
    assert expense.status == Expense.Status.DRAFT
    assert set(report.approval_events.values_list("id", flat=True)).issuperset(prior_event_ids)
    assert report.approval_events.last().action == ApprovalEvent.Action.RETURNED_TO_DRAFT
    assert not report.approval_steps.exclude(
        status=ApprovalStep.Status.PENDING,
        decided_at=None,
        comment=None,
    ).exists()


def test_only_admin_can_mark_approved_report_paid_and_statuses_sync():
    owner, report, expense, steps = valid_draft()
    services.submit(report_id=report.id, actor=owner)
    services.approve(report_id=report.id, actor=steps[0].approvers[0])
    assert_conflict(
        "mark_paid_forbidden",
        services.mark_paid,
        report_id=report.id,
        actor=owner,
    )
    admin = AdminFactory()
    services.mark_paid(report_id=report.id, actor=admin)
    report.refresh_from_db()
    expense.refresh_from_db()
    assert report.status == ExpenseReport.Status.PAID
    assert expense.status == Expense.Status.PAID
    assert report.approval_events.last().action == ApprovalEvent.Action.PAID


def test_each_transition_and_comment_creates_an_event():
    owner, report, _, steps = valid_draft()
    services.add_comment(report_id=report.id, actor=owner, comment="Before submit")
    services.submit(report_id=report.id, actor=owner)
    services.approve(report_id=report.id, actor=steps[0].approvers[0], comment="Looks good")
    services.mark_paid(report_id=report.id, actor=AdminFactory())
    assert list(report.approval_events.values_list("action", flat=True)) == [
        ApprovalEvent.Action.COMMENTED,
        ApprovalEvent.Action.SUBMITTED,
        ApprovalEvent.Action.APPROVED,
        ApprovalEvent.Action.PAID,
    ]


class DoubleApprovalConcurrencyTests(TransactionTestCase):
    reset_sequences = False

    def test_double_approval_serializes_to_one_success_and_one_conflict(self):
        owner = User.objects.create_user(
            email="owner-concurrency@example.com",
            password="Correct-Horse-42!",
            full_name="Owner",
        )
        approver = User.objects.create_user(
            email="approver-concurrency@example.com",
            password="Correct-Horse-42!",
            full_name="Approver",
            role=User.Role.APPROVER,
        )
        report = ExpenseReport.objects.create(user=owner, title="Concurrent")
        ExpenseFactory(report=report)
        step = ApprovalStep.objects.create(report=report, step_order=1)
        ApprovalStepCandidate.objects.create(step=step, user=approver)
        services.submit(report_id=report.id, actor=owner)

        barrier = threading.Barrier(2)
        outcomes = []

        def approve_once():
            close_old_connections()
            actor = User.objects.get(pk=approver.pk)
            barrier.wait(timeout=5)
            try:
                services.approve(report_id=report.id, actor=actor)
            except DomainConflict as exc:
                outcomes.append(exc.domain_code)
            else:
                outcomes.append("success")
            finally:
                close_old_connections()

        threads = [threading.Thread(target=approve_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["success", "invalid_state_transition"])
        self.assertEqual(
            ApprovalEvent.objects.filter(
                report=report,
                action=ApprovalEvent.Action.APPROVED,
            ).count(),
            1,
        )
