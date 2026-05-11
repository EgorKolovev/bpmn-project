"""Shared test fixtures, helpers, and constants for the ml service.

Organized in sections:

  * **Constants** — timeouts, RUN_E2E flag, sample process descriptions
    that several test modules consume.
  * **Sample XML** — `VALID_BPMN_XML*` used by unit tests of the fix
    passes / validator / layouter.
  * **i18n helpers** — `cyrillic_ratio`, `all_names_are_cyrillic`, …
  * **Structural helpers** — element counts, gateway / lane / cycle
    extraction. Pure-function utilities re-used across unit tests
    and the slim E2E layer.
  * **Fixtures** — `ml_client` async httpx client (for E2E),
    `_classify / _generate / _edit` helper coroutines that all E2E
    tests call.

Anything that *can* be a plain function lives here as one, so test
modules stay focused on assertions.
"""
import asyncio
import os
import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from defusedxml import ElementTree as ET


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Set RUN_E2E=1 to enable the slim real-LLM E2E layer. Without it the
#: E2E module is skipped at collection time.
RUN_E2E = os.environ.get("RUN_E2E", "0") == "1"


#: Centralised timeouts (seconds). Touched from multiple test modules,
#: so a single change here propagates everywhere.
TIMEOUTS = SimpleNamespace(
    # Per-LLM-call upper bound. Matches `GeminiBackend.http_client.timeout`
    # in `ml/app/llm.py`. Stretchy enough for a 4096-token thinking pass
    # plus a 32 KB BPMN payload.
    LLM_CALL=180.0,
    # Outer HTTP client used by /classify, /generate, /edit E2E tests.
    # Slightly bigger than LLM_CALL so an internal retry can fit before
    # we surface a timeout.
    ML_HTTP=200.0,
    # Tighter window for cheap calls (classify is small).
    CLASSIFY=30.0,
    # Used by Socket.IO `wait_for_action` to give the server room to
    # round-trip through ml → LLM → back.
    SOCKETIO_EVENT=90.0,
    # Initial Socket.IO handshake + init_data round-trip.
    SOCKETIO_INIT=15.0,
)


#: pytest-rerunfailures retry knobs for the slim E2E layer.
E2E_RERUNS = 2
E2E_RERUN_DELAY = 2

#: Shared marker stack for E2E tests — applied to `pytestmark` at the
#: module top level. Combines `@pytest.mark.e2e` (so we can `-m e2e` or
#: `-m "not e2e"`), the env-gate, and flaky retries.
E2E_MARKERS = [
    pytest.mark.e2e,
    pytest.mark.skipif(not RUN_E2E, reason="Set RUN_E2E=1 to run E2E tests"),
    pytest.mark.flaky(reruns=E2E_RERUNS, reruns_delay=E2E_RERUN_DELAY),
]


# ---------------------------------------------------------------------------
# Sample process descriptions used across the integration + E2E layers.
# Kept as module-level constants so a single edit propagates everywhere
# and so test files can be diffed without hunting for inline strings.
# ---------------------------------------------------------------------------

RU_VALID_PROCESS = (
    "Процесс согласования договора: менеджер создаёт заявку, "
    "юрист проверяет документы, директор подписывает."
)

RU_PROCESS_WITH_REWORK = (
    "Контракт: менеджер создаёт заявку, юрист проверяет. Если есть "
    "замечания — возвращает на доработку, иначе директор подписывает."
)

RU_PROCESS_WITH_LANES = (
    "Согласование заявки: Менеджер создаёт заявку. Директор рассматривает "
    "и принимает решение. Если одобрено — менеджер отправляет клиенту. "
    "Если отклонено — менеджер уведомляет клиента об отказе."
)

EN_LINEAR_PROCESS = (
    "Order fulfillment: customer places order, payment is verified, "
    "item is shipped to the customer."
)

EN_INVALID_GARBAGE = "What is the weather today?"
RU_INVALID_GARBAGE = "Какая сегодня погода?"


VALID_BPMN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
                  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
                  xmlns:di="http://www.omg.org/spec/DD/20100524/DI"
                  id="Definitions_1"
                  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:startEvent id="Start_1" name="Start">
      <bpmn:outgoing>Flow_1</bpmn:outgoing>
    </bpmn:startEvent>
    <bpmn:task id="Task_1" name="Receive Application">
      <bpmn:incoming>Flow_1</bpmn:incoming>
      <bpmn:outgoing>Flow_2</bpmn:outgoing>
    </bpmn:task>
    <bpmn:task id="Task_2" name="Review Application">
      <bpmn:incoming>Flow_2</bpmn:incoming>
      <bpmn:outgoing>Flow_3</bpmn:outgoing>
    </bpmn:task>
    <bpmn:endEvent id="End_1" name="End">
      <bpmn:incoming>Flow_3</bpmn:incoming>
    </bpmn:endEvent>
    <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1"/>
    <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="Task_2"/>
    <bpmn:sequenceFlow id="Flow_3" sourceRef="Task_2" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""


