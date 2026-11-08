import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-secret")
DEBUG = os.environ.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "payments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

# PostgreSQL, not SQLite: select_for_update and real concurrent transactions
# are the whole point, and SQLite ignores row locks.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "payments"),
        "USER": os.environ.get("DB_USER", "payments"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "payments"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "55433"),
    }
}

# Shared secret with the payment provider. Callbacks are signed with it.
PROVIDER_SECRET = os.environ.get("PROVIDER_SECRET", "dev-provider-secret")
# The point-of-sale system orders are sent to after payment.
POS_URL = os.environ.get("POS_URL", "http://localhost:9000")
# Seconds to wait for either outside system.
OUTSIDE_TIMEOUT = 10
# The payment provider's API, used for refunds.
PROVIDER_URL = os.environ.get("PROVIDER_URL", "http://localhost:9001")

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_EAGER", "0") == "1"
