import logging
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import httpx
import socketio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import (
    CORS_ALLOWED_ORIGINS,
    INTERNAL_API_KEY,
    MAX_MESSAGE_CHARS,
    ML_SERVICE_URL,
    SESSION_SECRET,
    SESSION_SECRET_FILE,
)
from app.database import async_session, init_db
from app.models import Message, Session
from app.security import (
    issue_session_token,
    load_or_create_session_secret,
    verify_session_token,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ml_http_client: Optional[httpx.AsyncClient] = None
session_signing_secret: Optional[str] = None

# Rate limiting: track timestamps per SID
_rate_limit_map: Dict[str, list] = defaultdict(list)
RATE_LIMIT_WINDOW = 10  # seconds
RATE_LIMIT_MAX = 5  # max messages per window

MAX_SESSIONS_PER_USER = 50


class ClientInputError(Exception):
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ml_http_client, session_signing_secret
    await init_db()

    headers = {}
    if INTERNAL_API_KEY:
        headers["X-Internal-Api-Key"] = INTERNAL_API_KEY

    ml_http_client = httpx.AsyncClient(
        base_url=ML_SERVICE_URL,
        timeout=60.0,
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


def _check_rate_limit(sid: str) -> None:
    now = time.time()
    timestamps = _rate_limit_map[sid]
    # Remove old entries outside window
    _rate_limit_map[sid] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limit_map[sid]) >= RATE_LIMIT_MAX:
        raise ClientInputError("Too many requests. Please wait a moment.")
    _rate_limit_map[sid].append(now)


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
            _check_rate_limit(sid)
            await handle_message(sid, data)
        else:
            raise ClientInputError("Unknown action.")
    except ClientInputError as exc:
        logger.warning("Client input error from %s: %s", sid, exc)
        await _emit_error(sid, str(exc))
    except Exception:
        logger.exception("Error handling action '%s'", action)
        await _emit_error(sid, "Internal server error.")


async def _emit_error(sid: str, message: str) -> None:
    await sio.emit(
        "new_action_event",
        {
            "action": "error",
            "message": message,
        },
        to=sid,
    )


def _parse_uuid(value: Any, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClientInputError(f"Invalid {field_name}.") from exc


def _try_parse_uuid(value: Any) -> Optional[uuid.UUID]:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _normalize_message_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ClientInputError("Message text must be a string.")

    text = value.strip()
    if not text:
        raise ClientInputError("Message text is required.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ClientInputError(f"Message text exceeds {MAX_MESSAGE_CHARS} characters.")
    return text


async def _resolve_user_identity(
    sid: str,
    data: Dict[str, Any],
) -> tuple[uuid.UUID, str]:
    global session_signing_secret

    if session_signing_secret is None:
        raise RuntimeError("Session secret not initialized.")

    socket_session = await sio.get_session(sid)
    requested_user_id = socket_session.get("requested_user_id") or data.get("user_id")
    requested_session_token = socket_session.get("requested_session_token") or data.get(
        "session_token"
    )

    user_id = _try_parse_uuid(requested_user_id)
    if user_id and verify_session_token(user_id, requested_session_token, session_signing_secret):
        session_token = requested_session_token
    else:
        # Invalid/expired/missing token — always create fresh identity
        if user_id and requested_session_token:
            logger.warning("Invalid or expired session token for user %s", user_id)
        user_id = uuid.uuid4()
        session_token = issue_session_token(user_id, session_signing_secret)

    await sio.save_session(
        sid,
        {
            "user_id": str(user_id),
            "session_token": session_token,
            "requested_user_id": str(user_id),
            "requested_session_token": session_token,
        },
    )
    return user_id, session_token


async def _get_bound_user_id(sid: str) -> uuid.UUID:
    socket_session = await sio.get_session(sid)
    user_id = socket_session.get("user_id")
    if not user_id:
        raise ClientInputError("Session not initialized.")
    return _parse_uuid(user_id, "user_id")


def _extract_ml_error_detail(exc: httpx.HTTPStatusError) -> str:
    # Only return safe, generic messages to the client
    if exc.response is not None and exc.response.status_code == 429:
        return "Daily usage cap reached. Try again later."
    logger.error("ML service error: status=%s", exc.response.status_code if exc.response else "N/A")
    return "Processing failed. Please try again."


async def handle_init(sid, data):
    user_id, session_token = await _resolve_user_identity(sid, data)

    async with async_session() as db:
        result = await db.execute(
            select(Session)
            .where(Session.user_id == user_id)
            .order_by(Session.updated_at.desc())
            .limit(MAX_SESSIONS_PER_USER)
        )
        sessions = result.scalars().all()

    sessions_list = [
        {"session_id": str(session.id), "name": session.name or "Untitled"}
        for session in sessions
    ]

    await sio.emit(
        "new_action_event",
        {
            "action": "init_data",
            "user_id": str(user_id),
            "session_token": session_token,
            "sessions": sessions_list,
        },
        to=sid,
    )


async def handle_open_session(sid, data):
    session_id = data.get("session_id")
    if not session_id:
        raise ClientInputError("session_id is required.")

    user_id = await _get_bound_user_id(sid)
    session_uuid = _parse_uuid(session_id, "session_id")

    async with async_session() as db:
        result = await db.execute(
            select(Session)
            .options(selectinload(Session.messages))
            .where(Session.id == session_uuid, Session.user_id == user_id)
        )
        session = result.scalar_one_or_none()

    if not session:
        raise ClientInputError("Session not found.")

    history = []
    for msg in sorted(session.messages, key=lambda message: message.order):
        entry = {"role": msg.role}
        if msg.role == "user":
            entry["text"] = msg.text
        else:
            entry["bpmn_xml"] = msg.bpmn_xml
        history.append(entry)

    await sio.emit(
        "new_action_event",
        {
            "action": "session_data",
            "session_id": str(session.id),
            "name": session.name or "Untitled",
            "bpmn_xml": session.current_bpmn_xml or "",
            "history": history,
        },
        to=sid,
    )


async def handle_message(sid, data):
    text = _normalize_message_text(data.get("text"))
    user_id = await _get_bound_user_id(sid)
    raw_session_id = data.get("session_id")
    if raw_session_id in (None, ""):
        session_uuid = None
        is_new_session = True
    else:
        session_uuid = _parse_uuid(raw_session_id, "session_id")
        is_new_session = False

    async with async_session() as db:
        if is_new_session:
            try:
                ml_response = await ml_http_client.post(
                    "/generate",
                    json={"description": text},
                )
                ml_response.raise_for_status()
                ml_data = ml_response.json()
            except httpx.HTTPStatusError as exc:
                await _emit_error(sid, _extract_ml_error_detail(exc))
                return
            except Exception:
                logger.exception("ML service communication error")
                await _emit_error(sid, "Processing failed. Please try again.")
                return

            bpmn_xml = ml_data.get("bpmn_xml", "")
            session_name = ml_data.get("session_name", "Untitled")

            if not bpmn_xml:
                await _emit_error(sid, "Processing failed. Please try again.")
                return

            new_session_id = uuid.uuid4()

            session = Session(
                id=new_session_id,
                user_id=user_id,
                name=session_name,
                current_bpmn_xml=bpmn_xml,
            )
            db.add(session)
            await db.flush()

            db.add(
                Message(
                    session_id=new_session_id,
                    role="user",
                    text=text,
                    order=0,
                )
            )
            db.add(
                Message(
                    session_id=new_session_id,
                    role="assistant",
                    bpmn_xml=bpmn_xml,
                    order=1,
                )
            )

            await db.commit()

            await sio.emit(
                "new_action_event",
                {
                    "action": "result",
                    "bpmn_xml": bpmn_xml,
                    "session_id": str(session.id),
                    "session_name": session_name,
                },
                to=sid,
            )
            return

        result = await db.execute(
            select(Session)
            .options(selectinload(Session.messages))
            .where(Session.id == session_uuid, Session.user_id == user_id)
        )
        session = result.scalar_one_or_none()

        if not session:
            raise ClientInputError("Session not found.")

        current_xml = session.current_bpmn_xml
        if not current_xml:
            raise ClientInputError("Session has no existing BPMN diagram.")

        try:
            ml_response = await ml_http_client.post(
                "/edit",
                json={"prompt": text, "bpmn_xml": current_xml},
            )
            ml_response.raise_for_status()
            ml_data = ml_response.json()
        except httpx.HTTPStatusError as exc:
            await _emit_error(sid, _extract_ml_error_detail(exc))
            return
        except Exception:
            logger.exception("ML service communication error")
            await _emit_error(sid, "Processing failed. Please try again.")
            return

        bpmn_xml = ml_data.get("bpmn_xml", "")
        if not bpmn_xml:
            await _emit_error(sid, "Processing failed. Please try again.")
            return

        msg_count = len(session.messages)

        db.add(
            Message(
                session_id=session.id,
                role="user",
                text=text,
                order=msg_count,
            )
        )
        db.add(
            Message(
                session_id=session.id,
                role="assistant",
                bpmn_xml=bpmn_xml,
                order=msg_count + 1,
            )
        )

        session.current_bpmn_xml = bpmn_xml
        db.add(session)
        await db.commit()

        await sio.emit(
            "new_action_event",
            {
                "action": "result",
                "bpmn_xml": bpmn_xml,
            },
            to=sid,
        )