# Version without BPMNDiagram - matches what the LLM should produce
VALID_BPMN_XML_NO_DI = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  id="Definitions_1"
                  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:startEvent id="Start_1" name="Start">
      <bpmn:outgoing>Flow_1</bpmn:outgoing>
    </bpmn:startEvent>
    <bpmn:task id="Task_1" name="Receive Application">
      <bpmn:incoming>Flow_1</bpmn:incoming>
      <bpmn:outgoing>Flow_2</bpmn:outgoing>
    </bpmn:task>
    <bpmn:task id="Task_2" name="Review Application">
      <bpmn:incoming>Flow_2</bpmn:incoming>
      <bpmn:outgoing>Flow_3</bpmn:outgoing>
    </bpmn:task>
    <bpmn:endEvent id="End_1" name="End">
      <bpmn:incoming>Flow_3</bpmn:incoming>
    </bpmn:endEvent>
    <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1"/>
    <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="Task_2"/>
    <bpmn:sequenceFlow id="Flow_3" sourceRef="Task_2" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""


# ---------------------------------------------------------------------------
# i18n helpers used by Level 2/3 tests
# ---------------------------------------------------------------------------

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Cyrillic letter ranges (covers Russian + other Cyrillic scripts)
_CYRILLIC_RANGES = [
    (0x0400, 0x04FF),  # Cyrillic
    (0x0500, 0x052F),  # Cyrillic Supplement
    (0x2DE0, 0x2DFF),  # Cyrillic Extended-A
    (0xA640, 0xA69F),  # Cyrillic Extended-B
]


def _is_cyrillic(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CYRILLIC_RANGES)


def cyrillic_ratio(s: str) -> float:
    """Share of Cyrillic letters among all letters. Non-letters ignored.

    Returns 0.0 for an empty / letter-less string.
    """
    if not s:
        return 0.0
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    cyr = sum(1 for c in letters if _is_cyrillic(c))
    return cyr / len(letters)


def extract_flow_node_names(xml_string: str) -> list[str]:
    """Return all `name` attributes on flow nodes (non-empty only).

    Excludes sequenceFlow (which is a non-flow-node). Parses with defusedxml.
    """
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return []

    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return []

    names: list[str] = []
    for elem in list(process):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag == "sequenceFlow":
            continue
        name = elem.get("name")
        if name:
            names.append(name)
    return names


def extract_sequence_flow_names(xml_string: str) -> list[str]:
    """Return `name` attributes on sequenceFlow elements (branch labels)."""
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return []

    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return []

    names: list[str] = []
    for elem in list(process):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag != "sequenceFlow":
            continue
        name = elem.get("name")
        if name:
            names.append(name)
    return names


def all_names_are_cyrillic(xml_string: str, min_ratio: float = 0.7) -> tuple[bool, list[str]]:
    """True iff every flow-node name has cyrillic_ratio >= min_ratio.

    Returns (passes, offending_names). Empty XML / no names → (False, []).
    """
    names = extract_flow_node_names(xml_string)
    if not names:
        return False, []
    offenders = [n for n in names if cyrillic_ratio(n) < min_ratio]
    return len(offenders) == 0, offenders


def all_names_are_latin(xml_string: str, max_cyr_ratio: float = 0.1) -> tuple[bool, list[str]]:
    """True iff every flow-node name has cyrillic_ratio <= max_cyr_ratio."""
    names = extract_flow_node_names(xml_string)
    if not names:
        return False, []
    offenders = [n for n in names if cyrillic_ratio(n) > max_cyr_ratio]
    return len(offenders) == 0, offenders


# ---------------------------------------------------------------------------
# Structural helpers for complex-process tests (Task 2)
# ---------------------------------------------------------------------------


def _parse_process(xml_string: str):
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return None
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    return process


def count_elements_by_tag(xml_string: str, tag_name: str) -> int:
    process = _parse_process(xml_string)
    if process is None:
        return 0
    return sum(
        1
        for elem in list(process)
        if (elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag) == tag_name
    )


def count_exclusive_gateways(xml_string: str) -> int:
    return count_elements_by_tag(xml_string, "exclusiveGateway")


