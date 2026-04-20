import os


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://bpmn:bpmn@localhost:5432/bpmn",
)

ML_SERVICE_URL = os.environ.get("ML_SERVICE_URL", "http://localhost:8001")
# Message char cap for /generate and /edit user prompts. Matched with
# ml REQUEST_CHAR_LIMIT — both default to 20000. Raised from 12000 after
# client feedback that 10–13 KB PDF-style specs were hitting the limit.
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "20000"))
SESSION_SECRET = os.environ.get("SESSION_SECRET")
SESSION_SECRET_FILE = os.environ.get("SESSION_SECRET_FILE", "/data/session_secret.txt")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")
SESSION_TOKEN_MAX_AGE_DAYS = int(os.environ.get("SESSION_TOKEN_MAX_AGE_DAYS", "7"))

CORS_ALLOWED_ORIGINS_RAW = os.environ.get("CORS_ALLOWED_ORIGINS", "")

if CORS_ALLOWED_ORIGINS_RAW.strip():
    CORS_ALLOWED_ORIGINS = [
        origin.strip()
        for origin in CORS_ALLOWED_ORIGINS_RAW.split(",")
        if origin.strip() and origin.strip() != "*"
    ]
else:
    # Development fallback — only used when env var is not set
    CORS_ALLOWED_ORIGINS = [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
