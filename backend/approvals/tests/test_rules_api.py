import pytest
from rest_framework import status
from rest_framework.test import APIClient

from approvals.models import ApprovalRule
from conftest import (
    AdminFactory,
    ApprovalRuleFactory,
    ApproverFactory,
    DepartmentFactory,
    ExpenseFactory,
    LocationFactory,
    PositionFactory,
    ReportFactory,
    UserFactory,
)
from users.models import OrgAttribute

pytestmark = pytest.mark.django_db


def rule_payload(**overrides):
    payload = {
        "name": "Spanish IT managers",
        "active": True,
        "priority": 10,
        "categoryId": None,
        "currency": "EUR",
        "thresholdMinor": "10000",
    }
    payload.update(overrides)
    return payload


def test_org_attribute_visibility_and_read_contract(authenticated_client, admin_client):
    active = LocationFactory(code=" es ", name="Spain")
    inactive = DepartmentFactory(name="Hidden", active=False)

    employee = authenticated_client.get("/api/org-attributes/")
    admin = admin_client.get("/api/org-attributes/")

    assert [item["id"] for item in employee.json()["results"]] == [str(active.id)]
    assert {item["id"] for item in admin.json()["results"]} == {str(active.id), str(inactive.id)}
    assert set(employee.json()["results"][0]) == {
        "id",
        "dimension",
        "code",
        "name",
        "active",
        "createdAt",
        "updatedAt",
    }
    assert employee.json()["results"][0]["code"] == "ES"


def test_org_attributes_filter_by_dimension(admin_client):
    location = LocationFactory()
    DepartmentFactory()

    response = admin_client.get("/api/org-attributes/", {"dimension": "LOCATION"})

    assert [item["id"] for item in response.json()["results"]] == [str(location.id)]


def test_org_attribute_admin_crud_and_immutable_dimension(admin_client):
    created = admin_client.post(
        "/api/org-attributes/",
        {"dimension": "POSITION", "code": " manager ", "name": "  Manager  ", "active": True},
        format="json",
    )
    assert created.status_code == status.HTTP_201_CREATED
    assert (created.json()["code"], created.json()["name"]) == ("MANAGER", "Manager")

    attribute_id = created.json()["id"]
    rejected = admin_client.patch(
        f"/api/org-attributes/{attribute_id}/", {"dimension": "LOCATION"}, format="json"
    )
    assert rejected.status_code == status.HTTP_400_BAD_REQUEST

    assert admin_client.delete(f"/api/org-attributes/{attribute_id}/").status_code == 204
    assert not OrgAttribute.objects.filter(pk=attribute_id).exists()


def test_duplicate_code_within_a_dimension_is_rejected(admin_client):
    LocationFactory(code="ES")
    duplicate = admin_client.post(
        "/api/org-attributes/",
        {"dimension": "LOCATION", "code": "es", "name": "Spain again", "active": True},
        format="json",
    )
    assert duplicate.status_code == status.HTTP_400_BAD_REQUEST
    allowed = admin_client.post(
        "/api/org-attributes/",
        {"dimension": "DEPARTMENT", "code": "ES", "name": "Escalations", "active": True},
        format="json",
    )
    assert allowed.status_code == status.HTTP_201_CREATED


def test_referenced_org_attribute_cannot_be_deleted(admin_client):
    location = LocationFactory()
    UserFactory(location=location)

    response = admin_client.delete(f"/api/org-attributes/{location.id}/")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "org_attribute_in_use"


