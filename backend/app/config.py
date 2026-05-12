"""Backend runtime configuration driven by environment variables.

Implemented on top of `pydantic-settings.BaseSettings` for typed
parsing and validation. The old `os.environ.get(...)` constants stay
exported under their UPPER_SNAKE_CASE names — every existing import
(`from app.config import DATABASE_URL`, etc.) keeps working.
"""

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_DEFAULT_CORS_ORIGINS = [
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


class Settings(BaseSettings):
    """Strongly-typed view of backend environment variables.

    Field names use snake_case; environment variable lookup is
    case-insensitive (BaseSettings default), so `DATABASE_URL` and
    `database_url` resolve to the same value.
    """

    database_url: str = Field(default="postgresql+asyncpg://bpmn:bpmn@localhost:5432/bpmn")
    ml_service_url: str = Field(default="http://localhost:8001")
    # Message char cap for /generate and /edit user prompts. Matched
    # with ml REQUEST_CHAR_LIMIT — both default to 20000.
    max_message_chars: int = Field(default=20000)
    session_secret: str | None = Field(default=None)
    session_secret_file: str = Field(default="/data/session_secret.txt")
    internal_api_key: str = Field(default="")
    session_token_max_age_days: int = Field(default=7)
    # `NoDecode` tells pydantic-settings NOT to JSON-decode the raw
    # env-var value (default list-field behaviour would try and fail
    # on "http://a,http://b"). The `@field_validator(mode="before")`
    # below receives the raw string and does the comma split itself.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(_DEFAULT_CORS_ORIGINS)
    )

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors(cls, value: object) -> object:
        """Parse comma-separated CORS list from env vars.

        Accepts `"http://a, http://b"` (env-style), a real list (test
        fixtures), or empty/whitespace which falls back to the
        development default list.
        """
        if value is None:
            return list(_DEFAULT_CORS_ORIGINS)
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return list(_DEFAULT_CORS_ORIGINS)
            parsed = [
                origin.strip()
                for origin in stripped.split(",")
                if origin.strip() and origin.strip() != "*"
            ]
            return parsed if parsed else list(_DEFAULT_CORS_ORIGINS)
        return value


settings = Settings()

# --- Backward-compatible module-level aliases ------------------------------
# These mirror the previous `os.environ.get`-style exports so existing
# `from app.config import DATABASE_URL` lines keep working unchanged.

DATABASE_URL: str = settings.database_url
ML_SERVICE_URL: str = settings.ml_service_url
MAX_MESSAGE_CHARS: int = settings.max_message_chars
SESSION_SECRET: str | None = settings.session_secret
SESSION_SECRET_FILE: str = settings.session_secret_file
INTERNAL_API_KEY: str = settings.internal_api_key
SESSION_TOKEN_MAX_AGE_DAYS: int = settings.session_token_max_age_days
CORS_ALLOWED_ORIGINS: list[str] = settings.cors_allowed_origins
