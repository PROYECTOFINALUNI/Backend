from copy import deepcopy

import pytest
from rest_framework import status

from conftest import (
    CategoryFactory,
    CategoryFieldFactory,
    ExpenseFactory,
    ReportFactory,
    UserFactory,
)
from expenses.models import CategoryField, Expense, ExpenseFieldValue, ExpenseReport

pytestmark = pytest.mark.django_db

EXPENSE_KEYS = {
    "id",
    "reportId",
    "merchant",
    "expenseDate",
    "category",
    "status",
    "amount",
    "netAmount",
    "tax",
    "total",
    "warnings",
    "customFields",
    "createdAt",
    "updatedAt",
}
CATEGORY_KEYS = {"id", "code", "name", "active", "customFields", "createdAt", "updatedAt"}
MONEY_KEYS = {"currency", "minor", "decimal", "display"}
TAX_KEYS = {"rateBps", "included", "minor", "decimal", "display"}


def answers(*pairs):
    return [{"fieldId": str(field.id), "value": value} for field, value in pairs]


def assert_expense_shape(body):
    assert set(body) == EXPENSE_KEYS
    assert set(body["amount"]) == MONEY_KEYS
    assert set(body["netAmount"]) == MONEY_KEYS
    assert set(body["tax"]) == TAX_KEYS
    assert set(body["total"]) == MONEY_KEYS
    assert set(body["category"]) == CATEGORY_KEYS


def test_expense_endpoints_require_authentication(api_client):
    for method, path in [
        ("get", "/api/expenses/"),
        ("post", "/api/expenses/"),
        ("post", "/api/expenses/calculate-preview/"),
    ]:
        response = getattr(api_client, method)(path, {}, format="json")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["code"] == "not_authenticated"


