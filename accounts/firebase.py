"""
Firebase authentication.

How this works
--------------
Firebase handles the *credential* — the password, or the Google account. The
browser signs in with Firebase, gets back a signed **ID token**, and sends that
token to ResQNet. This module verifies the signature against Google's public
keys and tells us who the person is.

The critical point: ResQNet never sees a password, and it never trusts anything
the client *claims*. The email, the user id, and the sign-in provider all come
out of a cryptographically verified token, not out of the request body.

Role assignment is deliberately ours, not Firebase's
----------------------------------------------------
A verified token proves identity, never authority. Anyone in the world can create
a Firebase account, so a token by itself must never grant the officer or
administrator role. Section 9 is explicit about this.

So:

* An email that already belongs to a ResQNet account signs in **as that account**,
  with whatever role an administrator gave it.
* An email we have never seen becomes a **citizen**. Always.

That is what makes officer provisioning an administrator action, exactly as it is
with password login.

Google sign-in is restricted to citizens
----------------------------------------
Per the requirement, `google.com` is accepted for citizens only. Officers and
administrators must use email and password, because their accounts are issued by
an administrator and should not be reachable through whatever personal Google
account happens to share the address.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("resqnet.firebase")

_init_lock = threading.Lock()
_app = None
_init_failed = False


class FirebaseError(Exception):
    """Raised when a token cannot be verified, or Firebase is not configured."""


#: Google's public keys for Firebase ID tokens, in JWKS form.
GOOGLE_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
)

_jwk_client = None


def has_admin_credentials() -> bool:
    """
    True when a service account is available.

    Required only for *privileged* operations — creating officer accounts and
    generating password links. Verifying a sign-in does not need it.
    """
    if getattr(settings, "FIREBASE_CREDENTIALS_JSON", ""):
        return True

    credentials_file = getattr(settings, "FIREBASE_CREDENTIALS_FILE", "")
    if not credentials_file:
        return False

    # Checks the file exists rather than merely that the setting is non-empty.
    # A path pointing at a missing file would otherwise report "ready" and fail
    # later, which reads as a broken site rather than a missing key.
    path = Path(credentials_file)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    return path.is_file()


def is_configured() -> bool:
    """
    True when this server can verify a Firebase sign-in.

    Two routes, and either is sufficient:

    * **Admin SDK** — when a service account is present.
    * **Public keys** — a Firebase ID token is an RS256 JWT signed by Google.
      Anyone can verify it against Google's published public keys; all that is
      needed is the project id, to check the token was minted for *this*
      project. No secret is involved, because verification uses a public key by
      definition.

    That second route is what lets citizens and officers sign in before a
    service account has been downloaded.
    """
    if has_admin_credentials():
        return True
    return bool(getattr(settings, "FIREBASE_PROJECT_ID", ""))


def _get_jwk_client():
    """Cached JWKS client. Google rotates these keys, so PyJWT caches and refetches."""
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient

        _jwk_client = PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwk_client


def _verify_with_public_keys(id_token: str) -> dict:
    """
    Verify an ID token against Google's public keys.

    This is exactly what the Admin SDK does internally. PyJWT checks the RS256
    signature, and the `audience` and `issuer` arguments pin the token to this
    Firebase project — without them a valid token from *any* Firebase project
    would be accepted, which would let anyone sign in as anyone.
    """
    import jwt

    project_id = getattr(settings, "FIREBASE_PROJECT_ID", "")
    if not project_id:
        raise FirebaseError("FIREBASE_PROJECT_ID is not set, so tokens cannot be verified.")

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=project_id,
            issuer=f"https://securetoken.google.com/{project_id}",
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except Exception as exc:  # noqa: BLE001 - PyJWT raises many distinct types
        logger.warning("Rejected Firebase token (public-key path): %s", exc)
        raise FirebaseError("Your sign-in could not be verified. Please try again.") from exc

    if not claims.get("sub"):
        raise FirebaseError("The sign-in token is missing a user id.")

    # The Admin SDK exposes the subject as `uid`; mirror that so both paths
    # return an identical shape to the caller.
    claims["uid"] = claims["sub"]
    return claims


def _get_app():
    """
    Initialise the Admin SDK once, lazily.

    Lazily because importing Django settings at module import time during tests
    (where Firebase is not configured) would be a needless failure, and once
    because the SDK raises if the default app is initialised twice.
    """
    global _app, _init_failed

    if _app is not None:
        return _app
    if _init_failed:
        raise FirebaseError("Firebase Admin SDK is not configured on this server.")

    with _init_lock:
        if _app is not None:
            return _app

        try:
            import firebase_admin
            from firebase_admin import credentials
        except ImportError as exc:
            _init_failed = True
            raise FirebaseError("firebase-admin is not installed on this server.") from exc

        # An already-initialised default app (e.g. after an autoreload) is reused
        # rather than fought with.
        try:
            _app = firebase_admin.get_app()
            return _app
        except ValueError:
            pass

        raw_json = getattr(settings, "FIREBASE_CREDENTIALS_JSON", "")
        credentials_file = getattr(settings, "FIREBASE_CREDENTIALS_FILE", "")

        try:
            if raw_json:
                # Useful on hosts that supply secrets as environment variables
                # rather than files.
                certificate = credentials.Certificate(json.loads(raw_json))
            elif credentials_file:
                # Resolved against BASE_DIR so `.env` can hold a tidy relative
                # path like `secrets/firebase-service-account.json` regardless of
                # the working directory the server was started from.
                path = Path(credentials_file)
                if not path.is_absolute():
                    path = Path(settings.BASE_DIR) / path
                if not path.is_file():
                    _init_failed = True
                    raise FirebaseError(
                        f"Firebase service account file not found at {path}. "
                        "Download it from the Firebase console: Project settings "
                        "-> Service accounts -> Generate new private key."
                    )
                certificate = credentials.Certificate(str(path))
            else:
                _init_failed = True
                raise FirebaseError(
                    "Firebase is not configured. Set FIREBASE_CREDENTIALS_FILE "
                    "(path to the service account JSON) in your .env file."
                )
            _app = firebase_admin.initialize_app(certificate)
            logger.info("Firebase Admin SDK initialised")
            return _app
        except FirebaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            _init_failed = True
            logger.exception("Failed to initialise the Firebase Admin SDK")
            raise FirebaseError(f"Could not initialise Firebase: {exc}") from exc


def verify_id_token(id_token: str) -> dict:
    """
    Verify a Firebase ID token and return the useful claims.

    Raises `FirebaseError` if the token is missing, malformed, expired, revoked,
    or signed by a different project. The SDK does the cryptographic work; this
    function's job is to turn its many exception types into one, and to reduce
    the claim set to the four things ResQNet actually uses.
    """
    if not id_token or not isinstance(id_token, str):
        raise FirebaseError("No Firebase ID token was supplied.")

    if has_admin_credentials():
        # Preferred when available: the SDK additionally supports check_revoked,
        # which catches a token from a session an administrator has terminated.
        app = _get_app()
        try:
            from firebase_admin import auth as firebase_auth

            claims = firebase_auth.verify_id_token(id_token, app=app, check_revoked=True)
        except Exception as exc:  # noqa: BLE001 - the SDK raises many distinct types
            logger.warning("Rejected Firebase token: %s", exc)
            raise FirebaseError("Your sign-in could not be verified. Please try again.") from exc
    else:
        # No service account: verify against Google's public keys instead. Just
        # as sound cryptographically — revocation checking is what is given up.
        claims = _verify_with_public_keys(id_token)

    email = (claims.get("email") or "").strip().lower()
    if not email:
        # Every provider we enable supplies an email. Without one there is no way
        # to link the identity to a ResQNet account.
        raise FirebaseError(
            "Your sign-in did not provide an email address, which ResQNet needs "
            "to link your account."
        )

    return {
        "uid": claims.get("uid") or claims.get("user_id") or "",
        "email": email,
        "email_verified": bool(claims.get("email_verified")),
        "name": (claims.get("name") or "").strip(),
        # sign_in_provider is "password", "google.com", and so on. This is the
        # claim that enforces "Google for citizens only".
        "provider": (claims.get("firebase", {}) or {}).get("sign_in_provider", ""),
    }


def provision_user(email: str, display_name: str = "") -> dict:
    """
    Ensure a Firebase account exists for a staff email, and return a link they
    can use to set their own password.

    Officers cannot self-register — that would let anyone claim the role — so
    without this an administrator has to create every officer twice: once in
    ResQNet and once by hand in the Firebase console. This closes that gap so
    provisioning is a single action.

    The administrator never sets the officer's password. A one-time link is
    generated instead, so the credential is only ever known to the officer.

    Returns `{"uid", "created", "reset_link"}`. Raises `FirebaseError` on
    failure, which the caller treats as non-fatal: the ResQNet account is still
    valid and the Firebase side can be retried.
    """
    app = _get_app()

    try:
        from firebase_admin import auth as firebase_auth
    except ImportError as exc:
        raise FirebaseError("firebase-admin is not installed on this server.") from exc

    email = (email or "").strip().lower()
    if not email:
        raise FirebaseError("An email address is required to provision a Firebase account.")

    created = False
    try:
        record = firebase_auth.get_user_by_email(email, app=app)
    except Exception:  # noqa: BLE001 - UserNotFoundError and transport errors alike
        try:
            record = firebase_auth.create_user(
                email=email,
                display_name=display_name or None,
                email_verified=False,
                app=app,
            )
            created = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not create Firebase user for %s", email)
            raise FirebaseError(f"Could not create the Firebase account: {exc}") from exc

    # A password *reset* link doubles as a "set your password" link for an
    # account created without one.
    try:
        reset_link = firebase_auth.generate_password_reset_link(email, app=app)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not generate a password link for %s: %s", email, exc)
        reset_link = ""

    return {"uid": record.uid, "created": created, "reset_link": reset_link}


#: Public endpoint that reports which identity providers a project has enabled.
PROJECT_CONFIG_URL = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/getProjectConfig"

#: Cache key and lifetime for the provider probe. Short enough that enabling
#: Google in the console takes effect quickly, long enough that a busy login
#: page is not calling Google on every render.
_PROVIDER_CACHE_KEY = "resqnet.firebase.providers"
_PROVIDER_CACHE_SECONDS = 300


def google_provider_enabled() -> bool | None:
    """
    Is Google sign-in actually enabled in the Firebase project?

    Returns True, False, or **None** when it could not be determined.

    Worth the round trip: without it, a citizen clicks "Continue with Google",
    a popup opens, and Firebase returns `auth/operation-not-allowed` — which
    reads as a broken site rather than an unconfigured provider. Knowing in
    advance lets the page explain itself instead.

    `None` is deliberately distinct from `False`: if the probe fails we must not
    disable a button that might work perfectly well.
    """
    from django.core.cache import cache

    cached = cache.get(_PROVIDER_CACHE_KEY)
    if cached is not None:
        return cached.get("google")

    api_key = getattr(settings, "FIREBASE_API_KEY", "")
    if not api_key:
        return None

    try:
        import requests

        response = requests.get(PROJECT_CONFIG_URL, params={"key": api_key}, timeout=5)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - a failed probe must never block sign-in
        logger.info("Could not probe Firebase providers: %s", exc)
        return None

    providers = payload.get("idpConfig") or []
    enabled = any(
        entry.get("provider") == "google.com" and entry.get("enabled")
        for entry in providers
    )

    cache.set(_PROVIDER_CACHE_KEY, {"google": enabled}, _PROVIDER_CACHE_SECONDS)
    return enabled


def public_config() -> dict:
    """
    The Firebase *web* config for the browser.

    These values are not secrets — they identify the project and ship in every
    Firebase web app. Access is controlled by Firebase security rules and by this
    server verifying tokens, never by hiding the API key.
    """
    return {
        "apiKey": getattr(settings, "FIREBASE_API_KEY", ""),
        "authDomain": getattr(settings, "FIREBASE_AUTH_DOMAIN", ""),
        "projectId": getattr(settings, "FIREBASE_PROJECT_ID", ""),
        "appId": getattr(settings, "FIREBASE_APP_ID", ""),
        "storageBucket": getattr(settings, "FIREBASE_STORAGE_BUCKET", ""),
        "messagingSenderId": getattr(settings, "FIREBASE_MESSAGING_SENDER_ID", ""),
    }


def is_web_configured() -> bool:
    """True when the browser has enough config to talk to Firebase at all."""
    config = public_config()
    return bool(config["apiKey"] and config["authDomain"] and config["projectId"])