def count_parallel_gateways(xml_string: str) -> int:
    return count_elements_by_tag(xml_string, "parallelGateway")


def extract_sequence_flows(xml_string: str) -> list[dict]:
    """Return [{id, sourceRef, targetRef, name, has_condition_expr}] for every
    sequenceFlow in the process."""
    process = _parse_process(xml_string)
    if process is None:
        return []
    out = []
    for elem in list(process):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag != "sequenceFlow":
            continue
        has_cond = False
        for child in list(elem):
            ctag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if ctag == "conditionExpression" and (child.text or "").strip():
                has_cond = True
                break
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


def exclusive_gateway_ids(xml_string: str) -> set[str]:
    process = _parse_process(xml_string)
    if process is None:
        return set()
    ids: set[str] = set()
    for elem in list(process):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag == "exclusiveGateway":
            if eid := elem.get("id"):
                ids.add(eid)
    return ids


def has_labeled_branch_from_gateway(xml_string: str) -> bool:
    """True iff at least one exclusiveGateway has a named or conditionalised
    outgoing sequenceFlow."""
    gw_ids = exclusive_gateway_ids(xml_string)
    if not gw_ids:
        return False
    for flow in extract_sequence_flows(xml_string):
        if flow["sourceRef"] in gw_ids and (flow["name"] or flow["has_condition_expr"]):
            return True
    return False


def extract_lanes(xml_string: str) -> dict[str, list[str]]:
    """Return {lane_name: [flow_node_ids]} for every lane in the process."""
    process = _parse_process(xml_string)
    if process is None:
        return {}
    out: dict[str, list[str]] = {}
    for child in list(process):
        tag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
        if tag != "laneSet":
            continue
        for lane in list(child):
            ltag = lane.tag.split("}", 1)[-1] if "}" in lane.tag else lane.tag
            if ltag != "lane":
                continue
            name = lane.get("name", "") or lane.get("id", "")
            refs = []
            for ref in list(lane):
                rtag = ref.tag.split("}", 1)[-1] if "}" in ref.tag else ref.tag
                if rtag == "flowNodeRef":
                    rid = (ref.text or "").strip()
                    if rid:
                        refs.append(rid)
            out[name] = refs
    return out


def has_lanes(xml_string: str) -> bool:
    return bool(extract_lanes(xml_string))


def all_flow_nodes_in_lanes(xml_string: str) -> tuple[bool, list[str]]:
    """Given a process with lanes, verify every flow node has exactly one
    flowNodeRef. Returns (ok, missing_or_duplicate_ids)."""
    lanes = extract_lanes(xml_string)
    if not lanes:
        return False, []
    process = _parse_process(xml_string)
    if process is None:
        return False, []
    flow_node_ids: list[str] = []
    for elem in list(process):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag in {
            "sequenceFlow", "laneSet", "messageFlow", "association",
            "dataObject", "dataObjectReference", "textAnnotation",
            "documentation", "extensionElements", "ioSpecification",
        }:
            continue
        if eid := elem.get("id"):
            flow_node_ids.append(eid)

    assignments: dict[str, int] = {nid: 0 for nid in flow_node_ids}
    for refs in lanes.values():
        for rid in refs:
            if rid in assignments:
                assignments[rid] += 1
    bad = [nid for nid, cnt in assignments.items() if cnt != 1]
    return len(bad) == 0, bad


def has_cycle(xml_string: str) -> bool:
    """Detect whether the process graph contains any cycle (back-edge).

    Uses iterative DFS with three-coloring. flow-nodes are vertices,
    sequenceFlows are edges from sourceRef → targetRef.
    """
    flows = extract_sequence_flows(xml_string)
    if not flows:
        return False
    adj: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for f in flows:
        s, t = f["sourceRef"], f["targetRef"]
        if not s or not t:
            continue
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
                    return True  # back-edge → cycle
                if color.get(nxt, WHITE) == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                stack.pop()
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_bpmn_xml():
    return VALID_BPMN_XML


@pytest.fixture
def ml_base_url() -> str:
    """Base URL of the running ML service (for E2E tests)."""
    return os.environ.get("ML_E2E_URL", "http://ml:8001")


@pytest.fixture
def backend_base_url() -> str:
    """Base URL of the running backend service (for full-stack E2E)."""
    return os.environ.get("BACKEND_E2E_URL", "http://backend:8000")


@pytest.fixture
def internal_api_key() -> str:
    """Shared secret for backend → ml calls. Tests skip if unset."""
    return os.environ.get("INTERNAL_API_KEY", "")


