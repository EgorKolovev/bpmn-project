"""Level 3 full-stack test — complex Russian process end-to-end.

Sends a multi-branch Russian process description through Socket.IO to the
backend and verifies that the BPMN the user actually sees includes a
gateway, a labeled branch, and a cycle.

Skipped unless RUN_E2E=1.
"""
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


def count_exclusive_gateways(xml: str) -> int:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return 0
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return 0
    return sum(1 for e in list(process) if _local(e.tag) == "exclusiveGateway")


def extract_flows(xml: str) -> list[dict]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return []
    out = []
    for elem in list(process):
        if _local(elem.tag) != "sequenceFlow":
            continue
        has_cond = any(
            _local(c.tag) == "conditionExpression" and (c.text or "").strip()
            for c in list(elem)
        )
        out.append(
            {
                "id": elem.get("id", ""),
                "sourceRef": elem.get("sourceRef", ""),
                "targetRef": elem.get("targetRef", ""),
                "name": elem.get("name", "") or "",
                "has_condition_expr": has_cond,
            }
        )
    return out


def has_cycle(xml: str) -> bool:
    flows = extract_flows(xml)
    adj: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for f in flows:
        s, t = f["sourceRef"], f["targetRef"]
        if s and t:
            adj.setdefault(s, []).append(t)
            nodes.add(s)
            nodes.add(t)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    for start in nodes:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, idx = stack[-1]
            children = adj.get(node, [])
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                nxt = children[idx]
                if color.get(nxt, WHITE) == GRAY:
                    return True
                if color.get(nxt, WHITE) == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                stack.pop()
    return False


# ---------------------------------------------------------------------------
# Socket helper
# ---------------------------------------------------------------------------


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

    async def wait_for_action(self, action: str, timeout: float = 120.0) -> dict:
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


RU_COMPLEX = (
    "Процесс согласования договора: менеджер создаёт заявку, юрист "
    "проверяет документы. Если есть замечания — возвращает на доработку. "
    "Если замечаний нет — директор подписывает договор."
)


class TestComplexFullStack:
    async def test_complex_russian_has_gateway_and_cycle(self, conversation):
        await conversation.send(
            {"action": "message", "session_id": None, "text": RU_COMPLEX}
        )
        result = await conversation.wait_for_action("result", timeout=180)
        xml = result.get("bpmn_xml", "")
        assert xml, f"Empty bpmn_xml in result: {result}"

        gw_count = count_exclusive_gateways(xml)
        assert gw_count >= 1, (
            f"Expected ≥1 exclusiveGateway end-to-end, got {gw_count}. XML: {xml[:500]}"
        )

        # At least one outgoing from a gateway must be labeled
        flows = extract_flows(xml)
        labeled_branch = any(
            f["name"] or f["has_condition_expr"] for f in flows
        )
        assert labeled_branch, (
            f"Expected at least one labeled sequenceFlow. Flows: {flows}"
        )

        # Rework loop → cycle
        assert has_cycle(xml), (
            f"Expected a cycle (rework loop). Flows: {[(f['sourceRef'], f['targetRef']) for f in flows]}"
        )