def test_org_attribute_writes_are_admin_only(authenticated_client):
    response = authenticated_client.post(
        "/api/org-attributes/",
        {"dimension": "LOCATION", "code": "ES", "name": "Spain", "active": True},
        format="json",
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_user_can_be_created_and_patched_with_org_attributes(admin_client):
    location = LocationFactory(name="Spain")
    department = DepartmentFactory(name="IT")
    position = PositionFactory(name="Manager")

    created = admin_client.post(
        "/api/users/",
        {
            "email": "routed@example.com",
            "fullName": "Routed User",
            "role": "APPROVER",
            "password": "Strong-Password-42!",
            "locationId": str(location.id),
            "departmentId": str(department.id),
        },
        format="json",
    )
    assert created.status_code == status.HTTP_201_CREATED
    assert created.json()["location"]["name"] == "Spain"
    assert created.json()["position"] is None

    patched = admin_client.patch(
        f"/api/users/{created.json()['id']}/",
        {"positionId": str(position.id), "locationId": None},
        format="json",
    )
    assert patched.status_code == status.HTTP_200_OK
    assert patched.json()["position"]["name"] == "Manager"
    assert patched.json()["location"] is None


def test_user_rejects_an_attribute_from_the_wrong_dimension(admin_client):
    department = DepartmentFactory()
    response = admin_client.patch(
        f"/api/users/{UserFactory().id}/", {"locationId": str(department.id)}, format="json"
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "locationId" in response.json()["fields"]


def test_users_can_be_filtered_by_org_attribute(admin_client):
    department = DepartmentFactory()
    matching = UserFactory(department=department)
    UserFactory()

    response = admin_client.get("/api/users/", {"department": str(department.id)})

    assert [item["id"] for item in response.json()["results"]] == [str(matching.id)]


def test_approval_rule_admin_crud_and_read_contract(admin_client):
    location = LocationFactory()
    position = PositionFactory()
    ApproverFactory(location=location, position=position)

    created = admin_client.post(
        "/api/approval-rules/",
        rule_payload(
            submitterLocationId=str(location.id),
            approverLocationId=str(location.id),
            approverPositionId=str(position.id),
            approverRole="APPROVER",
        ),
        format="json",
    )
    assert created.status_code == status.HTTP_201_CREATED
    body = created.json()
    assert set(body) == {
        "id",
        "name",
        "active",
        "priority",
        "categoryId",
        "currency",
        "thresholdMinor",
        "submitterLocationId",
        "submitterDepartmentId",
        "submitterPositionId",
        "approverRole",
        "approverLocationId",
        "approverDepartmentId",
        "approverPositionId",
        "approverPoolSize",
        "createdAt",
        "updatedAt",
    }
    assert body["thresholdMinor"] == "10000"
    assert body["approverPoolSize"] == 1
    assert body["submitterDepartmentId"] is None

    updated = admin_client.patch(
        f"/api/approval-rules/{body['id']}/", {"priority": 50, "active": False}, format="json"
    )
    assert (updated.json()["priority"], updated.json()["active"]) == (50, False)
    assert admin_client.delete(f"/api/approval-rules/{body['id']}/").status_code == 204
    assert not ApprovalRule.objects.filter(pk=body["id"]).exists()


def test_approval_rule_validation_rejects_bad_criteria(admin_client):
    department = DepartmentFactory()

    invalid_currency = admin_client.post(
        "/api/approval-rules/", rule_payload(currency="XYZ"), format="json"
    )
    assert invalid_currency.status_code == status.HTTP_400_BAD_REQUEST

    wrong_dimension = admin_client.post(
        "/api/approval-rules/",
        rule_payload(approverLocationId=str(department.id)),
        format="json",
    )
    assert wrong_dimension.status_code == status.HTTP_400_BAD_REQUEST

    unknown_field = admin_client.post(
        "/api/approval-rules/", rule_payload(severity="INFO"), format="json"
    )
    assert unknown_field.status_code == status.HTTP_400_BAD_REQUEST

    bad_role = admin_client.post(
        "/api/approval-rules/", rule_payload(approverRole="EMPLOYEE"), format="json"
    )
    assert bad_role.status_code == status.HTTP_400_BAD_REQUEST


def test_approval_rule_visibility_and_write_permissions(authenticated_client):
    ApprovalRuleFactory(active=False)
    active = ApprovalRuleFactory()

    listed = authenticated_client.get("/api/approval-rules/")
    assert [item["id"] for item in listed.json()["results"]] == [str(active.id)]

    denied = authenticated_client.post("/api/approval-rules/", rule_payload(), format="json")
    assert denied.status_code == status.HTTP_403_FORBIDDEN


def test_approval_preview_reports_the_chain_rules_would_build():
    submitter = UserFactory()
    approver = ApproverFactory()
    rule = ApprovalRuleFactory(name="Any expense")
    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report)
    client = APIClient()
    client.force_authenticate(user=submitter)

    body = client.get(f"/api/reports/{report.id}/approval-preview/").json()

    assert body["autoApprove"] is False
    assert body["manualOverride"] is False
    assert body["steps"] == [
        {
            "stepOrder": 1,
            "ruleId": str(rule.id),
            "ruleName": "Any expense",
            "approvers": [
                {
                    **{key: value for key, value in body["steps"][0]["approvers"][0].items()},
                }
            ],
        }
    ]
    assert body["steps"][0]["approvers"][0]["id"] == str(approver.id)


def test_approval_preview_announces_auto_approval():
    submitter = UserFactory()
    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report)
    client = APIClient()
    client.force_authenticate(user=submitter)

    body = client.get(f"/api/reports/{report.id}/approval-preview/").json()

    assert (body["autoApprove"], body["steps"]) == (True, [])


