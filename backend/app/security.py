import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Optional
from uuid import UUID


TOKEN_VERSION = "v2"
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days


def load_or_create_session_secret(
    env_secret: Optional[str],
    secret_file: str,
) -> str:
    if env_secret:
        return env_secret

    path = Path(secret_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret

    secret = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secret)
    return secret


def _compute_signature(user_id: UUID, issued_at: int, secret: str) -> str:
    message = f"{user_id}:{issued_at}".encode("utf-8")
    digest = hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_session_token(user_id: UUID, secret: str) -> str:
    issued_at = int(time.time())
    sig = _compute_signature(user_id, issued_at, secret)
    return f"{TOKEN_VERSION}.{issued_at}.{sig}"


def verify_session_token(
    user_id: UUID,
    token: Optional[str],
    secret: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    if not token or not isinstance(token, str):
        return False

    parts = token.split(".")
    if len(parts) != 3:
        return False

    version, issued_at_str, signature = parts
    if version != TOKEN_VERSION:
        return False

    try:
        issued_at = int(issued_at_str)
    except ValueError:
        return False

    # Check expiration
    if time.time() - issued_at > max_age_seconds:
        return False

    expected_signature = _compute_signature(user_id, issued_at, secret)
    return hmac.compare_digest(signature, expected_signature)
