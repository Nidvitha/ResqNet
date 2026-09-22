"""
Account services - the rules for turning a verified Firebase identity into a
ResQNet session.

The whole security argument for Firebase login rests on one separation:

    Firebase proves **who you are**.   ResQNet decides **what you may do**.

A valid Firebase token means someone controls that email address. It says nothing
about whether they should be able to inspect properties or approve relief
payments — anyone can create a Firebase account in seconds.

So `resolve_firebase_user()` never reads a role from the token. It matches the
verified email against accounts an administrator has already created, and any
address it does not recognise becomes a citizen.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from audit.models import AuditAction
from audit.services import record_event

from .models import Role, User

logger = logging.getLogger("resqnet.accounts")


class FirebaseAuthDenied(Exception):
    """Identity verified, but this account may not sign in this way."""


def _allowed_providers(role: str) -> list[str]:
    if role == Role.CITIZEN:
        return getattr(settings, "FIREBASE_CITIZEN_PROVIDERS", ["password", "google.com"])
    return getattr(settings, "FIREBASE_STAFF_PROVIDERS", ["password"])


def _readable_provider(provider: str) -> str:
    return {"password": "email and password", "google.com": "Google"}.get(provider, provider)


@transaction.atomic
def resolve_firebase_user(claims: dict, *, request=None) -> tuple[User, bool]:
    """
    Map verified Firebase claims to a ResQNet account.

    Returns `(user, created)`.

    Matching order matters:

    1. **By `firebase_uid`** — the durable identifier. Survives the user changing
       their email address at the provider, which would otherwise orphan their
       reports and case history.
    2. **By email** — links a Firebase sign-in to an account an administrator
       created in advance, which is how officers get in.
    3. **Otherwise create a citizen.** Never any other role.
    """
    uid = claims["uid"]
    email = claims["email"]
    provider = claims.get("provider", "")

    user = User.objects.filter(firebase_uid=uid).first() if uid else None
    created = False

    if user is None:
        user = User.objects.filter(email__iexact=email).first()

    if user is None:
        # Unknown address. A brand-new account, and it is a citizen by
        # construction - there is no code path here that assigns another role.
        user = User.objects.create(
            username=_unique_username(email),
            email=email,
            first_name=_first_name(claims, email),
            last_name=_last_name(claims),
            role=Role.CITIZEN,
            firebase_uid=uid or None,
            auth_provider=provider,
            is_active=True,
        )
        # Firebase holds the credential, so there is no usable Django password.
        # This is not a blank password - it can never match any input.
        user.set_unusable_password()
        user.save(update_fields=["password"])
        created = True

        record_event(
            entity=user,
            action=AuditAction.USER_REGISTERED,
            user=user,
            new_state=user.role,
            metadata={"via": "firebase", "provider": provider, "email": email},
            request=request,
        )
        logger.info("Created citizen %s from Firebase sign-in (%s)", user.username, provider)

    # --- Provider restriction: Google is for citizens only ------------------
    allowed = _allowed_providers(user.role)
    if provider and provider not in allowed:
        raise FirebaseAuthDenied(
            f"{_readable_provider(provider).capitalize()} sign-in is not available for "
            f"{user.get_role_display().lower()} accounts. "
            "Please sign in with your email address and password."
        )

    if not user.is_active:
        raise FirebaseAuthDenied("This account has been deactivated. Contact your administrator.")

    # --- Keep the link current ---------------------------------------------
    changed = []
    if uid and user.firebase_uid != uid:
        # First Firebase sign-in for a pre-provisioned account, or a re-link.
        if User.objects.filter(firebase_uid=uid).exclude(pk=user.pk).exists():
            raise FirebaseAuthDenied(
                "This Firebase identity is already linked to a different ResQNet account."
            )
        user.firebase_uid = uid
        changed.append("firebase_uid")
    if user.auth_provider != provider:
        user.auth_provider = provider
        changed.append("auth_provider")
    if not user.email and email:
        user.email = email
        changed.append("email")
    if changed:
        user.save(update_fields=changed + ["updated_at"])

    return user, created


def _unique_username(email: str) -> str:
    """
    Derive a readable username from an email, then make it unique.

    Firebase identifies people by uid and email; ResQNet still shows a username
    throughout the interface, so one is generated rather than exposing the raw
    uid to officers and administrators.
    """
    base = "".join(ch for ch in email.split("@")[0].lower() if ch.isalnum() or ch in "._-")
    base = (base or "user")[:140]

    if not User.objects.filter(username=base).exists():
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}{suffix}"
        if not User.objects.filter(username=candidate).exists():
            return candidate
    # Effectively unreachable, but a collision must never crash a sign-in.
    from uuid import uuid4

    return f"{base[:100]}-{uuid4().hex[:8]}"


def _first_name(claims: dict, email: str) -> str:
    name = (claims.get("name") or "").strip()
    if name:
        return name.split()[0][:150]
    return email.split("@")[0][:150].title()


def _last_name(claims: dict) -> str:
    parts = (claims.get("name") or "").strip().split()
    return " ".join(parts[1:])[:150] if len(parts) > 1 else ""
