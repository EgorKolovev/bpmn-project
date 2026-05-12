"""Socket.IO message handlers — the business logic of the backend.

Every external dependency that the test suite monkey-patches at module
scope on `app.main` (e.g. `sio`, `async_session`, `ml_http_client`,
`session_signing_secret`) is read from `app.main` at *call time*, not
import time. That lets `monkeypatch.setattr(backend_main, "sio", fake)`
take effect for handler invocations made through this module.

Constants like `RATE_LIMIT_*` and `MAX_SESSIONS_PER_USER` come from
`app.main` too, so any tweak made in the main module is visible here
without rebinding.
"""

import logging
import uuid
from typing import Any

import httpx

from app import main as _m
from app.config import MAX_MESSAGE_CHARS
from app.repositories import MessageRepository, SessionRepository
from app.security import issue_session_token, verify_session_token

logger = logging.getLogger(__name__)


class ClientInputError(Exception):
    """Raised when a Socket.IO payload from the client is malformed or
    semantically invalid. `handle_action` catches this and surfaces the
    message as an `error` event — the user sees the reason.
    """


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


async def _emit_error(sid: str, message: str) -> None:
    await _m.sio.emit(
        "new_action_event",
        {"action": "error", "message": message},
        to=sid,
    )


def _parse_uuid(value: Any, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClientInputError(f"Invalid {field_name}.") from exc


def _try_parse_uuid(value: Any) -> uuid.UUID | None:
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


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


async def _resolve_user_identity(
    sid: str,
    data: dict[str, Any],
) -> tuple[uuid.UUID, str]:
    if _m.session_signing_secret is None:
        raise RuntimeError("Session secret not initialized.")

    socket_session = await _m.sio.get_session(sid)
    requested_user_id = socket_session.get("requested_user_id") or data.get("user_id")
    requested_session_token = socket_session.get("requested_session_token") or data.get(
        "session_token"
    )

    user_id = _try_parse_uuid(requested_user_id)
    if user_id and verify_session_token(
        user_id, requested_session_token, _m.session_signing_secret
    ):
        session_token = requested_session_token
    else:
        # Invalid/expired/missing token — always create fresh identity.
        if user_id and requested_session_token:
            logger.warning("Invalid or expired session token for user %s", user_id)
        user_id = uuid.uuid4()
        session_token = issue_session_token(user_id, _m.session_signing_secret)

    await _m.sio.save_session(
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
    socket_session = await _m.sio.get_session(sid)
    user_id = socket_session.get("user_id")
    if not user_id:
        raise ClientInputError("Session not initialized.")
    return _parse_uuid(user_id, "user_id")


def _extract_ml_error_detail(exc: httpx.HTTPStatusError) -> str:
    # Only return safe, generic messages to the client.
    if exc.response is not None and exc.response.status_code == 429:
        return "Daily usage cap reached. Try again later."
    logger.error(
        "ML service error: status=%s",
        exc.response.status_code if exc.response else "N/A",
    )
    return "Processing failed. Please try again."


# ---------------------------------------------------------------------------
# Classify (ml HTTP boundary)
# ---------------------------------------------------------------------------


async def _classify_input(text: str) -> None:
    """Call ML classify endpoint; raise `ClientInputError` if the input
    is not BPMN-related. HTTP / transport failures are logged and let
    through (documented fallback) so a flaky classifier doesn't block
    legitimate users."""
    try:
        response = await _m.ml_http_client.post("/classify", json={"text": text})
        response.raise_for_status()
        result = response.json()
        if not result.get("is_valid", False):
            reason = result.get("reason", "")
            msg = "This doesn't look like a business process description."
            if reason:
                msg += f" {reason}"
            raise ClientInputError(msg)
    except httpx.HTTPStatusError:
        logger.warning("Classification endpoint returned error; skipping check")
    except httpx.HTTPError:
        logger.warning("Classification endpoint unreachable; skipping check")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def handle_init(sid, data):
    user_id, session_token = await _resolve_user_identity(sid, data)

    async with _m.async_session() as db:
        sessions = await SessionRepository(db).list_for_user(user_id, _m.MAX_SESSIONS_PER_USER)

    sessions_list = [
        {"session_id": str(session.id), "name": session.name or "Untitled"} for session in sessions
    ]

    await _m.sio.emit(
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

    async with _m.async_session() as db:
        session = await SessionRepository(db).get_with_messages(session_uuid, user_id)

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

    await _m.sio.emit(
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

    # Classify input before processing.
    await _classify_input(text)

    async with _m.async_session() as db:
        sessions = SessionRepository(db)
        messages = MessageRepository(db)

        if is_new_session:
            try:
                ml_response = await _m.ml_http_client.post(
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
            session = sessions.add_new(
                session_id=new_session_id,
                user_id=user_id,
                name=session_name,
                bpmn_xml=bpmn_xml,
            )
            await db.flush()
            messages.add_user_message(session_id=new_session_id, text=text, order=0)
            messages.add_assistant_message(session_id=new_session_id, bpmn_xml=bpmn_xml, order=1)
            await db.commit()

            await _m.sio.emit(
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

        session = await sessions.get_with_messages(session_uuid, user_id)

        if not session:
            raise ClientInputError("Session not found.")

        current_xml = session.current_bpmn_xml
        if not current_xml:
            raise ClientInputError("Session has no existing BPMN diagram.")

        try:
            ml_response = await _m.ml_http_client.post(
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
        messages.add_user_message(session_id=session.id, text=text, order=msg_count)
        messages.add_assistant_message(
            session_id=session.id, bpmn_xml=bpmn_xml, order=msg_count + 1
        )
        sessions.update_current_bpmn(session, bpmn_xml)
        await db.commit()

        await _m.sio.emit(
            "new_action_event",
            {
                "action": "result",
                "bpmn_xml": bpmn_xml,
            },
            to=sid,
        )
