"""WebAuthn / passkey registration and authentication."""

from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from apps.accounts.models import User, WebAuthnCredential

REG_CHALLENGE_KEY = "webauthn_reg_challenge"
AUTH_CHALLENGE_KEY = "webauthn_auth_challenge"


class WebAuthnError(Exception):
    pass


def _user_handle(user: User) -> bytes:
    return f"docnest-user-{user.pk}".encode()


def registration_options(request: HttpRequest, user: User) -> dict[str, Any]:
    options = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        rp_name=settings.WEBAUTHN_RP_NAME,
        user_id=_user_handle(user),
        user_name=user.get_username(),
        user_display_name=user.get_username(),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(c.credential_id)) for c in user.webauthn_credentials.all()
        ],
    )
    request.session[REG_CHALLENGE_KEY] = options.challenge.hex()
    return json.loads(options_to_json(options))


def register(request: HttpRequest, user: User, credential: dict[str, Any], name: str) -> WebAuthnCredential:
    challenge = request.session.pop(REG_CHALLENGE_KEY, None)
    if not challenge:
        raise WebAuthnError("registration not started")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=bytes.fromhex(challenge),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGIN,
        )
    except Exception as exc:
        raise WebAuthnError("security key could not be verified") from exc
    transports = credential.get("response", {}).get("transports") or []
    return WebAuthnCredential.objects.create(
        user=user,
        name=(name or "Security key")[:100],
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=[t for t in transports if isinstance(t, str)][:10],
    )


def authentication_options(request: HttpRequest, user: User) -> dict[str, Any]:
    options = generate_authentication_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(c.credential_id)) for c in user.webauthn_credentials.all()
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session[AUTH_CHALLENGE_KEY] = options.challenge.hex()
    return json.loads(options_to_json(options))


def authenticate(request: HttpRequest, user: User, credential: dict[str, Any]) -> WebAuthnCredential:
    challenge = request.session.pop(AUTH_CHALLENGE_KEY, None)
    if not challenge:
        raise WebAuthnError("authentication not started")
    try:
        raw_id = base64url_to_bytes(str(credential.get("rawId") or credential.get("id") or ""))
    except Exception as exc:
        raise WebAuthnError("invalid credential") from exc
    stored = user.webauthn_credentials.filter(credential_id=raw_id).first()
    if stored is None:
        raise WebAuthnError("unknown security key")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=bytes.fromhex(challenge),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGIN,
            credential_public_key=bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
        )
    except Exception as exc:
        raise WebAuthnError("security key could not be verified") from exc
    stored.sign_count = verified.new_sign_count
    stored.last_used_at = timezone.now()
    stored.save(update_fields=["sign_count", "last_used_at"])
    return stored
