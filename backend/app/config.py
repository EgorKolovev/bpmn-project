import os


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://bpmn:bpmn@localhost:5432/bpmn",
)

ML_SERVICE_URL = os.environ.get("ML_SERVICE_URL", "http://localhost:8001")
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "12000"))
SESSION_SECRET = os.environ.get("SESSION_SECRET")
SESSION_SECRET_FILE = os.environ.get("SESSION_SECRET_FILE", "/tmp/bpmn_session_secret.txt")
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        ",".join(
            [
                "http://localhost",
                "http://127.0.0.1",
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            ]
        ),
    ).split(",")
    if origin.strip()
]
