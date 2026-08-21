import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

if SECRET_KEY == "unsafe-development-key":  # noqa: F405
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production")
if not ALLOWED_HOSTS:  # noqa: F405
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production")
if not os.environ.get("DATABASE_URL"):
    raise ImproperlyConfigured("DATABASE_URL must be set in production")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
