import uuid

from app.security import issue_session_token, verify_session_token


class TestSessionTokens:
    def test_issue_and_verify(self):
        user_id = uuid.uuid4()
        token = issue_session_token(user_id, "secret-value")

        assert verify_session_token(user_id, token, "secret-value") is True

    def test_rejects_tampered_token(self):
        user_id = uuid.uuid4()
        token = issue_session_token(user_id, "secret-value")

        assert verify_session_token(uuid.uuid4(), token, "secret-value") is False
        assert verify_session_token(user_id, f"{token}tampered", "secret-value") is False
