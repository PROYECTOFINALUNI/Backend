import pytest
from rest_framework import status
from rest_framework.test import APIClient

from conftest import TEST_PASSWORD, AdminFactory, ApproverFactory, UserFactory
from users.models import User

pytestmark = pytest.mark.django_db


def test_admin_list_filters_role_active_and_search(admin_client):
    matching = ApproverFactory(email="needle@example.com", full_name="Match Name", is_active=True)
    ApproverFactory(email="inactive-needle@example.com", is_active=False)
    UserFactory(email="needle-employee@example.com")

    response = admin_client.get(
        "/api/users/",
        {"role": User.Role.APPROVER, "active": "true", "search": "match"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert [item["id"] for item in response.json()["results"]] == [str(matching.id)]
    assert set(response.json()["results"][0]) == {
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


def test_approver_lists_only_other_eligible_approvers():
    """Delegation needs a picker, so approvers get a directory instead of the full list."""
    caller = ApproverFactory()
    colleague = ApproverFactory()
    manager = AdminFactory()
    UserFactory(email="employee@example.com")
    ApproverFactory(email="retired@example.com", is_active=False)

    client = APIClient()
    client.force_authenticate(user=caller)
    response = client.get("/api/users/")

    assert response.status_code == status.HTTP_200_OK
    assert {item["id"] for item in response.json()["results"]} == {
        str(caller.id),
        str(colleague.id),
        str(manager.id),
    }


def test_approver_cannot_read_or_write_a_single_user():
    caller = ApproverFactory()
    other = UserFactory()

    client = APIClient()
    client.force_authenticate(user=caller)

    patched = client.patch(f"/api/users/{other.id}/", {"role": User.Role.ADMIN}, format="json")

    assert client.get(f"/api/users/{other.id}/").status_code == status.HTTP_403_FORBIDDEN
    assert patched.status_code == status.HTTP_403_FORBIDDEN


def test_admin_patch_user_including_password(admin_client):
    user = UserFactory()
    response = admin_client.patch(
        f"/api/users/{user.id}/",
        {
            "email": "changed@example.com",
            "fullName": "Changed Name",
            "role": User.Role.APPROVER,
            "active": False,
            "password": "Another-Strong-Password-42!",
        },
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    user.refresh_from_db()
    assert (user.email, user.full_name, user.role, user.is_active) == (
        "changed@example.com",
        "Changed Name",
        User.Role.APPROVER,
        False,
    )
    assert user.check_password("Another-Strong-Password-42!")


def test_admin_set_password_changes_credentials(admin_client):
    user = UserFactory()
    response = admin_client.post(
        f"/api/users/{user.id}/set-password/",
        {"password": "Replacement-Password-42!"},
        format="json",
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    user.refresh_from_db()
    assert user.check_password("Replacement-Password-42!")
    assert not user.check_password(TEST_PASSWORD)


@pytest.mark.parametrize(
    ("method", "payload", "field"),
    [
        ("patch", {"unknown": "value"}, "unknown"),
        ("patch", {"password": "short"}, "password"),
        ("set-password", {"password": "12345678"}, "password"),
    ],
)
def test_admin_user_writes_reject_invalid_fields_and_passwords(
    admin_client, method, payload, field
):
    user = UserFactory()
    if method == "set-password":
        response = admin_client.post(f"/api/users/{user.id}/set-password/", payload, format="json")
    else:
        response = admin_client.patch(f"/api/users/{user.id}/", payload, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert field in response.json()["fields"]
