"""Session tokens — JWT (HS256) over the user UUID.

We issue and verify HS256 JWTs with `sub = user_id`, `iat`, and `exp`
claims. The shared secret is loaded from `SESSION_SECRET` env var or
`SESSION_SECRET_FILE` on disk (with `O_CREAT | 0o600` mode for fresh
installs).

The frontend stores the opaque token blob in localStorage and resends
it via Socket.IO handshake `auth`; on reconnect we verify the
signature and the `sub` claim must match the claimed `user_id`.
"""

import os
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import jwt

JWT_ALGORITHM = "HS256"
DEFAULT_MAX_AGE_SECONDS = int(timedelta(days=7).total_seconds())


def load_or_create_session_secret(
    env_secret: str | None,
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


def issue_session_token(
    user_id: UUID,
    secret: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> str:
    """Issue an HS256 JWT containing `sub=user_id`, `iat`, `exp`.

    Uses integer-seconds for `iat` / `exp` to match RFC 7519 §2 NumericDate
    expectations and stay free of pyjwt's `datetime.utcnow` deprecation.
    """
    import time

    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + max_age_seconds,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def verify_session_token(
    user_id: UUID,
    token: str | None,
    secret: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    """Return True iff `token` is a valid HS256 JWT issued for `user_id`
    that has not yet expired (`exp` claim).

    `max_age_seconds` is accepted for backwards compatibility with the
    previous HMAC API but is unused — the token's own `exp` claim is
    authoritative. Tokens are issued with `exp = now + DEFAULT_MAX_AGE_SECONDS`
    in `issue_session_token`, so changing this argument on verify has no
    effect (kept for call-site stability).
    """
    if not token or not isinstance(token, str):
        return False

    try:
        decoded = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return False
    except jwt.InvalidTokenError:
        return False

    return decoded.get("sub") == str(user_id)
