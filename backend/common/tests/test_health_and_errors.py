from unittest.mock import patch

import pytest
from django.db.utils import OperationalError
from rest_framework import status

pytestmark = pytest.mark.django_db


def test_health_reports_database_success(api_client):
    response = api_client.get("/api/health/")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


def test_health_returns_controlled_database_failure(api_client):
    with patch("config.urls.connection.cursor", side_effect=OperationalError):
        response = api_client.get("/api/health/")
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "database_unavailable",
        "detail": "Database health check failed.",
        "fields": {},
    }


def test_unauthenticated_error_uses_standard_envelope(api_client):
    response = api_client.get("/api/reports/")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {
        "code": "not_authenticated",
        "detail": "Authentication credentials were not provided.",
        "fields": {},
    }
