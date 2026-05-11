"""Slim Layer-3 Socket.IO full-stack E2E suite — the only tests that
spin up the real backend + ml + db stack and exchange messages over
the live Socket.IO transport.

Replaces the original `test_complex_fullstack.py`, `test_i18n_fullstack.py`
and `test_lanes_fullstack.py` (5 tests). What we keep:

  1. **Russian generate end-to-end** — Socket.IO → backend → ml →
     LLM → result event, with Russian labels.
  2. **Classify rejection end-to-end** — invalid description never
     reaches /generate; the user sees an `error` event with a reason.
  3. **Websocket reconnect** — open a session, disconnect, reconnect
     with the stored (user_id, session_token), verify the session
     list comes back. Exercises the real `connect`/`disconnect`
     handlers plus the engine.io handshake `auth` payload — the
     parts the integration tests stub out.

Every other behaviour (routing, classify fallback, ML errors,
session ownership, rate limit, validation, DB-unavailable, expired
token) is covered by `tests/test_integration_backend.py` against
mocked ml.

Run with `RUN_E2E=1 pytest backend/tests/test_e2e_fullstack.py -v`.
"""
import pytest
import socketio  # python-socketio client

from tests.conftest import (
    E2E_MARKERS,
    TIMEOUTS,
    SocketConversation,
)


pytestmark = E2E_MARKERS


RU_VALID_PROCESS = (
    "Процесс согласования договора: менеджер создаёт заявку, "
    "юрист проверяет документы, директор подписывает."
)
RU_WEATHER = "Какая сегодня погода?"


async def test_russian_generate_round_trip(socketio_conversation):
    """Russian description → Russian BPMN diagram back via Socket.IO.
    The simplest end-to-end smoke for the entire stack."""
    await socketio_conversation.send(
        {"action": "message", "text": RU_VALID_PROCESS}
    )
    result = await socketio_conversation.wait_for_action(
        "result", timeout=TIMEOUTS.SOCKETIO_EVENT
    )
    assert "bpmn_xml" in result
    assert "session_id" in result
    assert result.get("session_name")
    # The diagram should have at least one task with a Cyrillic name.
    assert any(
        0x0400 <= ord(c) <= 0x04FF for c in result["bpmn_xml"][:5000]
    ), "no Cyrillic content found in BPMN XML"


async def test_classify_rejection_round_trip(socketio_conversation):
    """Weather question → classify rejects → user gets an `error`
    event with a reason, NOT a `result`."""
    await socketio_conversation.send(
        {"action": "message", "text": RU_WEATHER}
    )
    error = await socketio_conversation.wait_for_action(
        "error", timeout=TIMEOUTS.SOCKETIO_EVENT
    )
    assert error.get("message"), "expected non-empty error message"


async def test_websocket_reconnect_restores_user_session(backend_url):
    """Page-refresh / wifi-blip continuity, end-to-end over the real
    Socket.IO transport.

      1. Open client A, run init → receive (user_id, session_token).
      2. Create a session via /generate so we have something to "lose".
      3. Close client A.
      4. Open client B with the saved credentials in the handshake
         `auth` payload, run init.
      5. Verify the server returned the same user_id AND the session
         the first client created appears in `init_data.sessions`.

    This is the only fullstack test that does NOT need the LLM, but
    we keep it under E2E_MARKERS because it depends on the running
    backend container (real ASGI server, real Socket.IO transport).
    """
    # --- Client A: init + create a session ---
    client_a = socketio.AsyncClient(reconnection=False)
    convo_a = SocketConversation(client_a)
    await client_a.connect(backend_url, socketio_path="/socket.io")
    try:
        await convo_a.send({"action": "init"})
        first_init = await convo_a.wait_for_action(
            "init_data", timeout=TIMEOUTS.SOCKETIO_INIT
        )
        user_id = first_init["user_id"]
        session_token = first_init["session_token"]
        assert user_id and session_token, (
            f"init_data missing credentials: {first_init!r}"
        )

        # Use a known-valid Russian process so /classify accepts it
        # without burning LLM time on retries.
        await convo_a.send(
            {
                "action": "message",
                "text": (
                    "Процесс согласования договора: менеджер создаёт "
                    "заявку, юрист проверяет, директор подписывает."
                ),
            }
        )
        result = await convo_a.wait_for_action(
            "result", timeout=TIMEOUTS.SOCKETIO_EVENT
        )
        created_session_id = result["session_id"]
    finally:
        await client_a.disconnect()

    # --- Client B: reconnect with saved credentials ---
    client_b = socketio.AsyncClient(reconnection=False)
    convo_b = SocketConversation(client_b)
    await client_b.connect(
        backend_url,
        socketio_path="/socket.io",
        auth={"user_id": user_id, "session_token": session_token},
    )
    try:
        await convo_b.send({"action": "init"})
        second_init = await convo_b.wait_for_action(
            "init_data", timeout=TIMEOUTS.SOCKETIO_INIT
        )
        assert second_init["user_id"] == user_id, (
            f"reconnect changed user_id: {user_id} → {second_init['user_id']}"
        )
        sessions = second_init.get("sessions", [])
        session_ids = [s.get("session_id") for s in sessions]
        assert created_session_id in session_ids, (
            f"session created before disconnect was lost on reconnect; "
            f"got: {session_ids!r}"
        )
    finally:
        await client_b.disconnect()
