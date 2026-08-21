from datetime import date

import factory
import pytest
from rest_framework.test import APIClient

from approvals.models import (
    ApprovalEvent,
    ApprovalRule,
    ApprovalStep,
    ApprovalStepCandidate,
)
from expenses.models import (
    CategoryField,
    Expense,
    ExpenseCategory,
    ExpenseFieldValue,
    ExpenseReport,
    ExpenseWarning,
    WarningRule,
)
from expenses.services.money import calculate_tax, parse_decimal_string
from users.models import OrgAttribute, User

TEST_PASSWORD = "Correct-Horse-42!"


class OrgAttributeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = OrgAttribute

    dimension = OrgAttribute.Dimension.LOCATION
    code = factory.Sequence(lambda n: f"ORG{n}")
    name = factory.Sequence(lambda n: f"Attribute {n}")
    active = True


class LocationFactory(OrgAttributeFactory):
    dimension = OrgAttribute.Dimension.LOCATION


class DepartmentFactory(OrgAttributeFactory):
    dimension = OrgAttribute.Dimension.DEPARTMENT


class PositionFactory(OrgAttributeFactory):
    dimension = OrgAttribute.Dimension.POSITION


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    full_name = factory.Sequence(lambda n: f"Test User {n}")
    role = User.Role.EMPLOYEE
    is_active = True

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        password = kwargs.pop("password", TEST_PASSWORD)
        return model_class.objects.create_user(*args, password=password, **kwargs)


class AdminFactory(UserFactory):
    role = User.Role.ADMIN
    is_staff = True


class ApproverFactory(UserFactory):
    role = User.Role.APPROVER


class CategoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ExpenseCategory

    code = factory.Sequence(lambda n: f"CAT{n}")
    name = factory.Sequence(lambda n: f"Category {n}")
    active = True


class CategoryFieldFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CategoryField

    category = factory.SubFactory(CategoryFactory)
    name = factory.Sequence(lambda n: f"Field {n}")
    field_type = CategoryField.FieldType.TEXT
    required = False
    active = True
    display_order = 0


class ReportFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ExpenseReport

    user = factory.SubFactory(UserFactory)
    title = factory.Sequence(lambda n: f"Report {n}")
    status = ExpenseReport.Status.DRAFT


class ExpenseFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Expense

    report = factory.SubFactory(ReportFactory)
    user = factory.SelfAttribute("report.user")
    merchant = factory.Sequence(lambda n: f"Merchant {n}")
    category = factory.SubFactory(CategoryFactory)
    expense_date = factory.LazyFunction(date.today)
    currency = "EUR"
    amount_decimal = "100.00"
    tax_rate_bps = 2100
    tax_included_input = False
    status = Expense.Status.DRAFT

    @classmethod
    def _prepare_financial_fields(cls, kwargs):
        amount_decimal = kwargs.pop("amount_decimal")
        tax_included = kwargs.pop("tax_included_input")
        amount_minor = parse_decimal_string(amount_decimal, kwargs["currency"])
        calculation = calculate_tax(
            amount_minor,
            kwargs["tax_rate_bps"],
            tax_included=tax_included,
        )
        kwargs.update(
            amount_minor=amount_minor,
            tax_included=tax_included,
            tax_amount_minor=calculation.tax_minor,
            total_amount_minor=calculation.total_minor,
        )
        return kwargs

    @classmethod
    def _build(cls, model_class, *args, **kwargs):
        return model_class(*args, **cls._prepare_financial_fields(kwargs))

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        kwargs = cls._prepare_financial_fields(kwargs)
        return model_class.objects.create(*args, **kwargs)


class ExpenseFieldValueFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ExpenseFieldValue

    expense = factory.SubFactory(ExpenseFactory)
    field = factory.SubFactory(
        CategoryFieldFactory,
        category=factory.SelfAttribute("..expense.category"),
    )
    value_text = "Stored answer"


class WarningRuleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WarningRule

    name = factory.Sequence(lambda n: f"Rule {n}")
    category = None
    currency = "EUR"
    threshold_minor = 5_000
    severity = WarningRule.Severity.WARNING
    message = factory.Sequence(lambda n: f"Warning {n}")
    active = True


class ExpenseWarningFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ExpenseWarning

    expense = factory.SubFactory(ExpenseFactory)
    warning_rule = factory.SubFactory(WarningRuleFactory)
    severity = factory.SelfAttribute("warning_rule.severity")
    message = factory.SelfAttribute("warning_rule.message")


class ApprovalRuleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ApprovalRule

    name = factory.Sequence(lambda n: f"Approval rule {n}")
    active = True
    priority = 100
    category = None
    currency = "EUR"
    threshold_minor = 0


class ApprovalStepFactory(factory.django.DjangoModelFactory):
    """Accepts ``approver=`` for a single-candidate step or ``approvers=[...]`` for a pool."""

    class Meta:
        model = ApprovalStep

    report = factory.SubFactory(ReportFactory)
    step_order = 1
    status = ApprovalStep.Status.PENDING
    origin = ApprovalStep.Origin.MANUAL

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        approver = kwargs.pop("approver", None)
        approvers = kwargs.pop("approvers", None)
        step = model_class.objects.create(*args, **kwargs)
        pool = list(approvers) if approvers is not None else [approver or ApproverFactory()]
        for user in pool:
            ApprovalStepCandidate.objects.create(step=step, user=user)
        return step


class ApprovalEventFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ApprovalEvent

    report = factory.SubFactory(ReportFactory)
    actor = factory.SubFactory(UserFactory)
    action = ApprovalEvent.Action.COMMENTED
    comment = "Test event"


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def admin(db):
    return AdminFactory()


@pytest.fixture
def approver(db):
    return ApproverFactory()


@pytest.fixture
def category(db):
    return CategoryFactory()


@pytest.fixture
def report(user):
    return ReportFactory(user=user)


@pytest.fixture
def expense(report, category):
    return ExpenseFactory(report=report, category=category)


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def authenticated_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def admin_client(admin):
    client = APIClient()
    client.force_authenticate(user=admin)
    return client


@pytest.fixture
def expense_payload(report, category):
    return {
        "reportId": str(report.id),
        "merchant": "Corner Cafe",
        "expenseDate": "2026-07-23",
        "categoryId": str(category.id),
        "currency": "EUR",
        "amountDecimal": "100.00",
        "tax": {"rateBps": 2100, "included": False},
    }
