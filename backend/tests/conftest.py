"""Shared test fixtures + helpers for the backend service.

Two test layers live in this module:

  * **Integration tests** (Layer 2A/2B) — `mock_ml` fixture swaps
    `backend.app.main.ml_http_client` for an `httpx.AsyncClient` backed
    by `httpx.MockTransport`, so backend logic can be exercised without
    ever touching the real ml service. Tests call
    `mock_ml.set({"/classify": …, "/generate": …, "/edit": …})` to
    arrange the canned responses.

  * **Real-LLM Socket.IO E2E** (slimmed to a handful of tests) —
    `socketio_conversation` fixture connects to a live backend over
    `socket.io` and yields a tiny helper that lets tests await a
    specific server-sent action by name.
"""

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
import socketio  # python-socketio client

# Override DATABASE_URL at import time so any later `from app.config
# import DATABASE_URL` picks up the test value.
#
# We *force* the override (rather than `setdefault`) because the
# docker container ships with `DATABASE_URL=postgresql+asyncpg://…`,
# and we want backend integration tests to run against an isolated
# in-process SQLite file — no event-loop scoping mismatch with
# asyncpg, no cross-test state, no need for the Postgres service.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_backend.db"
os.environ.setdefault("ML_SERVICE_URL", "http://localhost:8001")


# ---------------------------------------------------------------------------
# Timeouts & flags. Mirrored on the ml side (`ml/tests/conftest.py`) —
# the two test packages run independently so duplicating these small
# constants is preferable to a cross-package import.
# ---------------------------------------------------------------------------

RUN_E2E = os.environ.get("RUN_E2E", "0") == "1"

TIMEOUTS = SimpleNamespace(
    SOCKETIO_EVENT=90.0,
    SOCKETIO_INIT=15.0,
    HTTP=30.0,
)

E2E_RERUNS = 2
E2E_RERUN_DELAY = 2

E2E_MARKERS = [
    pytest.mark.e2e,
    pytest.mark.skipif(not RUN_E2E, reason="Set RUN_E2E=1 to run E2E tests"),
    pytest.mark.flaky(reruns=E2E_RERUNS, reruns_delay=E2E_RERUN_DELAY),
]


# ---------------------------------------------------------------------------
# Canned BPMN response shapes used by integration tests.
# ---------------------------------------------------------------------------

SAMPLE_BPMN_XML = """<?xml version='1.0' encoding='UTF-8'?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
                  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
                  xmlns:di="http://www.omg.org/spec/DD/20100524/DI">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:startEvent id="Start_1"><bpmn:outgoing>F1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:task id="T1" name="Do Thing"><bpmn:incoming>F1</bpmn:incoming><bpmn:outgoing>F2</bpmn:outgoing></bpmn:task>
    <bpmn:endEvent id="End_1"><bpmn:incoming>F2</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="F1" sourceRef="Start_1" targetRef="T1"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T1" targetRef="End_1"/>
  </bpmn:process>
  <bpmndi:BPMNDiagram id="D1">
    <bpmndi:BPMNPlane id="P1" bpmnElement="Process_1"/>
  </bpmndi:BPMNDiagram>
</bpmn:definitions>"""

SAMPLE_GENERATE_RESPONSE = {
    "bpmn_xml": SAMPLE_BPMN_XML,
    "session_name": "Test Process",
}

SAMPLE_EDIT_RESPONSE = {"bpmn_xml": SAMPLE_BPMN_XML}

SAMPLE_CLASSIFY_VALID = {"is_valid": True}
SAMPLE_CLASSIFY_INVALID = {
    "is_valid": False,
    "reason": "This doesn't look like a business process.",
}


# ---------------------------------------------------------------------------
# Mock ML transport — used by Layer 2A/2B integration tests.
# ---------------------------------------------------------------------------


