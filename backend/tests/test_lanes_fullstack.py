"""Level 3 full-stack test — swimlanes end-to-end."""
import asyncio
import os

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


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def extract_lanes(xml: str) -> dict[str, list[str]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return {}
    out: dict[str, list[str]] = {}
    for child in list(process):
        if _local(child.tag) != "laneSet":
            continue
        for lane in list(child):
            if _local(lane.tag) != "lane":
                continue
            name = lane.get("name", "") or lane.get("id", "")
            refs = [
                (ref.text or "").strip()
                for ref in list(lane)
                if _local(ref.tag) == "flowNodeRef" and (ref.text or "").strip()
            ]
            out[name] = refs
    return out


class SocketConversation:
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

    async def wait_for_action(self, action: str, timeout: float = 180.0) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for ev in list(self.events):
                if ev.get("action") == action:
                    self.events.remove(ev)
                    return ev
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"Timeout waiting for {action}; events: {self.events}"
                )
            self._received.clear()
            try:
                await asyncio.wait_for(self._received.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise AssertionError(
                    f"Timeout waiting for {action}; events: {self.events}"
                )


@pytest.fixture
def backend_url() -> str:
    return os.environ.get("BACKEND_E2E_URL", "http://backend:8000")


@pytest.fixture
async def conversation(backend_url):
    sio = socketio.AsyncClient(reconnection=False)
    convo = SocketConversation(sio)
    await sio.connect(backend_url, socketio_path="/socket.io")
    try:
        await convo.send({"action": "init"})
        await convo.wait_for_action("init_data", timeout=15)
        yield convo
    finally:
        await sio.disconnect()


RU_WITH_ROLES = (
    "Согласование заявки: Менеджер создаёт заявку. Директор рассматривает "
    "и утверждает. Менеджер отправляет клиенту результат."
)


class TestLanesFullStack:
    async def test_russian_with_roles_produces_lanes(self, conversation):
        await conversation.send(
            {"action": "message", "session_id": None, "text": RU_WITH_ROLES}
        )
        result = await conversation.wait_for_action("result", timeout=180)
        xml = result.get("bpmn_xml", "")
        assert xml, f"Empty bpmn_xml: {result}"

        lanes = extract_lanes(xml)
        assert len(lanes) >= 2, (
            f"Expected ≥2 lanes end-to-end, got {len(lanes)}: {list(lanes)}"
        )
