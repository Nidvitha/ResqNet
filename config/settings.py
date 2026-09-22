"""
Django settings for the ResQNet project.

ResQNet is a Django monolith. Every environment-specific or secret value is read
from the `.env` file via django-environ, so no credentials ever live in source
control (Section 26 - Security Requirements).
"""

import sys
from pathlib import Path

import environ

#: True while `manage.py test` is running. Used only to relax rate limiting -
#: DRF keeps throttle history in a process-wide cache, so hundreds of test
#: requests from one "client" would otherwise trip the limiter and mask real
#: failures. Throttling itself is tested explicitly with @override_settings.
TESTING = "test" in sys.argv

# BASE_DIR points at `resQnet/` - the folder that contains manage.py.
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    MAX_UPLOAD_SIZE_MB=(int, 5),
    SECURE_SSL_REDIRECT=(bool, False),
    SESSION_COOKIE_SECURE=(bool, False),
    CSRF_COOKIE_SECURE=(bool, False),
)

# Read `.env` if it exists. In production the variables come from the real
# environment instead, so a missing file is not an error.
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(env_file)

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
]

# One app per responsibility, exactly as laid out in Section 7.
LOCAL_APPS = [
    "accounts",       # users, roles, officer profiles, authentication
    "reports",        # citizen damage reports + evidence photos
    "triage",         # rule-based scoring and severity classification
    "dispatch",       # automatic field-officer assignment
    "inspections",    # field verification of reported damage
    "compensation",   # relief matrix and itemised compensation
    "approvals",      # admin approve / reject / payout tracking
    "audit",          # append-only audit trail
    "analytics",      # aggregated read-only statistics
    "satellite",      # multi-source and satellite intelligence
    "web",            # server-rendered public site + role portals
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# DATABASE_URL drives everything, so moving from SQLite to PostgreSQL is a
# one-line change in `.env` with no code edits (Section 30).
DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# The custom user model must be declared before the very first migration.
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static and media files
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# File upload limits (Section 12 - backend must never trust the frontend)
# ---------------------------------------------------------------------------
MAX_UPLOAD_SIZE_MB = env("MAX_UPLOAD_SIZE_MB")
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]
ALLOWED_IMAGE_CONTENT_TYPES = ["image/jpeg", "image/png", "image/webp"]
MAX_IMAGE_DIMENSION = 6000  # pixels on the longest side

# Reject oversized uploads before they are fully buffered into memory.
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_BYTES
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_BYTES

# ---------------------------------------------------------------------------
# Firebase authentication
# ---------------------------------------------------------------------------
# The web values below are NOT secrets - they ship in every Firebase web app and
# identify the project. Security comes from this server verifying ID tokens, not
# from hiding them.
FIREBASE_API_KEY = env("FIREBASE_API_KEY", default="")
FIREBASE_AUTH_DOMAIN = env("FIREBASE_AUTH_DOMAIN", default="")
FIREBASE_PROJECT_ID = env("FIREBASE_PROJECT_ID", default="")
FIREBASE_APP_ID = env("FIREBASE_APP_ID", default="")
FIREBASE_STORAGE_BUCKET = env("FIREBASE_STORAGE_BUCKET", default="")
FIREBASE_MESSAGING_SENDER_ID = env("FIREBASE_MESSAGING_SENDER_ID", default="")

# The service account IS a secret. Supply a path to the JSON file, or the JSON
# itself for hosts that only provide environment variables. Never commit either.
FIREBASE_CREDENTIALS_FILE = env("FIREBASE_CREDENTIALS_FILE", default="")
FIREBASE_CREDENTIALS_JSON = env("FIREBASE_CREDENTIALS_JSON", default="")

# Providers accepted for each role. Google sign-in is for citizens only: officer
# and administrator accounts are issued by an administrator and must not be
# reachable through whatever personal Google account shares the address.
FIREBASE_CITIZEN_PROVIDERS = ["password", "google.com"]
FIREBASE_STAFF_PROVIDERS = ["password"]

# ---------------------------------------------------------------------------
# Satellite intelligence
# ---------------------------------------------------------------------------
# "demo" is deterministic and local-only. Swap to a real inference adapter via
# services.py when production infrastructure is available.
SATELLITE_INFERENCE_BACKEND = env("SATELLITE_INFERENCE_BACKEND", default="demo")

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    # Session auth serves the browser portals; token auth serves the offline
    # sync clients, which cannot rely on a session cookie surviving a reconnect.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    # Deny by default; every endpoint opens up explicitly.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    # Each scope is separate on purpose: a burst of sign-ins must never exhaust
    # the allowance for people creating accounts, or for a citizen filing a
    # report during an event. The login scope counts FAILED attempts only -
    # see accounts/throttling.py.
    "DEFAULT_THROTTLE_RATES": {
        "login": None if TESTING else "10/min",        # failed password attempts per IP
        "register": None if TESTING else "20/hour",    # new accounts per IP
        "report_create": None if TESTING else "60/hour",
        "sync": None if TESTING else "120/hour",
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

# ---------------------------------------------------------------------------
# CORS - only needed if a portal is ever served from a separate origin.
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = True

# ---------------------------------------------------------------------------
# Production hardening - all off in development, all on via `.env` in production
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT")
SESSION_COOKIE_SECURE = env("SESSION_COOKIE_SECURE")
CSRF_COOKIE_SECURE = env("CSRF_COOKIE_SECURE")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # the fetch() layer must read this token

if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "resqnet": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
