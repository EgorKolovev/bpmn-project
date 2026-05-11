"""Slim Layer-3 Socket.IO full-stack E2E suite — the only tests that
spin up the real backend + ml + db stack and exchange messages over
the live Socket.IO transport.

Replaces the original `test_complex_fullstack.py`, `test_i18n_fullstack.py`
and `test_lanes_fullstack.py` (5 tests). What we keep:

  1. **Russian generate end-to-end** — Socket.IO → backend → ml →
     LLM → result event, with Russian labels.
  2. **Classify rejection end-to-end** — invalid description never
     reaches /generate; the user sees an `error` event with a reason.

Every other behaviour (routing, classify fallback, ML errors,
session ownership, rate limit, validation) is covered by
`tests/test_integration_backend.py` against mocked ml.

Run with `RUN_E2E=1 pytest backend/tests/test_e2e_fullstack.py -v`.
"""
import pytest

from tests.conftest import E2E_MARKERS, TIMEOUTS


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
