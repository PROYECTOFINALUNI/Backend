from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from approvals.models import ApprovalStep
from conftest import (
    ApprovalStepFactory,
    CategoryFactory,
    CategoryFieldFactory,
    ExpenseFactory,
    ExpenseFieldValueFactory,
    ReportFactory,
    UserFactory,
    WarningRuleFactory,
)
from expenses.models import (
    CategoryField,
    Expense,
    ExpenseFieldValue,
    ExpenseReport,
    WarningRule,
)
from users.models import User

pytestmark = pytest.mark.django_db


def raw_expense_values(report):
    return {
        "user": report.user,
        "report": report,
        "merchant": "Merchant",
        "expense_date": date.today(),
        "currency": "EUR",
        "amount_minor": 100,
        "tax_rate_bps": 0,
        "tax_amount_minor": 0,
        "total_amount_minor": 100,
    }


def assert_integrity_error(**overrides):
    report = ReportFactory()
    values = raw_expense_values(report)
    values.update(overrides)
    with pytest.raises(IntegrityError), transaction.atomic():
        Expense.objects.create(**values)


def test_expense_report_is_not_nullable():
    assert_integrity_error(report=None)


@pytest.mark.parametrize("tax_rate_bps", [-1, 10_001])
def test_tax_basis_points_have_database_bounds(tax_rate_bps):
    assert_integrity_error(tax_rate_bps=tax_rate_bps)


@pytest.mark.parametrize(
    "field",
    ["amount_minor", "tax_amount_minor", "total_amount_minor"],
)
def test_money_fields_reject_negative_values_at_database(field):
    assert_integrity_error(**{field: -1})


def test_report_step_order_is_unique():
    report = ReportFactory()
    ApprovalStepFactory(report=report, step_order=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        ApprovalStep.objects.create(report=report, step_order=1)


def test_email_is_case_insensitively_unique():
    UserFactory(email="case@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create(
            email="CASE@EXAMPLE.COM",
            full_name="Duplicate",
            password="unusable",
        )


def test_warning_threshold_cannot_be_negative():
    with pytest.raises(IntegrityError), transaction.atomic():
        WarningRule.objects.create(
            name="Invalid",
            currency="EUR",
            threshold_minor=-1,
            severity=WarningRule.Severity.WARNING,
            message="No",
        )


def test_referenced_category_is_protected():
    category = CategoryFactory()
    ExpenseFactory(category=category)
    with pytest.raises(ProtectedError):
        category.delete()


def test_expense_clean_rejects_user_different_from_report_owner():
    report = ReportFactory()
    expense = ExpenseFactory.build(report=report, user=UserFactory())
    with pytest.raises(ValidationError, match="must match the report owner"):
        expense.full_clean()


def test_expense_clean_rejects_new_inactive_category():
    category = CategoryFactory(active=False)
    expense = ExpenseFactory.build(category=category)
    with pytest.raises(ValidationError, match="must be active"):
        expense.full_clean()


def test_existing_expense_can_keep_category_that_was_deactivated():
    expense = ExpenseFactory()
    expense.category.active = False
    expense.category.save(update_fields=["active"])
    expense.merchant = "Updated merchant"
    expense.full_clean()


def test_warning_rule_rejects_new_inactive_category():
    rule = WarningRuleFactory.build(category=CategoryFactory(active=False))
    with pytest.raises(ValidationError, match="active category"):
        rule.full_clean()


def test_report_defaults_to_draft():
    report = ExpenseReport(user=UserFactory(), title="Draft")
    assert report.status == ExpenseReport.Status.DRAFT


def test_category_field_name_is_case_insensitively_unique_per_category():
    field = CategoryFieldFactory(name="Project code")
    with pytest.raises(IntegrityError), transaction.atomic():
        CategoryField.objects.create(
            category=field.category,
            name="PROJECT CODE",
            field_type=CategoryField.FieldType.TEXT,
        )


def test_same_field_name_is_allowed_on_a_different_category():
    CategoryFieldFactory(name="Project code")
    other = CategoryFieldFactory(category=CategoryFactory(), name="Project code")
    assert other.pk is not None


def test_category_field_with_values_cannot_be_deleted():
    value = ExpenseFieldValueFactory()
    with pytest.raises(ProtectedError):
        value.field.delete()


@pytest.mark.parametrize(
    ("field_type", "values"),
    [
        (CategoryField.FieldType.TEXT, {"value_number": 5}),
        (CategoryField.FieldType.NUMBER, {"value_text": "five"}),
        (CategoryField.FieldType.BOOLEAN, {"value_text": "yes"}),
    ],
)
def test_field_value_must_use_the_column_matching_its_type(field_type, values):
    field = CategoryFieldFactory(field_type=field_type)
    value = ExpenseFieldValue(
        expense=ExpenseFactory(category=field.category), field=field, **values
    )
    with pytest.raises(ValidationError):
        value.full_clean()


def test_field_value_exposes_the_column_for_its_type():
    field = CategoryFieldFactory(field_type=CategoryField.FieldType.BOOLEAN)
    value = ExpenseFieldValueFactory(
        field=field,
        value_text="",
        value_boolean=False,
    )
    assert value.value is False


def test_one_value_per_field_per_expense():
    value = ExpenseFieldValueFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        ExpenseFieldValue.objects.create(
            expense=value.expense,
            field=value.field,
            value_text="Second answer",
        )