# ---------------------------------------------------------------------------
# E2E HTTP helpers
#
# `ml_client` and the `_classify / _generate / _edit` coroutines below
# used to live duplicated across `test_complex_e2e.py`, `test_i18n_e2e.py`
# and `test_lanes_e2e.py`. Centralised here so the slim E2E layer can
# `from tests.conftest import _classify, _generate, _edit, ml_client`
# and stay focused on assertions.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ml_client(ml_base_url, internal_api_key):
    """An async httpx client preconfigured with the right base URL and
    X-Internal-Api-Key header.

    Skips the test (rather than erroring) when INTERNAL_API_KEY isn't
    set — keeps E2E opt-in clean.
    """
    if not internal_api_key:
        pytest.skip("INTERNAL_API_KEY env var not set; skipping E2E test.")
    headers = {"X-Internal-Api-Key": internal_api_key}
    async with httpx.AsyncClient(
        base_url=ml_base_url,
        headers=headers,
        timeout=TIMEOUTS.ML_HTTP,
    ) as client:
        yield client


async def _classify(client: httpx.AsyncClient, text: str) -> dict[str, Any]:
    resp = await client.post("/classify", json={"text": text}, timeout=TIMEOUTS.CLASSIFY)
    assert resp.status_code == 200, (
        f"/classify failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


async def _generate(client: httpx.AsyncClient, description: str) -> dict[str, Any]:
    resp = await client.post(
        "/generate",
        json={"description": description},
        timeout=TIMEOUTS.LLM_CALL,
    )
    assert resp.status_code == 200, (
        f"/generate failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


async def _edit(
    client: httpx.AsyncClient, prompt: str, bpmn_xml: str
) -> dict[str, Any]:
    resp = await client.post(
        "/edit",
        json={"prompt": prompt, "bpmn_xml": bpmn_xml},
        timeout=TIMEOUTS.LLM_CALL,
    )
    assert resp.status_code == 200, (
        f"/edit failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


# ---------------------------------------------------------------------------
# Mock-LLM transport — used by Layer-2C integration tests in
# `test_integration_llm.py` to feed canned responses to `GeminiBackend`
# / `PolzaBackend` without touching the real Google / Polza APIs.
#
# Tests build a `MockTransport`-style callable that maps the request
# path to the response shape they want to test against, then inject it
# into the backend via:
#
#     monkeypatch.setattr(backend, "http_client",
#                         httpx.AsyncClient(transport=MockTransport(fn)))
#
# The two helper factories below cover the most common cases.
# ---------------------------------------------------------------------------


def make_gemini_response(
    *,
    text: str,
    prompt_tokens: int = 100,
    output_tokens: int = 200,
    thoughts_tokens: int = 50,
    finish_reason: str = "STOP",
) -> dict:
    """Build a Gemini v1beta `generateContent` response body."""
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "finishReason": finish_reason,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": output_tokens,
            "thoughtsTokenCount": thoughts_tokens,
            "totalTokenCount": prompt_tokens + output_tokens + thoughts_tokens,
        },
    }


def make_gemini_count_response(total_tokens: int = 100) -> dict:
    """Build a Gemini `countTokens` response body."""
    return {"totalTokens": total_tokens}


def make_polza_response(
    *,
    content: str,
    prompt_tokens: int = 100,
    completion_tokens: int = 200,
    reasoning_tokens: int = 50,
) -> dict:
    """Build a Polza (OpenAI-compatible) `chat/completions` response body."""
    return {
        "id": "test-gen-1",
        "model": "google/gemini-3-flash-preview",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "completion_tokens_details": {
                "reasoning_tokens": reasoning_tokens,
                "audio_tokens": 0,
                "image_tokens": 0,
            },
        },
    }


def make_mock_transport(handlers) -> httpx.MockTransport:
    """Adapter around `httpx.MockTransport` that lets tests supply a
    simple `{path_suffix: response_dict}` mapping instead of having to
    write a full request handler.

    A handler can be:
      * a `dict` → returned as 200 OK JSON;
      * an `(status, dict)` tuple → returned as `status` JSON;
      * a callable `(request: httpx.Request) -> httpx.Response` → invoked.

    Passing a callable directly (not a dict) is also supported — it just
    receives the request and returns the response unmodified.
    """

    if callable(handlers):
        return httpx.MockTransport(handlers)

    def _route(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for needle, response in handlers.items():
            if needle in url:
                if callable(response):
                    return response(request)
                if isinstance(response, tuple) and len(response) == 2:
                    status, body = response
                    return httpx.Response(status, json=body)
                return httpx.Response(200, json=response)
        return httpx.Response(404, json={"error": f"no handler for {url}"})

    return httpx.MockTransport(_route)
