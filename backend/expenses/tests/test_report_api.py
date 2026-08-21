import pytest
from rest_framework import status
from rest_framework.test import APIClient

from approvals.models import ApprovalStep
from conftest import (
    ApprovalEventFactory,
    ApprovalStepFactory,
    ApproverFactory,
    ExpenseFactory,
    ReportFactory,
    UserFactory,
)
from expenses.models import ExpenseReport

pytestmark = pytest.mark.django_db


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_report_create_list_detail_and_totals_exact_contract(authenticated_client, user):
    created = authenticated_client.post(
        "/api/reports/", {"title": "  July travel  "}, format="json"
    )
    assert created.status_code == status.HTTP_201_CREATED
    report = ExpenseReport.objects.get(pk=created.json()["id"])
    assert report.title == "July travel"
    ExpenseFactory(
        report=report,
        currency="EUR",
        amount_decimal="10.00",
        tax_rate_bps=0,
    )
    ExpenseFactory(
        report=report,
        currency="EUR",
        amount_decimal="5.50",
        tax_rate_bps=0,
    )
    ExpenseFactory(
        report=report,
        currency="USD",
        amount_decimal="2.00",
        tax_rate_bps=0,
    )

    listed = authenticated_client.get("/api/reports/").json()["results"][0]
    assert set(listed) == {
        "id",
        "title",
        "status",
        "expenseCount",
        "totals",
        "submittedAt",
        "createdAt",
        "updatedAt",
    }
    assert listed["expenseCount"] == 3
    assert listed["totals"] == [
        {"currency": "EUR", "minor": "1550", "decimal": "15.50", "display": "EUR 15.50"},
        {"currency": "USD", "minor": "200", "decimal": "2.00", "display": "USD 2.00"},
    ]

    detail = authenticated_client.get(f"/api/reports/{report.id}/")
    assert detail.status_code == status.HTTP_200_OK
    assert set(detail.json()) == {
        *set(listed),
        "owner",
        "expenses",
        "approvalSteps",
        "events",
    }
    assert detail.json()["owner"]["id"] == str(user.id)


def test_report_patch_and_delete_draft(authenticated_client, report):
    patched = authenticated_client.patch(
        f"/api/reports/{report.id}/", {"title": "Renamed"}, format="json"
    )
    assert patched.status_code == status.HTTP_200_OK
    assert patched.json()["title"] == "Renamed"
    assert authenticated_client.delete(f"/api/reports/{report.id}/").status_code == 204
    assert not ExpenseReport.objects.filter(pk=report.id).exists()


@pytest.mark.parametrize("operation", ["patch", "delete"])
def test_non_draft_report_cannot_be_changed(authenticated_client, report, operation):
    report.status = ExpenseReport.Status.SUBMITTED
    report.save(update_fields=["status"])
    response = getattr(authenticated_client, operation)(
        f"/api/reports/{report.id}/",
        {"title": "No"} if operation == "patch" else None,
        format="json",
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] in {"report_not_editable", "report_not_deletable"}


def test_report_with_prior_event_cannot_be_deleted(authenticated_client, report):
    ApprovalEventFactory(report=report, actor=report.user)
    response = authenticated_client.delete(f"/api/reports/{report.id}/")
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "report_not_deletable"


def test_admin_can_edit_foreign_draft_but_employee_cannot(authenticated_client, admin_client):
    report = ReportFactory(user=UserFactory())
    hidden = authenticated_client.patch(
        f"/api/reports/{report.id}/", {"title": "No access"}, format="json"
    )
    assert hidden.status_code == status.HTTP_404_NOT_FOUND
    updated = admin_client.patch(
        f"/api/reports/{report.id}/", {"title": "Admin edit"}, format="json"
    )
    assert updated.status_code == status.HTTP_200_OK
    assert updated.json()["title"] == "Admin edit"


@pytest.mark.parametrize(
    "payload",
    [{"title": "   "}, {"title": "Valid", "status": "APPROVED"}],
)
def test_report_write_validation_is_strict(authenticated_client, payload):
    response = authenticated_client.post("/api/reports/", payload, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_reports_role_filter_separates_owned_and_to_approve():
    owner = UserFactory()
    owned = ReportFactory(user=owner)
    to_approve = ReportFactory(user=UserFactory(), status=ExpenseReport.Status.SUBMITTED)
    ApprovalStepFactory(report=to_approve, approver=owner, step_order=1)

    client = _client_for(owner)
    all_ids = {item["id"] for item in client.get("/api/reports/").json()["results"]}
    assert all_ids == {str(owned.id), str(to_approve.id)}

    owned_ids = {item["id"] for item in client.get("/api/reports/?role=owner").json()["results"]}
    assert owned_ids == {str(owned.id)}

    approver_ids = {
        item["id"] for item in client.get("/api/reports/?role=approver").json()["results"]
    }
    assert approver_ids == {str(to_approve.id)}


def test_reports_pending_my_approval_only_returns_current_turn():
    owner = UserFactory()
    first = ApproverFactory()
    second = ApproverFactory()
    report = ReportFactory(user=owner, status=ExpenseReport.Status.SUBMITTED)
    ExpenseFactory(report=report)
    ApprovalStepFactory(report=report, approver=first, step_order=1)
    ApprovalStepFactory(report=report, approver=second, step_order=2)

    first_pending = _client_for(first).get("/api/reports/?pendingMyApproval=true").json()
    assert [item["id"] for item in first_pending["results"]] == [str(report.id)]

    # The second approver is assigned but it is not their turn yet.
    second_pending = _client_for(second).get("/api/reports/?pendingMyApproval=true").json()
    assert second_pending["results"] == []

    # After the first step is approved, the turn moves to the second approver.
    report.approval_steps.filter(step_order=1).update(status=ApprovalStep.Status.APPROVED)
    second_now = _client_for(second).get("/api/reports/?pendingMyApproval=true").json()
    assert [item["id"] for item in second_now["results"]] == [str(report.id)]

    # An owner with no pending step of their own gets nothing.
    owner_pending = _client_for(owner).get("/api/reports/?pendingMyApproval=true").json()
    assert owner_pending["results"] == []


def test_reports_status_filter():
    owner = UserFactory()
    draft = ReportFactory(user=owner, status=ExpenseReport.Status.DRAFT)
    ReportFactory(user=owner, status=ExpenseReport.Status.SUBMITTED)
    client = _client_for(owner)
    ids = {item["id"] for item in client.get("/api/reports/?status=DRAFT").json()["results"]}
    assert ids == {str(draft.id)}
