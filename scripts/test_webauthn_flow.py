"""
Simulates a hardware authenticator (soft-webauthn) to validate the passkey
registration and login flow end-to-end against the real server -- without
this, WebAuthn can only be tested manually in a browser.
"""
import base64
import os

import requests
from soft_webauthn import SoftWebauthnDevice

# Must match the server's APP_WEBAUTHN_ORIGIN: WebAuthn verification compares
# the origin the browser (here, this script) claims against the one the server
# was configured with, so a mismatched port fails the handshake rather than
# the connection. Override with WEBAUTHN_TEST_BASE to point at another port.
BASE = os.environ.get("WEBAUTHN_TEST_BASE", "http://localhost:8000")
session = requests.Session()


def b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def decode_creation_options(options: dict) -> dict:
    """Our endpoint returns challenge/ids as base64url strings (the format
    PublicKeyCredential.parseCreationOptionsFromJSON/parseRequestOptionsFromJSON
    expect); soft-webauthn wants real bytes (as a browser would deliver via
    ArrayBuffer)."""
    options = dict(options)
    options["challenge"] = b64url_decode(options["challenge"])
    options["user"] = dict(options["user"])
    options["user"]["id"] = b64url_decode(options["user"]["id"])
    for cred in options.get("excludeCredentials", []):
        cred["id"] = b64url_decode(cred["id"])
    return options


def decode_request_options(options: dict) -> dict:
    options = dict(options)
    options["challenge"] = b64url_decode(options["challenge"])
    for cred in options.get("allowCredentials", []):
        cred["id"] = b64url_decode(cred["id"])
    return options


def encode_credential_response(cred: dict) -> dict:
    """Converts soft-webauthn's (bytes) output into the base64url JSON
    format a real browser would send -- what our endpoints expect."""
    resp = cred["response"]
    if "attestationObject" in resp:
        response = {
            "clientDataJSON": b64url_encode(resp["clientDataJSON"]),
            "attestationObject": b64url_encode(resp["attestationObject"]),
        }
    else:
        response = {
            "clientDataJSON": b64url_encode(resp["clientDataJSON"]),
            "authenticatorData": b64url_encode(resp["authenticatorData"]),
            "signature": b64url_encode(resp["signature"]),
            "userHandle": b64url_encode(resp["userHandle"]) if resp.get("userHandle") else None,
        }
    return {
        "id": b64url_encode(cred["rawId"]),
        "rawId": b64url_encode(cred["rawId"]),
        "type": cred["type"],
        "response": response,
    }


# ---------- password login, so we can register the passkey ----------
resp = session.post(f"{BASE}/login", data={"username": "admin", "password": "password12345"})
assert resp.status_code in (200, 302), f"login failed: {resp.status_code}"
print("password login: OK")

device = SoftWebauthnDevice()

# ---------- Registration ----------
resp = session.post(f"{BASE}/settings/passkey/register/begin")
assert resp.status_code == 200, resp.text
options = resp.json()
print("register/begin: OK", {k: options[k] for k in ("rp", "user") if k in options})

attestation = device.create({"publicKey": decode_creation_options(options)}, BASE)

resp = session.post(
    f"{BASE}/settings/passkey/register/complete?nickname=SoftTest",
    json=encode_credential_response(attestation),
)
assert resp.status_code == 200, resp.text
print("register/complete: OK ->", resp.json())

# ---------- Logout and login via passkey ----------
session.post(f"{BASE}/logout")
session.cookies.clear()

resp = session.post(f"{BASE}/login/passkey/begin")
assert resp.status_code == 200, resp.text
auth_options = resp.json()
print("passkey/begin: OK")

assertion = device.get({"publicKey": decode_request_options(auth_options)}, BASE)

resp = session.post(f"{BASE}/login/passkey/complete", json=encode_credential_response(assertion))
assert resp.status_code == 200, resp.text
print("passkey/complete: OK ->", resp.json())

# confirms that the session created by the passkey actually authenticates
resp = session.get(f"{BASE}/settings/security")
assert resp.status_code == 200, f"post-passkey session did not authenticate: {resp.status_code}"
print("session validated after passkey login: OK")

print("\n✅ FULL WEBAUTHN HANDSHAKE VALIDATED END TO END")
