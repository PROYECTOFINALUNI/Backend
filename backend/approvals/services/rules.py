"""Turns approval rules into the ordered chain of approver pools for a report."""

from dataclasses import dataclass

from approvals.models import ApprovalRule
from users.models import User


@dataclass(frozen=True)
class PlannedStep:
    rule: ApprovalRule
    approvers: list[User]


def _attribute_matches(rule_value_id, user_value_id):
    return rule_value_id is None or rule_value_id == user_value_id


def matches_submitter(rule, submitter):
    return (
        _attribute_matches(rule.submitter_location_id, submitter.location_id)
        and _attribute_matches(rule.submitter_department_id, submitter.department_id)
        and _attribute_matches(rule.submitter_position_id, submitter.position_id)
    )


def matches_expense(rule, expense):
    # Threshold is strictly-below, matching how WarningRule already behaves.
    return (
        rule.currency == expense.currency
        and rule.threshold_minor < expense.amount_minor
        and (rule.category_id is None or rule.category_id == expense.category_id)
    )


def matches(rule, expense, submitter):
    return matches_expense(rule, expense) and matches_submitter(rule, submitter)


def resolve_pool(rule, *, exclude_user_id=None):
    """Users eligible to decide a step created by this rule. NULL criteria mean "any"."""
    queryset = User.objects.filter(
        is_active=True,
        role__in=[User.Role.APPROVER, User.Role.ADMIN],
    )
    if rule.approver_role:
        queryset = queryset.filter(role=rule.approver_role)
    for dimension in ("location", "department", "position"):
        value_id = getattr(rule, f"approver_{dimension}_id")
        if value_id:
            queryset = queryset.filter(**{f"{dimension}_id": value_id})
    if exclude_user_id:
        queryset = queryset.exclude(pk=exclude_user_id)
    return list(queryset.order_by("full_name", "email", "id"))


def matching_rules(report, *, expenses=None):
    """Active rules matched by at least one expense, in the order they should be applied."""
    submitter = report.user
    if expenses is None:
        expenses = list(report.expenses.all())
    rules = ApprovalRule.objects.filter(active=True).order_by("priority", "threshold_minor", "id")
    return [
        rule for rule in rules if any(matches(rule, expense, submitter) for expense in expenses)
    ]


def plan_chain(report, *, expenses=None):
    """The chain a submit would produce. Rules with no eligible approver are skipped."""
    planned = []
    for rule in matching_rules(report, expenses=expenses):
        approvers = resolve_pool(rule, exclude_user_id=report.user_id)
        if not approvers:
            continue
        planned.append(PlannedStep(rule=rule, approvers=approvers))
    return planned