class _MockMLClient:
    """Stateful wrapper around `httpx.MockTransport` that lets a test
    swap the response map at any time (most tests set it once in
    arrange; a few re-set it mid-test to simulate ml flakiness).
    """

    def __init__(self):
        self._handlers: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        self.request_bodies: list[dict] = []
        self.client = httpx.AsyncClient(
            base_url="http://ml-mock",
            transport=httpx.MockTransport(self._handle),
            timeout=TIMEOUTS.HTTP,
        )

    def set(self, handlers: dict[str, Any]) -> None:
        """Replace the response map. Values may be:

        * dict / list  — returned as 200 OK JSON
        * (status, body) tuple — explicit status + JSON body
        * callable(request) -> httpx.Response — full control
        * Exception instance — re-raised (simulates network error)
        """
        self._handlers = dict(handlers)

    def clear_requests(self) -> None:
        """Drop captured requests + bodies — call between arrange phases
        of a single test to keep the two lists in lock-step."""
        self.requests.clear()
        self.request_bodies.clear()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            import json as _json

            self.request_bodies.append(_json.loads(request.content or b"{}"))
        except Exception:
            self.request_bodies.append({})
        path = request.url.path
        for needle, response in self._handlers.items():
            if needle in path:
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(request)
                if isinstance(response, tuple) and len(response) == 2:
                    status, body = response
                    return httpx.Response(status, json=body)
                return httpx.Response(200, json=response)
        return httpx.Response(
            404,
            json={"error": f"no mock handler for {path}"},
        )

    async def aclose(self) -> None:
        await self.client.aclose()


@pytest_asyncio.fixture
async def mock_ml():
    """Install a `_MockMLClient` into `backend.app.main.ml_http_client`,
    yield it (test calls `.set(...)` to arrange), and restore the
    original module-global on teardown.
    """
    from app import main as backend_main

    saved = backend_main.ml_http_client
    mock = _MockMLClient()
    backend_main.ml_http_client = mock.client
    try:
        yield mock
    finally:
        backend_main.ml_http_client = saved
        await mock.aclose()


# ---------------------------------------------------------------------------
# Socket.IO conversation helper (real-server E2E)
# ---------------------------------------------------------------------------


class SocketConversation:
    """Collects server-sent `new_action_event` payloads and lets tests
    await a particular `action` value by name.
    """

    def __init__(self, sio: socketio.AsyncClient):
        self.sio = sio
        self.events: list[dict] = []
        self._received = asyncio.Event()

        @sio.on("new_action_event")
        async def _on_event(data):
            self.events.append(data)
            self._received.set()

    async def send(self, payload: dict) -> None:
        await self.sio.emit("new_action_event", payload)

    async def wait_for_action(
        self,
        action: str,
        timeout: float = TIMEOUTS.SOCKETIO_EVENT,
    ) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for ev in list(self.events):
                if ev.get("action") == action:
                    self.events.remove(ev)
                    return ev
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"Timeout waiting for action={action!r}; " f"events seen: {self.events}"
                )
            self._received.clear()
            try:
                await asyncio.wait_for(self._received.wait(), timeout=remaining)
            except TimeoutError as exc:
                raise AssertionError(
                    f"Timeout waiting for action={action!r}; events seen: {self.events}"
                ) from exc


@pytest.fixture
def backend_url() -> str:
    return os.environ.get("BACKEND_E2E_URL", "http://backend:8000")


@pytest_asyncio.fixture
async def socketio_conversation(backend_url):
    """Connect a Socket.IO client to the live backend, run the init
    handshake, yield a `SocketConversation` for the test, disconnect
    on teardown.
    """
    sio = socketio.AsyncClient(reconnection=False)
    convo = SocketConversation(sio)
    await sio.connect(backend_url, socketio_path="/ws")
    try:
        await convo.send({"action": "init"})
        init = await convo.wait_for_action("init_data", timeout=TIMEOUTS.SOCKETIO_INIT)
        assert "user_id" in init, f"missing user_id in init_data: {init}"
        yield convo
    finally:
        await sio.disconnect()
