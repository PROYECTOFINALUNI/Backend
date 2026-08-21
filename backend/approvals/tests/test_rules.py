import pytest

from approvals import services
from approvals.models import ApprovalEvent, ApprovalStep
from approvals.services.rules import plan_chain, resolve_pool
from conftest import (
    AdminFactory,
    ApprovalRuleFactory,
    ApproverFactory,
    CategoryFactory,
    DepartmentFactory,
    ExpenseFactory,
    LocationFactory,
    PositionFactory,
    ReportFactory,
    UserFactory,
)
from expenses.models import Expense, ExpenseReport
from users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def org():
    return {
        "spain": LocationFactory(code="ES", name="Spain"),
        "france": LocationFactory(code="FR", name="France"),
        "it": DepartmentFactory(code="IT", name="IT"),
        "sales": DepartmentFactory(code="SALES", name="Sales"),
        "manager": PositionFactory(code="MANAGER", name="Manager"),
        "analyst": PositionFactory(code="ANALYST", name="Analyst"),
    }


def draft_with_expense(submitter, **expense_kwargs):
    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report, **expense_kwargs)
    return report


def test_rule_matches_submitter_attributes_and_routes_to_matching_pool(org):
    submitter = UserFactory(location=org["spain"], department=org["it"], position=org["analyst"])
    manager = ApproverFactory(location=org["spain"], department=org["it"], position=org["manager"])
    ApproverFactory(location=org["france"], department=org["it"], position=org["manager"])
    ApproverFactory(location=org["spain"], department=org["sales"], position=org["manager"])
    rule = ApprovalRuleFactory(
        submitter_location=org["spain"],
        submitter_department=org["it"],
        approver_location=org["spain"],
        approver_department=org["it"],
        approver_position=org["manager"],
    )

    report = draft_with_expense(submitter)
    planned = plan_chain(report)

    assert [entry.rule for entry in planned] == [rule]
    assert [user.id for user in planned[0].approvers] == [manager.id]


def test_null_criteria_act_as_wildcards(org):
    submitter = UserFactory(location=org["france"], department=org["sales"])
    approver = ApproverFactory(position=org["manager"])
    ApprovalRuleFactory(approver_position=org["manager"])

    report = draft_with_expense(submitter)
    planned = plan_chain(report)

    assert [user.id for user in planned[0].approvers] == [approver.id]


def test_submitter_attribute_mismatch_skips_the_rule(org):
    submitter = UserFactory(department=org["sales"])
    ApproverFactory()
    ApprovalRuleFactory(submitter_department=org["it"])

    assert plan_chain(draft_with_expense(submitter)) == []


def test_currency_category_and_threshold_are_matched_per_expense(org):
    submitter = UserFactory()
    ApproverFactory()
    travel = CategoryFactory()
    meals = CategoryFactory()
    ApprovalRuleFactory(currency="EUR", category=travel, threshold_minor=5_000)

    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report, category=meals, currency="EUR", amount_decimal="900.00")
    assert plan_chain(report) == []

    ExpenseFactory(report=report, category=travel, currency="EUR", amount_decimal="20.00")
    assert plan_chain(report) == []

    ExpenseFactory(report=report, category=travel, currency="EUR", amount_decimal="900.00")
    assert len(plan_chain(report)) == 1


def test_threshold_is_strictly_below_the_expense_amount():
    submitter = UserFactory()
    ApproverFactory()
    ApprovalRuleFactory(threshold_minor=10_000)

    report = draft_with_expense(submitter, amount_decimal="100.00")
    assert plan_chain(report) == []

    ExpenseFactory(report=report, amount_decimal="100.01")
    assert len(plan_chain(report)) == 1


def test_matching_rules_stack_into_a_chain_ordered_by_priority():
    submitter = UserFactory()
    first = ApproverFactory()
    second = AdminFactory()
    finance = ApprovalRuleFactory(priority=200, approver_role=User.Role.ADMIN)
    manager = ApprovalRuleFactory(priority=10, approver_role=User.Role.APPROVER)

    planned = plan_chain(draft_with_expense(submitter))

    assert [entry.rule for entry in planned] == [manager, finance]
    assert [user.id for user in planned[0].approvers] == [first.id]
    assert [user.id for user in planned[1].approvers] == [second.id]


def test_rule_with_empty_pool_is_skipped(org):
    submitter = UserFactory()
    ApproverFactory(location=org["spain"])
    ApprovalRuleFactory(name="Unreachable", approver_location=org["france"])
    reachable = ApprovalRuleFactory(name="Reachable", approver_location=org["spain"])

    planned = plan_chain(draft_with_expense(submitter))

    assert [entry.rule for entry in planned] == [reachable]


def test_submitter_is_never_their_own_approver():
    submitter = ApproverFactory()
    ApprovalRuleFactory(approver_role=User.Role.APPROVER)

    assert plan_chain(draft_with_expense(submitter)) == []


def test_inactive_rules_and_inactive_users_are_ignored():
    submitter = UserFactory()
    ApproverFactory(is_active=False)
    ApprovalRuleFactory(active=False)
    active_rule = ApprovalRuleFactory()

    assert resolve_pool(active_rule) == []
    assert plan_chain(draft_with_expense(submitter)) == []


def test_employees_are_never_in_an_approver_pool():
    UserFactory(role=User.Role.EMPLOYEE)
    assert resolve_pool(ApprovalRuleFactory()) == []


