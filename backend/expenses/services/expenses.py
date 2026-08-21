from django.db import transaction
from django.db.models import Q

from common.api import DomainConflict
from expenses.models import Expense, ExpenseReport, ExpenseWarning, WarningRule
from expenses.services.custom_fields import replace_field_values
from expenses.services.money import calculate_tax, parse_decimal_string, validate_currency


def evaluate_warnings(*, currency, category_id, amount_minor):
    return list(
        WarningRule.objects.filter(active=True, currency=currency, threshold_minor__lt=amount_minor)
        .filter(Q(category_id=category_id) | Q(category__isnull=True))
        .order_by("id")
    )


def calculate_payload(*, currency, amount_decimal, tax):
    currency = validate_currency(currency)
    amount_minor = parse_decimal_string(amount_decimal, currency)
    calculation = calculate_tax(
        amount_minor,
        tax["rate_bps"],
        tax_included=tax["included"],
    )
    return currency, amount_minor, calculation


def _assert_mutable(report, actor):
    if report.user_id != actor.id:
        raise DomainConflict("report_not_owned", "Only the report owner can edit expenses.")
    if report.status != ExpenseReport.Status.DRAFT:
        raise DomainConflict(
            "invalid_state_transition",
            "Expenses can only be edited in a draft report.",
        )


def _replace_warnings(expense):
    rules = evaluate_warnings(
        currency=expense.currency,
        category_id=expense.category_id,
        amount_minor=expense.amount_minor,
    )
    expense.warnings.all().delete()
    ExpenseWarning.objects.bulk_create(
        [
            ExpenseWarning(
                expense=expense,
                warning_rule=rule,
                severity=rule.severity,
                message=rule.message,
            )
            for rule in rules
        ]
    )


@transaction.atomic
def create_expense(*, actor, report, data):
    report = ExpenseReport.objects.select_for_update().get(pk=report.pk)
    _assert_mutable(report, actor)
    currency, amount_minor, calculation = calculate_payload(
        currency=data["currency"],
        amount_decimal=data["amount_decimal"],
        tax=data["tax"],
    )
    expense = Expense(
        user=report.user,
        report=report,
        merchant=data["merchant"].strip(),
        expense_date=data["expense_date"],
        category=data.get("category"),
        currency=currency,
        amount_minor=amount_minor,
        tax_rate_bps=data["tax"]["rate_bps"],
        tax_included=data["tax"]["included"],
        tax_amount_minor=calculation.tax_minor,
        total_amount_minor=calculation.total_minor,
    )
    expense.full_clean()
    expense.save()
    replace_field_values(expense, data.get("custom_fields") or [])
    _replace_warnings(expense)
    return expense


@transaction.atomic
def update_expense(*, actor, expense, data):
    report = ExpenseReport.objects.select_for_update().get(pk=expense.report_id)
    _assert_mutable(report, actor)
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    for field in ("merchant", "expense_date", "category"):
        if field in data:
            setattr(expense, field, data[field])
    if "currency" in data and "amount_decimal" not in data:
        raise ValueError("amountDecimal is required when currency changes.")
    currency = data.get("currency", expense.currency)
    amount_decimal = data.get("amount_decimal")
    if amount_decimal is None:
        from expenses.services.money import minor_to_decimal_string

        amount_decimal = minor_to_decimal_string(expense.amount_minor, expense.currency)
    tax = data.get(
        "tax",
        {"rate_bps": expense.tax_rate_bps, "included": expense.tax_included},
    )
    currency, amount_minor, calculation = calculate_payload(
        currency=currency, amount_decimal=amount_decimal, tax=tax
    )
    expense.currency = currency
    expense.amount_minor = amount_minor
    expense.tax_rate_bps = tax["rate_bps"]
    expense.tax_included = tax["included"]
    expense.tax_amount_minor = calculation.tax_minor
    expense.total_amount_minor = calculation.total_minor
    expense.full_clean()
    expense.save()
# Los campos personalizados se conservan salvo que se cambie de categoría.
    replace_field_values(expense, data.get("custom_fields"))
    _replace_warnings(expense)
    return expense


@transaction.atomic
def delete_expense(*, actor, expense):
    report = ExpenseReport.objects.select_for_update().get(pk=expense.report_id)
    _assert_mutable(report, actor)
    expense.delete()


def preview_expense(*, actor, report, data):
    _assert_mutable(report, actor)
    currency, amount_minor, calculation = calculate_payload(
        currency=data["currency"],
        amount_decimal=data["amount_decimal"],
        tax=data["tax"],
    )
    rules = evaluate_warnings(
        currency=currency,
        category_id=getattr(data.get("category"), "id", None),
        amount_minor=amount_minor,
    )
    return currency, amount_minor, calculation, rules
