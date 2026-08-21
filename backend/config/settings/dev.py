from .base import *  # noqa: F403
from .base import _env_bool

DEBUG = _env_bool("DEBUG", True)
if not ALLOWED_HOSTS:  # noqa: F405
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
