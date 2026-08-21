import pytest
from rest_framework import status

from conftest import (
    CategoryFactory,
    CategoryFieldFactory,
    ExpenseFactory,
    ExpenseFieldValueFactory,
    WarningRuleFactory,
)
from expenses.models import CategoryField, ExpenseCategory, WarningRule

pytestmark = pytest.mark.django_db


def rule_payload(**overrides):
    payload = {
        "name": "Large purchase",
        "categoryId": None,
        "currency": "EUR",
        "thresholdMinor": "10000",
        "severity": WarningRule.Severity.WARNING,
        "message": "Needs review",
        "active": True,
    }
    payload.update(overrides)
    return payload


def test_category_visibility_and_exact_read_contract(authenticated_client, admin_client):
    active = CategoryFactory(code=" travel ", name="Travel")
    inactive = CategoryFactory(name="Hidden", active=False)

    employee = authenticated_client.get("/api/categories/")
    admin = admin_client.get("/api/categories/")

    assert employee.status_code == status.HTTP_200_OK
    assert [item["id"] for item in employee.json()["results"]] == [str(active.id)]
    assert {item["id"] for item in admin.json()["results"]} == {str(active.id), str(inactive.id)}
    assert set(employee.json()["results"][0]) == {
        "id",
        "code",
        "name",
        "active",
        "customFields",
        "createdAt",
        "updatedAt",
    }


def test_category_admin_crud_normalizes_values(admin_client):
    created = admin_client.post(
        "/api/categories/",
        {"code": " meals ", "name": "  Meals  ", "active": True},
        format="json",
    )
    assert created.status_code == status.HTTP_201_CREATED
    assert (created.json()["code"], created.json()["name"]) == ("MEALS", "Meals")

    category_id = created.json()["id"]
    updated = admin_client.patch(
        f"/api/categories/{category_id}/", {"name": "Dining", "active": False}, format="json"
    )
    assert updated.status_code == status.HTTP_200_OK
    assert (updated.json()["name"], updated.json()["active"]) == ("Dining", False)
    assert admin_client.delete(f"/api/categories/{category_id}/").status_code == 204
    assert not ExpenseCategory.objects.filter(pk=category_id).exists()


