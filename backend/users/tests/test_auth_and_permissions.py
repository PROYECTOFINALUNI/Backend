import pytest
from rest_framework import status
from rest_framework.test import APIClient

from conftest import (
    TEST_PASSWORD,
    ApprovalStepFactory,
    ApproverFactory,
    ExpenseFactory,
    ReportFactory,
    UserFactory,
)
from users.models import User

pytestmark = pytest.mark.django_db


def test_login_returns_jwt_pair_and_camel_case_user():
    user = UserFactory(email="login@example.com")
    client = APIClient()
    response = client.post(
        "/api/auth/login/",
        {"email": user.email.upper(), "password": TEST_PASSWORD},
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {"access", "refresh", "user"}
    assert set(body["user"]) == {
        "id",
        "email",
        "fullName",
        "role",
        "active",
        "location",
        "department",
        "position",
        "createdAt",
        "updatedAt",
    }
    bearer = APIClient()
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    me = bearer.get("/api/me/")
    assert me.status_code == status.HTTP_200_OK
    assert me.json()["email"] == user.email


def test_access_token_authenticates_protected_endpoints():
    user = UserFactory()
    access = (
        APIClient()
        .post(
            "/api/auth/login/",
            {"email": user.email, "password": TEST_PASSWORD},
            format="json",
        )
        .json()["access"]
    )
    anonymous = APIClient()
    assert anonymous.get("/api/reports/").status_code == status.HTTP_401_UNAUTHORIZED
    authorized = APIClient()
    authorized.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert authorized.get("/api/reports/").status_code == status.HTTP_200_OK


def test_refresh_endpoint_issues_new_access_token():
    user = UserFactory()
    tokens = (
        APIClient()
        .post(
            "/api/auth/login/",
            {"email": user.email, "password": TEST_PASSWORD},
            format="json",
        )
        .json()
    )
    response = APIClient().post("/api/auth/refresh/", {"refresh": tokens["refresh"]}, format="json")
    assert response.status_code == status.HTTP_200_OK
    assert "access" in response.json()


def test_inactive_user_login_is_denied():
    user = UserFactory(is_active=False)
    response = APIClient().post(
        "/api/auth/login/",
        {"email": user.email, "password": TEST_PASSWORD},
        format="json",
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["code"] == "invalid_credentials"


def test_logout_blacklists_refresh_token_and_is_idempotent():
    user = UserFactory()
    tokens = (
        APIClient()
        .post(
            "/api/auth/login/",
            {"email": user.email, "password": TEST_PASSWORD},
            format="json",
        )
        .json()
    )
    client = APIClient()
    first = client.post("/api/auth/logout/", {"refresh": tokens["refresh"]}, format="json")
    assert first.status_code == status.HTTP_205_RESET_CONTENT
    # Idempotent: logging out an already-blacklisted or missing token still succeeds.
    second = client.post("/api/auth/logout/", {"refresh": tokens["refresh"]}, format="json")
    assert second.status_code == status.HTTP_205_RESET_CONTENT
    assert client.post("/api/auth/logout/", {}, format="json").status_code == 205
    # The blacklisted refresh token can no longer be exchanged.
    refreshed = APIClient().post(
        "/api/auth/refresh/", {"refresh": tokens["refresh"]}, format="json"
    )
    assert refreshed.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.parametrize("method", ["get", "post"])
def test_user_endpoints_are_admin_only(authenticated_client, method):
    payload = (
        {}
        if method == "get"
        else {
            "email": "new@example.com",
            "fullName": "New User",
            "role": User.Role.EMPLOYEE,
            "password": "Strong-Password-42!",
        }
    )
    response = getattr(authenticated_client, method)("/api/users/", payload, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_admin_can_create_user_and_password_is_hashed(admin_client):
    response = admin_client.post(
        "/api/users/",
        {
            "email": "created@example.com",
            "fullName": "Created User",
            "role": User.Role.APPROVER,
            "password": "Strong-Password-42!",
        },
        format="json",
    )
    assert response.status_code == status.HTTP_201_CREATED
    user = User.objects.get(email="created@example.com")
    assert user.check_password("Strong-Password-42!")
    assert user.password != "Strong-Password-42!"


def test_normal_user_lists_only_own_reports_and_expenses(authenticated_client, user):
    own = ReportFactory(user=user)
    own_expense = ExpenseFactory(report=own)
    ReportFactory(user=UserFactory())
    response = authenticated_client.get("/api/reports/")
    assert [item["id"] for item in response.json()["results"]] == [str(own.id)]
    response = authenticated_client.get("/api/expenses/")
    assert [item["id"] for item in response.json()["results"]] == [str(own_expense.id)]


def test_assigned_approver_sees_report_and_its_expenses():
    owner = UserFactory()
    report = ReportFactory(user=owner)
    expense = ExpenseFactory(report=report)
    approver = ApproverFactory()
    ApprovalStepFactory(report=report, approver=approver)
    client = APIClient()
    client.force_authenticate(user=approver)
    assert client.get(f"/api/reports/{report.id}/").status_code == status.HTTP_200_OK
    assert client.get(f"/api/expenses/{expense.id}/").status_code == status.HTTP_200_OK


def test_unassigned_foreign_resources_are_404(authenticated_client):
    report = ReportFactory(user=UserFactory())
    expense = ExpenseFactory(report=report)
    assert (
        authenticated_client.get(f"/api/reports/{report.id}/").status_code
        == status.HTTP_404_NOT_FOUND
    )
    assert (
        authenticated_client.get(f"/api/expenses/{expense.id}/").status_code
        == status.HTTP_404_NOT_FOUND
    )


def test_normal_user_can_read_but_cannot_manage_warning_rules(authenticated_client):
    assert authenticated_client.get("/api/warning-rules/").status_code == status.HTTP_200_OK
    assert (
        authenticated_client.post(
            "/api/warning-rules/",
            {
                "name": "Rule",
                "categoryId": None,
                "currency": "EUR",
                "thresholdMinor": "100",
                "severity": "WARNING",
                "message": "Warning",
                "active": True,
            },
            format="json",
        ).status_code
        == status.HTTP_403_FORBIDDEN
    )


def test_normal_user_cannot_configure_approval_steps(authenticated_client, report):
    response = authenticated_client.post(
        f"/api/reports/{report.id}/approval-steps/",
        {"approverId": str(ApproverFactory().id), "stepOrder": 1},
        format="json",
    )
    assert response.status_code in {
        status.HTTP_403_FORBIDDEN,
        status.HTTP_409_CONFLICT,
    }


def test_admin_can_configure_warning_rules_and_approval_steps(admin_client, report):
    rule = admin_client.post(
        "/api/warning-rules/",
        {
            "name": "Admin rule",
            "categoryId": None,
            "currency": "EUR",
            "thresholdMinor": "100",
            "severity": "WARNING",
            "message": "Review",
            "active": True,
        },
        format="json",
    )
    assert rule.status_code == status.HTTP_201_CREATED
    step = admin_client.post(
        f"/api/reports/{report.id}/approval-steps/",
        {"approverId": str(ApproverFactory().id), "stepOrder": 1},
        format="json",
    )
    assert step.status_code == status.HTTP_201_CREATED
