"""Level 3 i18n full-stack tests.

These connect a real Socket.IO client to the backend and exercise the full
pipeline: socket → backend → ml → LLM → backend → socket. They verify
that Russian messages flow through the whole stack intact and produce
Russian BPMN output in the user-facing response.

Skipped unless RUN_E2E=1. Typical invocation (inside docker backend container):

    RUN_E2E=1 pytest tests/test_i18n_fullstack.py -v

Requires: backend, ml, and db containers all up.
"""
import asyncio
import os
import re

import pytest
import socketio
from defusedxml import ElementTree as ET


RUN_E2E = os.environ.get("RUN_E2E", "0") == "1"
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.fullstack,
    pytest.mark.skipif(not RUN_E2E, reason="Set RUN_E2E=1 to run E2E tests"),
    pytest.mark.flaky(reruns=2, reruns_delay=2),
]


BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_CYRILLIC_RANGES = [
    (0x0400, 0x04FF),
    (0x0500, 0x052F),
    (0x2DE0, 0x2DFF),
    (0xA640, 0xA69F),
]


def _is_cyrillic(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CYRILLIC_RANGES)


def cyrillic_ratio(s: str) -> float:
    if not s:
        return 0.0
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _is_cyrillic(c)) / len(letters)


def extract_flow_node_names(xml_string: str) -> list[str]:
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return []
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return []
    names = []
    for elem in list(process):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag == "sequenceFlow":
            continue
        if name := elem.get("name"):
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend_url() -> str:
    return os.environ.get("BACKEND_E2E_URL", "http://backend:8000")


class SocketConversation:
    """Small helper that collects server-sent events and lets tests wait
    for a specific `action` to arrive."""

    def __init__(self, sio: socketio.AsyncClient):
        self.sio = sio
        self.events: list[dict] = []
        self._received = asyncio.Event()

        @sio.on("new_action_event")
        async def _on_event(data):
            self.events.append(data)
            self._received.set()

    async def send(self, payload: dict):
        await self.sio.emit("new_action_event", payload)

    async def wait_for_action(
        self,
        action: str,
        timeout: float = 90.0,
    ) -> dict:
        """Wait until an event with given action is received; return it."""
        loop_deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for ev in list(self.events):
                if ev.get("action") == action:
                    self.events.remove(ev)
                    return ev
            remaining = loop_deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"Timeout waiting for action={action}; events seen: {self.events}"
                )
            self._received.clear()
            try:
                await asyncio.wait_for(self._received.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise AssertionError(
                    f"Timeout waiting for action={action}; events seen: {self.events}"
                )


@pytest.fixture
async def conversation(backend_url):
    """Yields a fresh Socket.IO client already initialized (received init_data)."""
    sio = socketio.AsyncClient(reconnection=False)
    convo = SocketConversation(sio)
    await sio.connect(backend_url, socketio_path="/socket.io")
    try:
        # Initialize session — gets user_id + session_token
        await convo.send({"action": "init"})
        init_data = await convo.wait_for_action("init_data", timeout=15)
        assert "user_id" in init_data
        yield convo
    finally:
        await sio.disconnect()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


RU_VALID_PROCESS = (
    "Процесс согласования договора: менеджер создаёт заявку, "
    "юрист проверяет документы, директор подписывает."
)
RU_WEATHER = "Какая сегодня погода?"
EN_VALID_PROCESS = (
    "Order fulfillment: customer places order, payment is verified, "
    "item is shipped to the customer."
)


class TestFullStackI18n:
    async def test_russian_flow_end_to_end(self, conversation):
        """Send Russian process description → receive Russian BPMN diagram."""
        await conversation.send(
            {
                "action": "message",
                "session_id": None,
                "text": RU_VALID_PROCESS,
            }
        )
        result = await conversation.wait_for_action("result", timeout=120)
        xml = result.get("bpmn_xml", "")
        assert xml, f"Expected bpmn_xml in result, got {result}"

        names = extract_flow_node_names(xml)
        assert names, f"No named flow nodes in generated XML: {xml[:300]}"

        low_cyr = [(n, round(cyrillic_ratio(n), 2)) for n in names if cyrillic_ratio(n) < 0.7]
        assert not low_cyr, (
            f"Russian input should produce Russian names end-to-end; "
            f"offenders: {low_cyr}. All: {names}"
        )

        session_name = result.get("session_name", "")
        assert session_name
        assert cyrillic_ratio(session_name) >= 0.7, (
            f"session_name must be Russian, got: {session_name!r}"
        )

    async def test_russian_rejection_passes_reason_to_client(self, conversation):
        """Invalid Russian input → `error` event with Russian message."""
        await conversation.send(
            {
                "action": "message",
                "session_id": None,
                "text": RU_WEATHER,
            }
        )
        error = await conversation.wait_for_action("error", timeout=30)
        message = error.get("message", "")
        assert message, f"Error message must not be empty: {error}"
        # Backend prefixes with English "This doesn't look like..." + reason in Russian.
        # Check that SOME Cyrillic is present (from the reason).
        # Lower threshold because of the English prefix.
        assert cyrillic_ratio(message) >= 0.2, (
            f"Error message for Russian input should contain Russian reason; "
            f"got: {message!r} (cyrillic_ratio={cyrillic_ratio(message):.2f})"
        )

    async def test_english_flow_regression(self, conversation):
        """Regression: English input still produces English BPMN."""
        await conversation.send(
            {
                "action": "message",
                "session_id": None,
                "text": EN_VALID_PROCESS,
            }
        )
        result = await conversation.wait_for_action("result", timeout=120)
        xml = result.get("bpmn_xml", "")
        assert xml

        names = extract_flow_node_names(xml)
        assert names
        high_cyr = [
            (n, round(cyrillic_ratio(n), 2))
            for n in names
            if cyrillic_ratio(n) > 0.1
        ]
        assert not high_cyr, (
            f"English input must keep English names; offenders: {high_cyr}. All: {names}"
        )
