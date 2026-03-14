import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional
from uuid import UUID


TOKEN_VERSION = "v1"


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


def _signature_for_user(user_id: UUID, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        str(user_id).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_session_token(user_id: UUID, secret: str) -> str:
    return f"{TOKEN_VERSION}.{_signature_for_user(user_id, secret)}"


def verify_session_token(user_id: UUID, token: Optional[str], secret: str) -> bool:
    if not token or not isinstance(token, str):
        return False
    version, separator, signature = token.partition(".")
    if version != TOKEN_VERSION or not separator or not signature:
        return False
    expected_signature = _signature_for_user(user_id, secret)
    return hmac.compare_digest(signature, expected_signature)
