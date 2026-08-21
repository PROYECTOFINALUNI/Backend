import pytest
from rest_framework import status
from rest_framework.test import APIClient

from approvals.models import ApprovalEvent, ApprovalStep
from conftest import (
    AdminFactory,
    ApprovalStepFactory,
    ApproverFactory,
    ExpenseFactory,
    ReportFactory,
    UserFactory,
)
from expenses.models import ExpenseReport

pytestmark = pytest.mark.django_db


def submitted_report():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    ExpenseFactory(report=report)
    approver = ApproverFactory()
    ApprovalStepFactory(report=report, approver=approver)
    owner_client = APIClient()
    owner_client.force_authenticate(user=owner)
    response = owner_client.post(f"/api/reports/{report.id}/submit/", {}, format="json")
    assert response.status_code == status.HTTP_200_OK
    return report, owner, approver, owner_client


def test_reject_comment_is_required_and_cannot_be_blank():
    report, _, approver, _ = submitted_report()
    client = APIClient()
    client.force_authenticate(user=approver)
    for payload in ({}, {"comment": ""}, {"comment": "   "}):
        response = client.post(f"/api/reports/{report.id}/reject/", payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "comment" in response.json()["fields"]


def test_future_approver_cannot_approve_via_api():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    ExpenseFactory(report=report)
    ApprovalStepFactory(report=report, approver=ApproverFactory(), step_order=1)
    future = ApproverFactory()
    ApprovalStepFactory(report=report, approver=future, step_order=2)
    owner_client = APIClient()
    owner_client.force_authenticate(user=owner)
    owner_client.post(f"/api/reports/{report.id}/submit/", {}, format="json")
    client = APIClient()
    client.force_authenticate(user=future)
    response = client.post(
        f"/api/reports/{report.id}/approve/", {"comment": "Early"}, format="json"
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "not_current_approver"


def test_approval_api_returns_camel_case_steps_and_events():
    report, _, approver, _ = submitted_report()
    client = APIClient()
    client.force_authenticate(user=approver)
    response = client.post(
        f"/api/reports/{report.id}/approve/",
        {"comment": "Approved"},
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == ExpenseReport.Status.APPROVED
    step = body["approvalSteps"][0]
    assert set(step) == {
        "id",
        "approvers",
        "stepOrder",
        "status",
        "origin",
        "ruleName",
        "decidedBy",
        "decidedAt",
        "comment",
        "createdAt",
        "updatedAt",
    }
    assert [entry["id"] for entry in step["approvers"]] == [str(approver.id)]
    assert step["decidedBy"]["id"] == str(approver.id)
    assert body["events"][-1]["action"] == ApprovalEvent.Action.APPROVED
    assert "createdAt" in body["events"][-1]


def test_only_admin_can_mark_paid_via_api():
    report, _, approver, owner_client = submitted_report()
    approver_client = APIClient()
    approver_client.force_authenticate(user=approver)
    approver_client.post(f"/api/reports/{report.id}/approve/", {}, format="json")
    denied = owner_client.post(f"/api/reports/{report.id}/mark-paid/", {}, format="json")
    assert denied.status_code == status.HTTP_409_CONFLICT
    admin_client = APIClient()
    admin_client.force_authenticate(user=AdminFactory())
    paid = admin_client.post(f"/api/reports/{report.id}/mark-paid/", {}, format="json")
    assert paid.status_code == status.HTTP_200_OK
    assert paid.json()["status"] == ExpenseReport.Status.PAID


def test_admin_can_create_update_list_and_delete_approval_steps():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    first = ApproverFactory()
    second = ApproverFactory()
    client = APIClient()
    client.force_authenticate(user=AdminFactory())

    created = client.post(
        f"/api/reports/{report.id}/approval-steps/",
        {"approverId": str(first.id), "stepOrder": 1},
        format="json",
    )
    assert created.status_code == status.HTTP_201_CREATED
    step_id = created.json()["id"]
    updated = client.patch(
        f"/api/reports/{report.id}/approval-steps/{step_id}/",
        {"approverId": str(second.id), "stepOrder": 2},
        format="json",
    )
    assert updated.status_code == status.HTTP_200_OK
    assert [entry["id"] for entry in updated.json()["approvers"]] == [str(second.id)]
    assert updated.json()["stepOrder"] == 2

    owner_client = APIClient()
    owner_client.force_authenticate(user=owner)
    listed = owner_client.get(f"/api/reports/{report.id}/approval-steps/")
    assert listed.status_code == status.HTTP_200_OK
    assert [item["id"] for item in listed.json()] == [step_id]

    deleted = client.delete(f"/api/reports/{report.id}/approval-steps/{step_id}/")
    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert not ApprovalStep.objects.filter(pk=step_id).exists()


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_non_admin_cannot_modify_approval_step(method):
    owner = UserFactory()
    report = ReportFactory(user=owner)
    step = ApprovalStepFactory(report=report)
    client = APIClient()
    client.force_authenticate(user=owner)
    response = getattr(client, method)(
        f"/api/reports/{report.id}/approval-steps/{step.id}/",
        {"stepOrder": 2} if method == "patch" else None,
        format="json",
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_missing_approval_step_returns_not_found():
    report = ReportFactory(user=UserFactory())
    client = APIClient()
    client.force_authenticate(user=AdminFactory())
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        client.patch(
            f"/api/reports/{report.id}/approval-steps/{missing}/",
            {"stepOrder": 1},
            format="json",
        ).status_code
        == status.HTTP_404_NOT_FOUND
    )
    assert (
        client.delete(f"/api/reports/{report.id}/approval-steps/{missing}/").status_code
        == status.HTTP_404_NOT_FOUND
    )


def test_owner_and_assigned_approver_can_comment_but_outsider_cannot():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    approver = ApproverFactory()
    ApprovalStepFactory(report=report, approver=approver)

    for actor, comment in [(owner, "Owner note"), (approver, "Approver note")]:
        client = APIClient()
        client.force_authenticate(user=actor)
        response = client.post(
            f"/api/reports/{report.id}/comment/", {"comment": comment}, format="json"
        )
        assert response.status_code == status.HTTP_200_OK

    outsider = UserFactory()
    client = APIClient()
    client.force_authenticate(user=outsider)
    hidden = client.post(
        f"/api/reports/{report.id}/comment/", {"comment": "No access"}, format="json"
    )
    assert hidden.status_code == status.HTTP_404_NOT_FOUND
    assert list(report.approval_events.values_list("comment", flat=True)) == [
        "Owner note",
        "Approver note",
    ]
