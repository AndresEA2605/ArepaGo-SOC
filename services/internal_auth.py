"""Short-lived HMAC-signed identity tokens for internal service calls.

The gateway signs the authenticated user's id and role; every internal service
verifies the signature before trusting the identity. The shared secret never
travels in the request, so a client (or any process without the secret) cannot
forge an identity by setting headers.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

IDENTITY_HEADER = "X-ArepaGo-Identity"
DEFAULT_TTL_SECONDS = 120
MIN_SECRET_LENGTH = 32
MAX_TOKEN_LENGTH = 4096
ROLES = {"admin", "customer"}
_secret_cache = None


def load_secret():
    """Load the shared secret from the environment or a mounted secret file."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    value = os.environ.get("INTERNAL_AUTH_SECRET")
    path = os.environ.get("INTERNAL_AUTH_SECRET_FILE")
    if path:
        value = Path(path).read_text(encoding="utf-8").strip()
    if not value or len(value) < MIN_SECRET_LENGTH:
        raise RuntimeError("INTERNAL_AUTH_SECRET o INTERNAL_AUTH_SECRET_FILE debe definir "
                           f"al menos {MIN_SECRET_LENGTH} caracteres")
    _secret_cache = value.encode("utf-8")
    return _secret_cache


def _b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(secret, encoded_payload):
    return hmac.new(secret, encoded_payload.encode("ascii"), hashlib.sha256).digest()


def sign_identity(user_id, role, secret=None, ttl=DEFAULT_TTL_SECONDS, now=None):
    """Return a compact ``payload.signature`` token for an authenticated user."""
    if role not in ROLES:
        raise ValueError("rol inválido")
    secret = secret or load_secret()
    issued_at = int(now if now is not None else time.time())
    payload = json.dumps({"sub": str(uuid.UUID(str(user_id))), "role": role, "exp": issued_at + ttl},
                         separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64encode(payload)
    return f"{encoded}.{_b64encode(_sign(secret, encoded))}"


def verify_identity(token, secret=None, now=None):
    """Return ``{"id": ..., "role": ...}`` or None when the token is invalid."""
    if not token or len(str(token)) > MAX_TOKEN_LENGTH:
        return None
    try:
        encoded, signature = str(token).split(".", 1)
        secret = secret or load_secret()
        if not hmac.compare_digest(_b64decode(signature), _sign(secret, encoded)):
            return None
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) < int(now if now is not None else time.time()):
            return None
        role = payload.get("role")
        if role not in ROLES:
            return None
        return {"id": str(uuid.UUID(payload["sub"])), "role": role}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError,
            UnicodeDecodeError, binascii.Error):
        return None