def test_non_admin_category_writes_are_forbidden(authenticated_client):
    response = authenticated_client.post(
        "/api/categories/",
        {"code": "MEALS", "name": "Meals", "active": True},
        format="json",
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_referenced_category_delete_returns_domain_conflict(admin_client):
    category = CategoryFactory()
    ExpenseFactory(category=category)
    response = admin_client.delete(f"/api/categories/{category.id}/")
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json() == {
        "code": "category_in_use",
        "detail": "Referenced categories cannot be deleted.",
        "fields": {},
    }


def test_category_is_created_with_its_custom_fields_in_submitted_order(admin_client):
    response = admin_client.post(
        "/api/categories/",
        {
            "code": "TRAVEL",
            "name": "Travel",
            "active": True,
            "customFields": [
                {"name": " Trip code ", "fieldType": "TEXT", "required": True},
                {"name": "Nights", "fieldType": "NUMBER", "required": False},
                {"name": "Billable", "fieldType": "BOOLEAN", "required": False},
            ],
        },
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    fields = response.json()["customFields"]
    assert [(item["name"], item["fieldType"], item["displayOrder"]) for item in fields] == [
        ("Trip code", "TEXT", 0),
        ("Nights", "NUMBER", 1),
        ("Billable", "BOOLEAN", 2),
    ]
    assert fields[0]["required"] is True
    assert all(item["inUse"] is False for item in fields)
    assert set(fields[0]) == {
        "id",
        "name",
        "fieldType",
        "required",
        "active",
        "displayOrder",
        "inUse",
        "createdAt",
        "updatedAt",
    }


def test_patch_reconciles_custom_fields_by_id(admin_client):
    kept = CategoryFieldFactory(name="Trip code")
    dropped = CategoryFieldFactory(category=kept.category, name="Obsolete")

    response = admin_client.patch(
        f"/api/categories/{kept.category_id}/",
        {
            "customFields": [
                {"id": str(kept.id), "name": "Project code", "fieldType": "TEXT"},
                {"name": "Nights", "fieldType": "NUMBER"},
            ]
        },
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert [item["name"] for item in response.json()["customFields"]] == [
        "Project code",
        "Nights",
    ]
    assert not CategoryField.objects.filter(pk=dropped.id).exists()


def test_omitting_custom_fields_on_patch_leaves_them_alone(admin_client):
    field = CategoryFieldFactory()

    response = admin_client.patch(
        f"/api/categories/{field.category_id}/", {"name": "Renamed"}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert [item["id"] for item in response.json()["customFields"]] == [str(field.id)]


def test_custom_field_in_use_is_reported_and_cannot_be_removed(admin_client):
    value = ExpenseFieldValueFactory()
    field = value.field

    listed = admin_client.get(f"/api/categories/{field.category_id}/")
    removed = admin_client.patch(
        f"/api/categories/{field.category_id}/", {"customFields": []}, format="json"
    )

    assert listed.json()["customFields"][0]["inUse"] is True
    assert removed.status_code == status.HTTP_409_CONFLICT
    assert removed.json()["code"] == "category_field_in_use"
    assert CategoryField.objects.filter(pk=field.id).exists()


def test_custom_field_in_use_cannot_be_retyped_but_can_be_renamed(admin_client):
    value = ExpenseFieldValueFactory()
    field = value.field

    retyped = admin_client.patch(
        f"/api/categories/{field.category_id}/",
        {"customFields": [{"id": str(field.id), "name": field.name, "fieldType": "NUMBER"}]},
        format="json",
    )
    renamed = admin_client.patch(
        f"/api/categories/{field.category_id}/",
        {
            "customFields": [
                {"id": str(field.id), "name": "Renamed", "fieldType": "TEXT", "active": False}
            ]
        },
        format="json",
    )

    assert retyped.status_code == status.HTTP_400_BAD_REQUEST
    assert "cannot change" in str(retyped.json()["fields"]["customFields"])
    assert renamed.status_code == status.HTTP_200_OK
    assert renamed.json()["customFields"][0]["name"] == "Renamed"
    assert renamed.json()["customFields"][0]["active"] is False


def test_custom_field_names_must_be_unique_within_a_category(admin_client):
    response = admin_client.post(
        "/api/categories/",
        {
            "code": "TRAVEL",
            "name": "Travel",
            "customFields": [
                {"name": "Trip code", "fieldType": "TEXT"},
                {"name": "TRIP CODE", "fieldType": "NUMBER"},
            ],
        },
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "customFields" in response.json()["fields"]


def test_custom_field_from_another_category_is_rejected(admin_client):
    stranger = CategoryFieldFactory()
    category = CategoryFactory()

    response = admin_client.patch(
        f"/api/categories/{category.id}/",
        {"customFields": [{"id": str(stranger.id), "name": "Trip code", "fieldType": "TEXT"}]},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "customFields" in response.json()["fields"]


def test_non_admin_cannot_change_custom_fields(authenticated_client):
    field = CategoryFieldFactory()
    response = authenticated_client.patch(
        f"/api/categories/{field.category_id}/", {"customFields": []}, format="json"
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_warning_rule_visibility_order_and_threshold_string(authenticated_client, admin_client):
    visible_b = WarningRuleFactory(name="B rule", threshold_minor=9007199254740993)
    visible_a = WarningRuleFactory(name="A rule", threshold_minor=2)
    hidden = WarningRuleFactory(name="Hidden", active=False)

    response = authenticated_client.get("/api/warning-rules/")
    assert response.status_code == status.HTTP_200_OK
    assert [item["id"] for item in response.json()["results"]] == [
        str(visible_a.id),
        str(visible_b.id),
    ]
    assert response.json()["results"][1]["thresholdMinor"] == "9007199254740993"
    assert str(hidden.id) in {
        item["id"] for item in admin_client.get("/api/warning-rules/").json()["results"]
    }


def test_warning_rule_admin_crud_and_exact_contract(admin_client):
    created = admin_client.post("/api/warning-rules/", rule_payload(), format="json")
    assert created.status_code == status.HTTP_201_CREATED
    assert set(created.json()) == {
        "id",
        "name",
        "categoryId",
        "currency",
        "thresholdMinor",
        "severity",
        "message",
        "active",
        "createdAt",
        "updatedAt",
    }
    rule_id = created.json()["id"]
    updated = admin_client.patch(
        f"/api/warning-rules/{rule_id}/",
        {"thresholdMinor": "25000", "message": "Escalate"},
        format="json",
    )
    assert updated.status_code == status.HTTP_200_OK
    assert (updated.json()["thresholdMinor"], updated.json()["message"]) == ("25000", "Escalate")
    assert admin_client.delete(f"/api/warning-rules/{rule_id}/").status_code == 204


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"thresholdMinor": 100}, "thresholdMinor"),
        ({"thresholdMinor": "-1"}, "thresholdMinor"),
        ({"currency": "euro"}, "currency"),
        ({"unexpected": True}, "unexpected"),
    ],
)
def test_warning_rule_rejects_invalid_or_unknown_fields(admin_client, change, field):
    response = admin_client.post("/api/warning-rules/", rule_payload(**change), format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert field in response.json()["fields"]
