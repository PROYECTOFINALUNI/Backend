import pytest

from approvals import services as approval_services
from common.api import DomainConflict
from conftest import (
    ApprovalStepFactory,
    ApproverFactory,
    CategoryFactory,
    ExpenseFactory,
    ReportFactory,
    UserFactory,
    WarningRuleFactory,
)
from expenses.models import ExpenseWarning, WarningRule
from expenses.services import expenses as expense_services

pytestmark = pytest.mark.django_db


def test_warning_threshold_is_strictly_greater():
    rule = WarningRuleFactory(threshold_minor=10_000)
    assert (
        expense_services.evaluate_warnings(currency="EUR", category_id=None, amount_minor=10_000)
        == []
    )
    assert expense_services.evaluate_warnings(
        currency="EUR", category_id=None, amount_minor=10_001
    ) == [rule]


def test_warning_filters_category_currency_and_inactive_rules():
    category = CategoryFactory()
    other = CategoryFactory()
    matching = WarningRuleFactory(category=category, currency="EUR")
    WarningRuleFactory(category=other, currency="EUR")
    WarningRuleFactory(category=category, currency="USD")
    WarningRuleFactory(category=category, currency="EUR", active=False)
    assert expense_services.evaluate_warnings(
        currency="EUR", category_id=category.id, amount_minor=10_000
    ) == [matching]


def test_global_category_rule_matches_null_and_specific_categories():
    global_rule = WarningRuleFactory(category=None)
    category = CategoryFactory()
    for category_id in (None, category.id):
        assert global_rule in expense_services.evaluate_warnings(
            currency="EUR", category_id=category_id, amount_minor=10_000
        )


def test_create_and_edit_replace_warning_snapshots_without_duplicates():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    rule = WarningRuleFactory(threshold_minor=100)
    data = {
        "merchant": "Cafe",
        "expense_date": "2026-07-23",
        "category": None,
        "currency": "EUR",
        "amount_decimal": "2.00",
        "tax": {"rate_bps": 0, "included": False},
    }
    expense = expense_services.create_expense(actor=owner, report=report, data=data)
    assert list(expense.warnings.values_list("warning_rule_id", flat=True)) == [rule.id]
    expense = expense_services.update_expense(
        actor=owner,
        expense=expense,
        data={"merchant": "Updated"},
    )
    assert expense.warnings.count() == 1
    expense = expense_services.update_expense(
        actor=owner,
        expense=expense,
        data={"amount_decimal": "1.00"},
    )
    assert expense.warnings.count() == 0


def submitted_report_with_warning(severity=WarningRule.Severity.WARNING):
    owner = UserFactory()
    report = ReportFactory(user=owner)
    expense = ExpenseFactory(report=report, category=None)
    rule = WarningRuleFactory(
        threshold_minor=1,
        severity=severity,
        message="Original snapshot",
    )
    ApprovalStepFactory(report=report, approver=ApproverFactory(), step_order=1)
    return owner, report, expense, rule


def test_submitted_warning_snapshot_survives_rule_changes():
    owner, report, expense, rule = submitted_report_with_warning()
    approval_services.submit(report_id=report.id, actor=owner)
    warning = expense.warnings.get()
    rule.message = "Changed later"
    rule.active = False
    rule.save(update_fields=["message", "active"])
    warning.refresh_from_db()
    assert warning.message == "Original snapshot"
    assert warning.severity == WarningRule.Severity.WARNING


def test_deleting_rule_preserves_submitted_snapshot():
    owner, report, expense, rule = submitted_report_with_warning()
    approval_services.submit(report_id=report.id, actor=owner)
    warning_id = expense.warnings.get().id
    rule.delete()
    warning = ExpenseWarning.objects.get(pk=warning_id)
    assert warning.warning_rule is None
    assert warning.message == "Original snapshot"


def test_blocking_warning_blocks_submission_atomically():
    owner, report, expense, _ = submitted_report_with_warning(WarningRule.Severity.BLOCKING)
    with pytest.raises(DomainConflict) as exc:
        approval_services.submit(report_id=report.id, actor=owner)
    assert exc.value.domain_code == "blocking_warning"
    report.refresh_from_db()
    expense.refresh_from_db()
    assert report.status == report.Status.DRAFT
    assert expense.status == expense.Status.DRAFT
    assert expense.warnings.count() == 0


def test_warning_severity_allows_submission():
    owner, report, expense, _ = submitted_report_with_warning()
    approval_services.submit(report_id=report.id, actor=owner)
    report.refresh_from_db()
    expense.refresh_from_db()
    assert report.status == report.Status.SUBMITTED
    assert expense.status == expense.Status.SUBMITTED
    assert expense.warnings.count() == 1


def test_preview_returns_rules_without_persisting_warning_snapshots():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    rule = WarningRuleFactory(threshold_minor=1)
    data = {
        "merchant": "Cafe",
        "expense_date": "2026-07-23",
        "category": None,
        "currency": "EUR",
        "amount_decimal": "100.00",
        "tax": {"rate_bps": 0, "included": False},
    }
    _, _, _, rules = expense_services.preview_expense(actor=owner, report=report, data=data)
    assert rules == [rule]
    assert ExpenseWarning.objects.count() == 0
