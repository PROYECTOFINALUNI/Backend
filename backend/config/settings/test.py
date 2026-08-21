import os

from .base import *  # noqa: F403
from .base import _postgres_database

SECRET_KEY = "test-secret-key-for-jwt-signing-not-for-production-use"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MIDDLEWARE = [  # noqa: F405
    middleware
    for middleware in MIDDLEWARE  # noqa: F405
    if middleware != "whitenoise.middleware.WhiteNoiseMiddleware"
]
DATABASES = {
    "default": _postgres_database(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/expenses_test",
        )
    )
}