def test_submit_materialises_the_rule_chain_with_pools(org):
    submitter = UserFactory(department=org["it"])
    first = ApproverFactory(department=org["it"], full_name="Ana")
    second = ApproverFactory(department=org["it"], full_name="Bob")
    rule = ApprovalRuleFactory(submitter_department=org["it"], approver_department=org["it"])

    report = draft_with_expense(submitter)
    services.submit(report_id=report.id, actor=submitter)

    report.refresh_from_db()
    assert report.status == ExpenseReport.Status.SUBMITTED
    step = report.approval_steps.get()
    assert (step.step_order, step.origin, step.rule_id) == (1, ApprovalStep.Origin.RULE, rule.id)
    assert {user.id for user in step.approvers} == {first.id, second.id}


def test_any_pool_member_can_approve_and_the_decider_is_recorded():
    submitter = UserFactory()
    ApproverFactory(full_name="Ana")
    second = ApproverFactory(full_name="Bob")
    ApprovalRuleFactory()

    report = draft_with_expense(submitter)
    services.submit(report_id=report.id, actor=submitter)
    services.approve(report_id=report.id, actor=second, comment="Fine by me")

    report.refresh_from_db()
    step = report.approval_steps.get()
    assert report.status == ExpenseReport.Status.APPROVED
    assert (step.status, step.decided_by_id) == (ApprovalStep.Status.APPROVED, second.id)


def test_user_outside_the_pool_cannot_approve():
    submitter = UserFactory()
    ApproverFactory()
    outsider = ApproverFactory()
    ApprovalRuleFactory(approver_role=User.Role.APPROVER)

    report = draft_with_expense(submitter)
    services.submit(report_id=report.id, actor=submitter)
    report.approval_steps.get().candidates.filter(user=outsider).delete()

    with pytest.raises(Exception) as exc:
        services.approve(report_id=report.id, actor=outsider)
    assert exc.value.domain_code == "not_current_approver"


def test_manual_chain_overrides_the_rules(org):
    from conftest import ApprovalStepFactory

    submitter = UserFactory()
    rule_approver = ApproverFactory(location=org["spain"])
    chosen = ApproverFactory(location=org["france"])
    ApprovalRuleFactory(approver_location=org["spain"])

    report = draft_with_expense(submitter)
    ApprovalStepFactory(report=report, approver=chosen, step_order=1)
    services.submit(report_id=report.id, actor=submitter)

    step = report.approval_steps.get()
    assert step.origin == ApprovalStep.Origin.MANUAL
    assert [user.id for user in step.approvers] == [chosen.id]
    assert rule_approver.id not in {user.id for user in step.approvers}


def test_delegation_replaces_the_current_pool_and_records_an_event():
    submitter = UserFactory()
    original = ApproverFactory(full_name="Ana")
    target = ApproverFactory(full_name="Bob")
    ApprovalRuleFactory(approver_role=User.Role.APPROVER)

    report = draft_with_expense(submitter)
    services.submit(report_id=report.id, actor=submitter)
    step = report.approval_steps.get()
    step.candidates.exclude(user=original).delete()

    services.delegate(report_id=report.id, actor=original, approver=target, comment="On leave")

    step.refresh_from_db()
    assert [user.id for user in step.approvers] == [target.id]
    assert step.origin == ApprovalStep.Origin.DELEGATED
    assert report.approval_events.last().action == ApprovalEvent.Action.DELEGATED
    services.approve(report_id=report.id, actor=target)
    report.refresh_from_db()
    assert report.status == ExpenseReport.Status.APPROVED


def test_delegation_rejects_the_owner_and_non_approvers():
    submitter = UserFactory()
    approver = ApproverFactory()
    ApprovalRuleFactory(approver_role=User.Role.APPROVER)

    report = draft_with_expense(submitter)
    services.submit(report_id=report.id, actor=submitter)

    for target in (submitter, UserFactory(), ApproverFactory(is_active=False)):
        with pytest.raises(Exception) as exc:
            services.delegate(report_id=report.id, actor=approver, approver=target)
        assert exc.value.domain_code == "invalid_approver"


def test_return_to_draft_regenerates_the_chain_from_current_rules():
    submitter = UserFactory()
    first = ApproverFactory()
    ApprovalRuleFactory(approver_role=User.Role.APPROVER)

    report = draft_with_expense(submitter)
    services.submit(report_id=report.id, actor=submitter)
    services.reject(report_id=report.id, actor=first, comment="Redo")
    services.return_to_draft(report_id=report.id, actor=submitter)

    assert not report.approval_steps.exists()

    second = AdminFactory()
    ApprovalRuleFactory(priority=200, approver_role=User.Role.ADMIN)
    services.submit(report_id=report.id, actor=submitter)

    steps = list(report.approval_steps.order_by("step_order"))
    assert [step.step_order for step in steps] == [1, 2]
    assert [user.id for user in steps[1].approvers] == [second.id]


def test_auto_approved_report_can_still_be_marked_paid():
    submitter = UserFactory()
    report = draft_with_expense(submitter)
    services.submit(report_id=report.id, actor=submitter)
    services.mark_paid(report_id=report.id, actor=AdminFactory())

    report.refresh_from_db()
    assert report.status == ExpenseReport.Status.PAID
    assert report.expenses.first().status == Expense.Status.PAID