def test_submit_without_rules_auto_approves_via_the_api():
    submitter = UserFactory()
    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report)
    client = APIClient()
    client.force_authenticate(user=submitter)

    body = client.post(f"/api/reports/{report.id}/submit/", {}, format="json").json()

    assert body["status"] == "APPROVED"
    assert body["approvalSteps"] == []
    assert [event["action"] for event in body["events"]] == ["SUBMITTED", "AUTO_APPROVED"]


def test_delegate_endpoint_moves_the_step_to_a_new_approver():
    submitter = UserFactory()
    original = ApproverFactory(full_name="Ana")
    target = ApproverFactory(full_name="Bob")
    ApprovalRuleFactory(approver_role="APPROVER")
    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report)

    owner_client = APIClient()
    owner_client.force_authenticate(user=submitter)
    owner_client.post(f"/api/reports/{report.id}/submit/", {}, format="json")
    report.approval_steps.get().candidates.exclude(user=original).delete()

    approver_client = APIClient()
    approver_client.force_authenticate(user=original)
    response = approver_client.post(
        f"/api/reports/{report.id}/delegate/",
        {"approverId": str(target.id), "comment": "Out of office"},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    step = response.json()["approvalSteps"][0]
    assert [entry["id"] for entry in step["approvers"]] == [str(target.id)]
    assert step["origin"] == "DELEGATED"
    assert response.json()["events"][-1]["action"] == "DELEGATED"


def test_a_later_step_approver_cannot_delegate_the_current_step():
    spain = LocationFactory(code="ES")
    france = LocationFactory(code="FR")
    submitter = UserFactory()
    ApproverFactory(location=spain)
    later = ApproverFactory(location=france)
    ApprovalRuleFactory(priority=10, approver_location=spain)
    ApprovalRuleFactory(priority=20, approver_location=france)
    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report)

    owner_client = APIClient()
    owner_client.force_authenticate(user=submitter)
    owner_client.post(f"/api/reports/{report.id}/submit/", {}, format="json")

    client = APIClient()
    client.force_authenticate(user=later)
    response = client.post(
        f"/api/reports/{report.id}/delegate/", {"approverId": str(later.id)}, format="json"
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "not_current_approver"


def test_admin_can_delegate_on_behalf_of_the_pool():
    submitter = UserFactory()
    ApproverFactory()
    target = ApproverFactory()
    ApprovalRuleFactory(approver_role="APPROVER")
    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report)

    owner_client = APIClient()
    owner_client.force_authenticate(user=submitter)
    owner_client.post(f"/api/reports/{report.id}/submit/", {}, format="json")

    admin_client = APIClient()
    admin_client.force_authenticate(user=AdminFactory())
    response = admin_client.post(
        f"/api/reports/{report.id}/delegate/", {"approverId": str(target.id)}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert [entry["id"] for entry in response.json()["approvalSteps"][0]["approvers"]] == [
        str(target.id)
    ]


def test_pool_member_sees_the_report_in_their_pending_inbox():
    submitter = UserFactory()
    first = ApproverFactory(full_name="Ana")
    second = ApproverFactory(full_name="Bob")
    ApprovalRuleFactory(approver_role="APPROVER")
    report = ReportFactory(user=submitter)
    ExpenseFactory(report=report)

    owner_client = APIClient()
    owner_client.force_authenticate(user=submitter)
    owner_client.post(f"/api/reports/{report.id}/submit/", {}, format="json")

    for approver in (first, second):
        client = APIClient()
        client.force_authenticate(user=approver)
        pending = client.get("/api/reports/", {"pendingMyApproval": "true"})
        assert [item["id"] for item in pending.json()["results"]] == [str(report.id)]
