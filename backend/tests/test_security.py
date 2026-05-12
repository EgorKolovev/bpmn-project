import time
import uuid

import jwt
import pytest

from app.security import (
    DEFAULT_MAX_AGE_SECONDS,
    JWT_ALGORITHM,
    issue_session_token,
    verify_session_token,
)


class TestSessionTokens:
    def test_issue_and_verify(self):
        user_id = uuid.uuid4()
        token = issue_session_token(user_id, "secret-value")

        assert verify_session_token(user_id, token, "secret-value") is True

    def test_rejects_tampered_token(self):
        user_id = uuid.uuid4()
        token = issue_session_token(user_id, "secret-value")

        # Wrong user_id → sub mismatch.
        assert verify_session_token(uuid.uuid4(), token, "secret-value") is False
        # Truncated/garbled token → invalid signature.
        assert verify_session_token(user_id, f"{token}tampered", "secret-value") is False

    def test_token_is_a_jwt(self):
        """Issued tokens decode as JWTs with `sub`, `iat`, `exp` claims."""
        user_id = uuid.uuid4()
        token = issue_session_token(user_id, "secret-value")

        # JWT format: three dot-separated base64url segments.
        assert token.count(".") == 2
        # `eyJ...` (URL-safe base64 of `{...`).
        assert token.startswith("eyJ")

        decoded = jwt.decode(token, "secret-value", algorithms=[JWT_ALGORITHM])
        assert decoded["sub"] == str(user_id)
        assert "iat" in decoded
        assert "exp" in decoded
        # Default lifetime ~7 days.
        assert decoded["exp"] - decoded["iat"] == DEFAULT_MAX_AGE_SECONDS

    def test_rejects_wrong_secret(self):
        user_id = uuid.uuid4()
        token = issue_session_token(user_id, "secret-value")

        assert verify_session_token(user_id, token, "different-secret") is False

    def test_rejects_expired_token(self):
        """An `exp` claim in the past must fail verification — even with
        a perfectly valid HMAC signature."""
        user_id = uuid.uuid4()
        # Hand-roll an expired token using pyjwt directly so we don't
        # have to monkey-patch `time.time` inside the security module.
        now = int(time.time())
        expired_payload = {
            "sub": str(user_id),
            "iat": now - (DEFAULT_MAX_AGE_SECONDS + 86400),  # 8 days ago
            "exp": now - 86400,  # expired 1 day ago
        }
        expired_token = jwt.encode(expired_payload, "secret-value", algorithm=JWT_ALGORITHM)

        assert verify_session_token(user_id, expired_token, "secret-value") is False

    @pytest.mark.parametrize("bad", [None, "", "not-a-jwt", "x.y", "x.y.z.extra"])
    def test_rejects_malformed_token(self, bad):
        user_id = uuid.uuid4()
        assert verify_session_token(user_id, bad, "secret-value") is False
