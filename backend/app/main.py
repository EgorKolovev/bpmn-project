"""ASGI app entry point — wires FastAPI + Socket.IO, owns module-level
globals (`sio`, `ml_http_client`, `session_signing_secret`, the rate
limit map), and defines the dispatcher `handle_action`.

The actual handler bodies live in `app.handlers`. They reach back into
this module for `sio`, `async_session`, `ml_http_client`,
`session_signing_secret`, and `MAX_SESSIONS_PER_USER` at call time —
that's the seam tests rely on (every existing
`monkeypatch.setattr(backend_main, "sio", fake)` keeps working).
"""

import logging
from contextlib import asynccontextmanager

import httpx
import socketio
from aiolimiter import AsyncLimiter
from fastapi import FastAPI

from app.config import (
    CORS_ALLOWED_ORIGINS,
    INTERNAL_API_KEY,
    ML_SERVICE_URL,
    SESSION_SECRET,
    SESSION_SECRET_FILE,
)

# `async_session` is re-exported so `app.handlers` can read
# `_m.async_session` at call time and tests can `monkeypatch.setattr(
# backend_main, "async_session", ...)` to swap it for a failing factory.
from app.database import async_session, init_db  # noqa: F401  (re-export)
from app.security import load_or_create_session_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ml_http_client: httpx.AsyncClient | None = None
session_signing_secret: str | None = None

# Rate limiting: one `aiolimiter.AsyncLimiter` per SID, lazily created.
# `AsyncLimiter(N, W)` = "up to N actions per W seconds". We use
# `has_capacity()` (sync, atomic in single-threaded asyncio) to test
# *before* acquiring, so the user gets an immediate `error` event
# instead of a coroutine that silently blocks until capacity frees.
_rate_limit_map: dict[str, AsyncLimiter] = {}
RATE_LIMIT_WINDOW = 10  # seconds
RATE_LIMIT_MAX = 5  # max messages per window

MAX_SESSIONS_PER_USER = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ml_http_client, session_signing_secret
    await init_db()

    headers = {}
    if INTERNAL_API_KEY:
        headers["X-Internal-Api-Key"] = INTERNAL_API_KEY

    ml_http_client = httpx.AsyncClient(
        base_url=ML_SERVICE_URL,
        # 240s — longer than the ML service's own 180s httpx timeout so
        # we never cut off a legitimate LLM call from the outside.
        timeout=240.0,
        headers=headers,
    )
    session_signing_secret = load_or_create_session_secret(
        SESSION_SECRET,
        SESSION_SECRET_FILE,
    )
    logger.info("Backend started. ML service at: %s", ML_SERVICE_URL)
    yield
    await ml_http_client.aclose()
    logger.info("Backend shut down")


app = FastAPI(title="BPMN Backend", version="1.0.0", lifespan=lifespan)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=CORS_ALLOWED_ORIGINS,
    logger=False,
    engineio_logger=False,
    max_http_buffer_size=1_000_000,  # 1MB max message size
)

combined_app = socketio.ASGIApp(sio, app, socketio_path="/socket.io")


@app.get("/health")
async def health():
    return {"status": "ok"}


# `from app.handlers import ...` MUST run after the module globals
# above (`sio`, `async_session`, etc.) are bound: handlers.py imports
# `app.main` and reads them at call time, but importing handlers
# before they exist would fail. Bottom of file = safest.
from app.handlers import (  # noqa: E402  (intentional late import)
    ClientInputError,
    _emit_error,
    handle_init,
    handle_message,
    handle_open_session,
)


@sio.event
async def connect(sid, environ, auth):
    auth_payload = auth if isinstance(auth, dict) else {}
    await sio.save_session(
        sid,
        {
            "requested_user_id": auth_payload.get("user_id"),
            "requested_session_token": auth_payload.get("session_token"),
        },
    )
    logger.info("Client connected: %s", sid)


@sio.event
async def disconnect(sid):
    _rate_limit_map.pop(sid, None)
    logger.info("Client disconnected: %s", sid)


async def _check_rate_limit(sid: str) -> None:
    """Per-SID token-bucket rate limit. Raises `ClientInputError` if the
    SID has already used `RATE_LIMIT_MAX` actions in the last
    `RATE_LIMIT_WINDOW` seconds.

    Note: `AsyncLimiter.acquire()` would *block* until a slot frees —
    that's the wrong UX for our chat-style flow. We probe with
    `has_capacity()` first (sync, atomic in single-threaded asyncio)
    and only call `acquire()` when we know it will return immediately.
    """
    limiter = _rate_limit_map.get(sid)
    if limiter is None:
        limiter = AsyncLimiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)
        _rate_limit_map[sid] = limiter

    if not limiter.has_capacity():
        raise ClientInputError("Too many requests. Please wait a moment.")
    await limiter.acquire()


@sio.on("new_action_event")
async def handle_action(sid, data):
    if not isinstance(data, dict):
        await _emit_error(sid, "Invalid event payload.")
        return

    action = data.get("action")
    logger.info("Received action '%s' from %s", action, sid)

    try:
        if action == "init":
            await handle_init(sid, data)
        elif action == "open_session":
            await handle_open_session(sid, data)
        elif action == "message":
            await _check_rate_limit(sid)
            await handle_message(sid, data)
        else:
            raise ClientInputError("Unknown action.")
    except ClientInputError as exc:
        logger.warning("Client input error from %s: %s", sid, exc)
        await _emit_error(sid, str(exc))
    except Exception:
        logger.exception("Error handling action '%s'", action)
        await _emit_error(sid, "Internal server error.")
