import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)
from webauthn.helpers import base64url_to_bytes

from app.config import settings

_hasher = PasswordHasher()


# ---------- Password ----------

def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


# Checked against when the username matches nobody, so a wrong name costs
# what a wrong password does and the answer's timing does not say which of
# the two was wrong.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def verify_password(plain: str, hashed: str | None) -> bool:
    """Blocking (argon2 is deliberately slow): call it in a thread."""
    if hashed is None:
        try:
            _hasher.verify(_DUMMY_HASH, plain)
        except VerificationError:
            pass
        return False
    try:
        return _hasher.verify(hashed, plain)
    except (VerificationError, InvalidHashError):
        # VerificationError covers VerifyMismatchError (the normal "wrong
        # password" case) along with other verification failures.
        # InvalidHashError covers a malformed/corrupted stored hash --
        # extremely unlikely in practice, but without this an odd hash
        # value would raise straight through this function as an
        # unhandled 500 instead of a clean "invalid credentials" response.
        return False


# ---------- Session ----------

def new_session_token() -> tuple[str, str]:
    """Returns (token_for_cookie, hash_for_db)."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utc_now_iso() -> str:
    """Now, in the format session_expiry writes, so the two compare as text."""
    return datetime.now(timezone.utc).isoformat()


def session_expiry() -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.session_lifetime_hours)
    return exp.isoformat()


# ---------- TOTP ----------

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=username, issuer_name=settings.webauthn_rp_name
    )


def totp_step(secret: str, code: str) -> int | None:
    """The time step `code` belongs to, or None when it matches none.

    A step either side of now is accepted (a phone clock a few seconds out,
    a code typed as it rolled over) -- the window pyotp's verify(...,
    valid_window=1) gave. The step, rather than a yes, is what lets the
    caller refuse a code already used: without it one code could sign in
    again for as long as its window lasted, about a minute and a half."""
    totp = pyotp.totp.TOTP(secret)
    now = totp.timecode(datetime.now(timezone.utc))
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(totp.generate_otp(step), str(code).strip()):
            return step
    return None



# ---------- WebAuthn (passkeys) ----------
# 2-step flow (begin/complete) for both registration and login.
# The "challenge" is stored in `webauthn_challenges` between the two calls.

def build_registration_options(user_id: int, username: str, existing_credential_ids: list[str]):
    options = generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=str(user_id).encode(),
        user_name=username,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
            for cid in existing_credential_ids
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    return options


def verify_registration(credential_json: dict, expected_challenge: str):
    return verify_registration_response(
        credential=credential_json,
        expected_challenge=base64url_to_bytes(expected_challenge),
        expected_origin=settings.webauthn_origin,
        expected_rp_id=settings.webauthn_rp_id,
    )


def build_authentication_options(allowed_credential_ids: list[str]):
    return generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
            for cid in allowed_credential_ids
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )


def verify_authentication(credential_json: dict, expected_challenge: str, public_key: str, sign_count: int):
    return verify_authentication_response(
        credential=credential_json,
        expected_challenge=base64url_to_bytes(expected_challenge),
        expected_origin=settings.webauthn_origin,
        expected_rp_id=settings.webauthn_rp_id,
        credential_public_key=base64url_to_bytes(public_key),
        credential_current_sign_count=sign_count,
    )