def test_create_report_then_expense_derives_user(authenticated_client, user, category):
    report_response = authenticated_client.post(
        "/api/reports/", {"title": "July travel"}, format="json"
    )
    assert report_response.status_code == status.HTTP_201_CREATED
    report_id = report_response.json()["id"]
    payload = {
        "reportId": report_id,
        "merchant": "Rail",
        "expenseDate": "2026-07-20",
        "categoryId": str(category.id),
        "currency": "EUR",
        "amountDecimal": "100.00",
        "tax": {"rateBps": 2100, "included": False},
    }
    response = authenticated_client.post("/api/expenses/", payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert_expense_shape(body)
    assert body["reportId"] == report_id
    assert body["amount"]["minor"] == "10000"
    assert body["netAmount"]["minor"] == "10000"
    assert body["tax"] == {
        "rateBps": 2100,
        "included": False,
        "minor": "2100",
        "decimal": "21.00",
        "display": "EUR 21.00",
    }
    assert body["total"]["minor"] == "12100"
    assert Expense.objects.get(pk=body["id"]).user == user


@pytest.mark.parametrize("report_value", ["missing", None])
def test_create_requires_non_null_report_id(authenticated_client, expense_payload, report_value):
    payload = deepcopy(expense_payload)
    if report_value == "missing":
        payload.pop("reportId")
    else:
        payload["reportId"] = None
    response = authenticated_client.post("/api/expenses/", payload, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "reportId" in response.json()["fields"]


def test_foreign_report_is_hidden(authenticated_client, expense_payload):
    expense_payload["reportId"] = str(ReportFactory(user=UserFactory()).id)
    response = authenticated_client.post("/api/expenses/", expense_payload, format="json")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.parametrize(
    "report_status",
    [
        ExpenseReport.Status.SUBMITTED,
        ExpenseReport.Status.APPROVED,
        ExpenseReport.Status.REJECTED,
        ExpenseReport.Status.PAID,
    ],
)
def test_create_conflicts_for_non_draft_report(
    authenticated_client, report, expense_payload, report_status
):
    report.status = report_status
    report.save(update_fields=["status"])
    response = authenticated_client.post("/api/expenses/", expense_payload, format="json")
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "invalid_state_transition"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("userId", "00000000-0000-0000-0000-000000000000"),
        ("status", "APPROVED"),
        ("amountMinor", "1"),
        ("taxAmountMinor", "1"),
        ("totalAmountMinor", "1"),
        ("createdAt", "2026-01-01T00:00:00Z"),
        ("netAmount", {"minor": "1"}),
        ("total", {"minor": "1"}),
    ],
)
def test_create_rejects_server_managed_fields(authenticated_client, expense_payload, field, value):
    expense_payload[field] = value
    response = authenticated_client.post("/api/expenses/", expense_payload, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert field in response.json()["fields"]


def test_tax_included_response_shape(authenticated_client, expense_payload):
    expense_payload["amountDecimal"] = "121.00"
    expense_payload["tax"] = {"rateBps": 2100, "included": True}
    response = authenticated_client.post("/api/expenses/", expense_payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["amount"]["minor"] == "12100"
    assert body["netAmount"]["minor"] == "10000"
    assert body["tax"]["minor"] == "2100"
    assert body["total"]["minor"] == "12100"


def test_patch_recalculates_amount_and_tax(authenticated_client, expense):
    response = authenticated_client.patch(
        f"/api/expenses/{expense.id}/",
        {
            "amountDecimal": "50.00",
            "tax": {"rateBps": 1000, "included": False},
        },
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["amount"]["minor"] == "5000"
    assert body["tax"]["minor"] == "500"
    assert body["total"]["minor"] == "5500"
    expense.refresh_from_db()
    assert (expense.amount_minor, expense.tax_amount_minor, expense.total_amount_minor) == (
        5000,
        500,
        5500,
    )


def test_patch_report_id_is_immutable(authenticated_client, expense):
    response = authenticated_client.patch(
        f"/api/expenses/{expense.id}/",
        {"reportId": str(ReportFactory(user=expense.user).id)},
        format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "reportId" in response.json()["fields"]


@pytest.mark.parametrize(
    "operation",
    [
        ("patch", {"merchant": "Changed"}),
        ("delete", None),
    ],
)
def test_patch_and_delete_only_allow_draft_reports(authenticated_client, expense, operation):
    expense.report.status = ExpenseReport.Status.SUBMITTED
    expense.report.save(update_fields=["status"])
    method, payload = operation
    response = getattr(authenticated_client, method)(
        f"/api/expenses/{expense.id}/", payload, format="json"
    )
    assert response.status_code == status.HTTP_409_CONFLICT


def test_delete_draft_expense(authenticated_client, expense):
    response = authenticated_client.delete(f"/api/expenses/{expense.id}/")
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert not Expense.objects.filter(pk=expense.id).exists()


def test_approved_expense_is_immutable(authenticated_client, expense):
    expense.report.status = ExpenseReport.Status.APPROVED
    expense.report.save(update_fields=["status"])
    for method, payload in [
        ("patch", {"merchant": "Changed"}),
        ("delete", None),
    ]:
        response = getattr(authenticated_client, method)(
            f"/api/expenses/{expense.id}/", payload, format="json"
        )
        assert response.status_code == status.HTTP_409_CONFLICT
    expense.refresh_from_db()
    assert expense.merchant != "Changed"


def test_read_money_minor_values_are_strings(authenticated_client, expense):
    response = authenticated_client.get(f"/api/expenses/{expense.id}/")
    body = response.json()
    assert response.status_code == status.HTTP_200_OK
    assert all(
        isinstance(body[field]["minor"], str) for field in ("amount", "netAmount", "tax", "total")
    )


def test_preview_does_not_persist_and_matches_create(authenticated_client, expense_payload):
    before = Expense.objects.count()
    preview = authenticated_client.post(
        "/api/expenses/calculate-preview/", expense_payload, format="json"
    )
    assert preview.status_code == status.HTTP_200_OK
    assert Expense.objects.count() == before
    created = authenticated_client.post("/api/expenses/", expense_payload, format="json")
    assert created.status_code == status.HTTP_201_CREATED
    preview_body = preview.json()
    created_body = created.json()
    assert set(preview_body) == {"amount", "netAmount", "tax", "total", "warnings"}
    for field in ("amount", "netAmount", "tax", "total"):
        assert preview_body[field] == created_body[field]
    assert [
        {"severity": item["severity"], "message": item["message"]}
        for item in created_body["warnings"]
    ] == preview_body["warnings"]


def test_custom_field_values_are_coerced_and_read_back_in_display_order(
    authenticated_client, expense_payload, category
):
    text = CategoryFieldFactory(category=category, name="Trip code", display_order=0)
    number = CategoryFieldFactory(
        category=category,
        name="Nights",
        field_type=CategoryField.FieldType.NUMBER,
        display_order=1,
    )
    flag = CategoryFieldFactory(
        category=category,
        name="Billable",
        field_type=CategoryField.FieldType.BOOLEAN,
        display_order=2,
    )
    expense_payload["customFields"] = answers((flag, False), (text, "  TRIP-9  "), (number, "3.5"))

    response = authenticated_client.post("/api/expenses/", expense_payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["customFields"] == [
        {"fieldId": str(text.id), "name": "Trip code", "fieldType": "TEXT", "value": "TRIP-9"},
        {"fieldId": str(number.id), "name": "Nights", "fieldType": "NUMBER", "value": "3.5"},
        {"fieldId": str(flag.id), "name": "Billable", "fieldType": "BOOLEAN", "value": False},
    ]


def test_required_custom_field_blocks_the_expense(authenticated_client, expense_payload, category):
    field = CategoryFieldFactory(category=category, name="Trip code", required=True)

    missing = authenticated_client.post("/api/expenses/", expense_payload, format="json")
    blank = authenticated_client.post(
        "/api/expenses/",
        {**expense_payload, "customFields": answers((field, "   "))},
        format="json",
    )

    for response in (missing, blank):
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert f"customFields.{field.id}" in response.json()["fields"]
    assert Expense.objects.count() == 0


@pytest.mark.parametrize(
    ("field_type", "value"),
    [
        (CategoryField.FieldType.NUMBER, "not a number"),
        (CategoryField.FieldType.NUMBER, True),
        (CategoryField.FieldType.BOOLEAN, "yes"),
        (CategoryField.FieldType.TEXT, 42),
    ],
)
def test_custom_field_value_must_match_its_type(
    authenticated_client, expense_payload, category, field_type, value
):
    field = CategoryFieldFactory(category=category, field_type=field_type)
    expense_payload["customFields"] = answers((field, value))

    response = authenticated_client.post("/api/expenses/", expense_payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert f"customFields.{field.id}" in response.json()["fields"]


def test_custom_field_from_another_category_is_rejected(authenticated_client, expense_payload):
    stranger = CategoryFieldFactory()
    expense_payload["customFields"] = answers((stranger, "value"))

    response = authenticated_client.post("/api/expenses/", expense_payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert f"customFields.{stranger.id}" in response.json()["fields"]


def test_inactive_custom_field_is_not_collected_but_stays_readable(
    authenticated_client, expense_payload, category
):
    field = CategoryFieldFactory(category=category, name="Legacy code")
    created = authenticated_client.post(
        "/api/expenses/",
        {**expense_payload, "customFields": answers((field, "LEG-1"))},
        format="json",
    )
    field.active = False
    field.save(update_fields=["active"])

    patched = authenticated_client.patch(
        f"/api/expenses/{created.json()['id']}/", {"merchant": "Renamed"}, format="json"
    )

    assert patched.status_code == status.HTTP_200_OK
    assert patched.json()["customFields"] == [
        {
            "fieldId": str(field.id),
            "name": "Legacy code",
            "fieldType": "TEXT",
            "value": "LEG-1",
        }
    ]


def test_patch_without_custom_fields_keeps_the_stored_answers(
    authenticated_client, expense_payload, category
):
    field = CategoryFieldFactory(category=category, name="Trip code", required=True)
    created = authenticated_client.post(
        "/api/expenses/",
        {**expense_payload, "customFields": answers((field, "TRIP-9"))},
        format="json",
    )

    response = authenticated_client.patch(
        f"/api/expenses/{created.json()['id']}/", {"merchant": "Renamed"}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["customFields"][0]["value"] == "TRIP-9"


def test_switching_category_discards_the_previous_values(
    authenticated_client, expense_payload, category
):
    old_field = CategoryFieldFactory(category=category, name="Trip code")
    new_category = CategoryFactory()
    created = authenticated_client.post(
        "/api/expenses/",
        {**expense_payload, "customFields": answers((old_field, "TRIP-9"))},
        format="json",
    )

    response = authenticated_client.patch(
        f"/api/expenses/{created.json()['id']}/",
        {"categoryId": str(new_category.id)},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["customFields"] == []
    assert not ExpenseFieldValue.objects.filter(field=old_field).exists()


def test_switching_to_a_category_with_a_required_field_demands_a_value(
    authenticated_client, expense_payload
):
    new_category = CategoryFactory()
    required = CategoryFieldFactory(category=new_category, name="Trip code", required=True)
    created = authenticated_client.post("/api/expenses/", expense_payload, format="json")

    response = authenticated_client.patch(
        f"/api/expenses/{created.json()['id']}/",
        {"categoryId": str(new_category.id)},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert f"customFields.{required.id}" in response.json()["fields"]


def test_clearing_the_category_discards_the_values(authenticated_client, expense_payload, category):
    field = CategoryFieldFactory(category=category, name="Trip code")
    created = authenticated_client.post(
        "/api/expenses/",
        {**expense_payload, "customFields": answers((field, "TRIP-9"))},
        format="json",
    )

    response = authenticated_client.patch(
        f"/api/expenses/{created.json()['id']}/", {"categoryId": None}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["customFields"] == []


def test_optional_custom_field_can_be_cleared_by_sending_null(
    authenticated_client, expense_payload, category
):
    field = CategoryFieldFactory(category=category, name="Trip code")
    created = authenticated_client.post(
        "/api/expenses/",
        {**expense_payload, "customFields": answers((field, "TRIP-9"))},
        format="json",
    )

    response = authenticated_client.patch(
        f"/api/expenses/{created.json()['id']}/",
        {"customFields": answers((field, None))},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["customFields"] == []


def test_preview_ignores_custom_fields(authenticated_client, expense_payload, category):
    field = CategoryFieldFactory(category=category, required=True)
    expense_payload["customFields"] = answers((field, "TRIP-9"))

    response = authenticated_client.post(
        "/api/expenses/calculate-preview/", expense_payload, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert set(response.json()) == {"amount", "netAmount", "tax", "total", "warnings"}


def test_list_filters_by_report_category_currency_and_merchant(
    authenticated_client, user, category
):
    report = ReportFactory(user=user)
    matching = ExpenseFactory(
        report=report, category=category, currency="EUR", merchant="Needle Cafe"
    )
    ExpenseFactory(report=report, category=CategoryFactory(), merchant="Other")
    response = authenticated_client.get(
        "/api/expenses/",
        {
            "reportId": str(report.id),
            "categoryId": str(category.id),
            "currency": "EUR",
            "merchant": "needle",
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assert [item["id"] for item in response.json()["results"]] == [str(matching.id)]
