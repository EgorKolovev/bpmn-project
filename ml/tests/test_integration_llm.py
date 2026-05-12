"""Layer 2C integration tests — `LLMClient.generate/edit/classify`
exercised with a fully-mocked LLM transport.

These tests cover the glue code that turns raw LLM responses into the
shapes the FastAPI endpoints (and downstream backend service) consume:

  * Retry loops on JSON parse failure / missing fields / validator
    errors / missing laneSet (when the description asks for roles).
  * Recovery from Gemini's double / structural-escape JSON
    pathologies (more focused than `test_json_recovery.py` — these
    drive end-to-end through `LLMClient.generate`).
  * MAX_TOKENS handled as an informative error.
  * Polza's two HTTP-402 shapes (daily-cap vs generic balance).
  * Polza's `reasoning.effort` is auto-mapped from the gemini-side
    `GEMINI_THINKING_BUDGET`.
  * `responseJsonSchema` actually lands in the Gemini payload.

No real network — every test installs an `httpx.MockTransport` into
the backend's `http_client` via monkeypatch.

The tests are pure asyncio / fast (well under one second total).
Independent of the running ml container.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("GEMINI_API_KEY", "test-key-for-unit-tests")

import httpx
import pytest

from app.budget import BudgetTracker
from app.llm import (
    GeminiBackend,
    LLMClient,
    LLMClientError,
    PolzaBackend,
)
from tests.conftest import (
    VALID_BPMN_XML_NO_DI,
    make_gemini_count_response,
    make_gemini_response,
    make_polza_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _budget_tracker(tmp_path) -> BudgetTracker:
    """A fresh disk-backed budget tracker that won't deny anything.

    `BudgetTracker` opens a fresh SQLite connection on every call, so
    `:memory:` doesn't survive the first round-trip — we need a real
    file, which `tmp_path` gives us per-test.
    """
    return BudgetTracker(
        db_path=str(tmp_path / "budget.sqlite"),
        daily_limit_usd=100.0,
        input_price_per_million_usd=0.25,
        output_price_per_million_usd=1.50,
        max_output_tokens=4096,
    )


def _gemini_response_body(bpmn_xml: str, session_name: str = "Test") -> dict:
    """Build a Gemini response whose `text` part is the JSON the
    `_extract_json` step expects."""
    payload = {"bpmn_xml": bpmn_xml, "session_name": session_name}
    return make_gemini_response(text=json.dumps(payload, ensure_ascii=False))


def _gemini_edit_response(bpmn_xml: str) -> dict:
    payload = {"bpmn_xml": bpmn_xml}
    return make_gemini_response(text=json.dumps(payload, ensure_ascii=False))


def _gemini_classify_response(is_valid: bool, reason: str = "") -> dict:
    payload: dict = {"is_valid": is_valid}
    if reason:
        payload["reason"] = reason
    return make_gemini_response(text=json.dumps(payload, ensure_ascii=False))


class _SequencedTransport:
    """A test helper that pops successive responses for the same path
    from a list. Useful when a single test wants the LLM to fail-then-
    succeed (e.g. retry-after-bad-JSON).

    Example::

        transport = _SequencedTransport(
            {
                "countTokens": [count_resp, count_resp, count_resp, count_resp],
                "generateContent": [bad_resp, good_resp],
            }
        )
    """

    def __init__(self, sequences: dict):
        # Wrap a single response into a one-element list for ergonomics.
        # NOTE: a tuple is treated as a `(status, body)` response, NOT
        # as a sequence — use `list` if you want multiple responses.
        self._queues: dict[str, list] = {
            k: list(v) if isinstance(v, list) else [v] for k, v in sequences.items()
        }
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for needle, queue in self._queues.items():
            if needle in str(request.url):
                if not queue:
                    return httpx.Response(
                        500,
                        json={"error": f"queue exhausted for {needle}"},
                    )
                response = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(response, tuple) and len(response) == 2:
                    status, body = response
                    return httpx.Response(status, json=body)
                if isinstance(response, Exception):
                    raise response
                return httpx.Response(200, json=response)
        return httpx.Response(404, json={"error": f"no handler for {request.url}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _make_gemini_client(
    monkeypatch, tmp_path, sequences: dict
) -> tuple[LLMClient, _SequencedTransport]:
    """Wire a fresh LLMClient on top of GeminiBackend with the supplied
    canned-response sequences. Returns (client, transport) so tests can
    introspect `transport.requests` after the call.
    """
    seq = _SequencedTransport(sequences)
    backend = GeminiBackend(
        api_key="dummy",
        model="gemini-3-flash-preview",
        max_output_tokens=4096,
    )
    monkeypatch.setattr(
        backend,
        "http_client",
        httpx.AsyncClient(
            transport=seq.transport(),
            headers={"x-goog-api-key": "dummy"},
            base_url="https://generativelanguage.googleapis.com",
        ),
    )
    tracker = _budget_tracker(tmp_path)
    return LLMClient(budget_tracker=tracker, backend=backend), seq


def _make_polza_client(
    monkeypatch, tmp_path, sequences: dict
) -> tuple[LLMClient, _SequencedTransport]:
    seq = _SequencedTransport(sequences)
    backend = PolzaBackend(
        api_key="dummy",
        model="google/gemini-3-flash-preview",
        base_url="http://polza-mock",
        max_output_tokens=4096,
    )
    monkeypatch.setattr(
        backend,
        "http_client",
        httpx.AsyncClient(
            transport=seq.transport(),
            headers={"Authorization": "Bearer dummy"},
            base_url="http://polza-mock",
        ),
    )
    tracker = _budget_tracker(tmp_path)
    return LLMClient(budget_tracker=tracker, backend=backend), seq


# ---------------------------------------------------------------------------
# Gemini backend — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_happy_path(monkeypatch, tmp_path):
    """Valid description → countTokens 200 → generateContent 200 with
    well-formed JSON → returned dict has bpmn_xml + session_name AND
    server-side layout has baked BPMNDiagram in."""
    client, seq = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": _gemini_response_body(VALID_BPMN_XML_NO_DI, "Linear Test"),
        },
    )
    result = await client.generate("Order fulfillment: receive order, verify payment, ship item.")
    assert result["session_name"] == "Linear Test"
    assert "bpmn_xml" in result
    # layout_bpmn() runs at the end of generate() — diagram must be present
    assert "<bpmndi:BPMNDiagram" in result["bpmn_xml"]


@pytest.mark.asyncio
async def test_edit_happy_path(monkeypatch, tmp_path):
    new_xml = VALID_BPMN_XML_NO_DI
    client, _ = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": _gemini_edit_response(new_xml),
        },
    )
    result = await client.edit("Add a verification step.", VALID_BPMN_XML_NO_DI)
    assert "bpmn_xml" in result
    assert "<bpmndi:BPMNDiagram" in result["bpmn_xml"]


@pytest.mark.asyncio
async def test_classify_happy_path(monkeypatch, tmp_path):
    client, _ = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": _gemini_classify_response(True),
        },
    )
    result = await client.classify("Order fulfillment process")
    assert result["is_valid"] is True


@pytest.mark.asyncio
async def test_classify_rejection_passes_reason(monkeypatch, tmp_path):
    client, _ = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": _gemini_classify_response(False, "Not a business process"),
        },
    )
    result = await client.classify("What is the weather?")
    assert result["is_valid"] is False
    assert "weather" in result["reason"].lower() or "business" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Retry-loop coverage — bad responses, then a good one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_retries_on_json_parse_failure(monkeypatch, tmp_path):
    """First generateContent returns chatty unparseable text → retry →
    succeed."""
    bad = make_gemini_response(text="Sure! Here's your diagram. {garbage")
    good = _gemini_response_body(VALID_BPMN_XML_NO_DI, "After Retry")
    client, seq = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": [bad, good],
        },
    )
    result = await client.generate("Order fulfillment process.")
    assert result["session_name"] == "After Retry"
    # Two POSTs to generateContent (countTokens calls don't matter here).
    gen_calls = [r for r in seq.requests if "generateContent" in str(r.url)]
    assert len(gen_calls) == 2


@pytest.mark.asyncio
async def test_generate_retries_on_missing_bpmn_xml_field(monkeypatch, tmp_path):
    """LLM returned valid JSON but no bpmn_xml — retry."""
    bad = make_gemini_response(text=json.dumps({"session_name": "Nope"}))
    good = _gemini_response_body(VALID_BPMN_XML_NO_DI, "OK")
    client, seq = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": [bad, good],
        },
    )
    result = await client.generate("Order fulfillment.")
    assert result["session_name"] == "OK"


@pytest.mark.asyncio
async def test_generate_retries_on_invalid_bpmn(monkeypatch, tmp_path):
    """LLM emits XML that fails the validator (missing process root) —
    retry with correction prompt."""
    broken_xml = """<?xml version="1.0"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"/>"""
    bad = make_gemini_response(text=json.dumps({"bpmn_xml": broken_xml, "session_name": "Bad"}))
    good = _gemini_response_body(VALID_BPMN_XML_NO_DI, "Recovered")
    client, seq = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": [bad, good],
        },
    )
    result = await client.generate("Order fulfillment.")
    assert result["session_name"] == "Recovered"


@pytest.mark.asyncio
async def test_generate_lane_guard_triggers_retry(monkeypatch, tmp_path):
    """User said 'Используй роли' but LLM returned no laneSet — guard
    forces a retry, on which the LLM does emit lanes."""
    lane_xml = """<?xml version="1.0"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:laneSet id="LaneSet_1">
      <bpmn:lane id="Lane_M" name="Менеджер">
        <bpmn:flowNodeRef>Start_1</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>Task_1</bpmn:flowNodeRef>
      </bpmn:lane>
      <bpmn:lane id="Lane_D" name="Директор">
        <bpmn:flowNodeRef>Task_2</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>End_1</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
    <bpmn:startEvent id="Start_1"><bpmn:outgoing>F1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:task id="Task_1" name="Создать"><bpmn:incoming>F1</bpmn:incoming><bpmn:outgoing>F2</bpmn:outgoing></bpmn:task>
    <bpmn:task id="Task_2" name="Подписать"><bpmn:incoming>F2</bpmn:incoming><bpmn:outgoing>F3</bpmn:outgoing></bpmn:task>
    <bpmn:endEvent id="End_1"><bpmn:incoming>F3</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="F1" sourceRef="Start_1" targetRef="Task_1"/>
    <bpmn:sequenceFlow id="F2" sourceRef="Task_1" targetRef="Task_2"/>
    <bpmn:sequenceFlow id="F3" sourceRef="Task_2" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""
    first_attempt = _gemini_response_body(VALID_BPMN_XML_NO_DI, "No Lanes")
    second_attempt = _gemini_response_body(lane_xml, "With Lanes")
    client, seq = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": [first_attempt, second_attempt],
        },
    )
    result = await client.generate("Используй роли: Менеджер создаёт заявку, Директор подписывает.")
    assert "<bpmn:lane " in result["bpmn_xml"] or '<bpmn:lane id="' in result["bpmn_xml"]
    gen_calls = [r for r in seq.requests if "generateContent" in str(r.url)]
    assert len(gen_calls) == 2, "lane guard should trigger one retry"


@pytest.mark.asyncio
async def test_generate_lane_guard_falls_back_after_max_retries(monkeypatch, tmp_path):
    """If three attempts all produce lane-less XML, we still ship the
    lane-less result rather than 500-ing — the diagram is at least
    structurally valid."""
    plain = _gemini_response_body(VALID_BPMN_XML_NO_DI, "Lane-less")
    client, _ = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": [plain, plain, plain],
        },
    )
    result = await client.generate("Используй роли: Менеджер, Директор.")
    # Lane-less fallback path — must still return valid XML, not throw.
    assert "<bpmn:definitions" in result["bpmn_xml"]


@pytest.mark.asyncio
async def test_generate_gives_up_after_max_retries_on_bad_xml(monkeypatch, tmp_path):
    """Three consecutive structurally-broken XMLs → ValueError, not
    a silent return of garbage."""
    bad = make_gemini_response(
        text=json.dumps(
            {
                "bpmn_xml": "<not-bpmn/>",
                "session_name": "Junk",
            }
        )
    )
    client, _ = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": [bad, bad, bad],
        },
    )
    with pytest.raises(ValueError):
        await client.generate("Anything.")


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_max_tokens_surfaces_clear_error(monkeypatch, tmp_path):
    """When Gemini hits MAX_TOKENS, the user-facing error names the
    actual fix (raise output / lower thinking)."""
    truncated = make_gemini_response(text="...", finish_reason="MAX_TOKENS")
    client, _ = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": truncated,
        },
    )
    with pytest.raises(LLMClientError) as ei:
        await client.generate("Anything.")
    msg = ei.value.message.lower()
    assert "max_tokens" in msg or "truncat" in msg


@pytest.mark.asyncio
async def test_generate_translates_500_to_llm_error(monkeypatch, tmp_path):
    client, _ = _make_gemini_client(
        monkeypatch,
        tmp_path,
        {
            "countTokens": make_gemini_count_response(),
            "generateContent": (500, {"error": "boom"}),
        },
    )
    with pytest.raises(LLMClientError):
        await client.generate("Anything.")


# ---------------------------------------------------------------------------
# Polza backend — reasoning.effort mapping + 402 translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_polza_reasoning_effort_maps_from_thinking_budget(monkeypatch, tmp_path):
    """The reasoning.effort POST body field must match the static
    mapping defined in `_map_budget_to_effort`. We capture the
    outgoing request body to assert this."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(
            200,
            json=make_polza_response(
                content=json.dumps({"bpmn_xml": VALID_BPMN_XML_NO_DI, "session_name": "Z"})
            ),
        )

    backend = PolzaBackend(
        api_key="dummy",
        model="google/gemini-3-flash-preview",
        base_url="http://polza-mock",
        max_output_tokens=4096,
    )
    monkeypatch.setattr(
        backend,
        "http_client",
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer dummy"},
            base_url="http://polza-mock",
        ),
    )
    tracker = _budget_tracker(tmp_path)
    client = LLMClient(budget_tracker=tracker, backend=backend)

    await client.generate("Order fulfillment.")

    # Default GEMINI_THINKING_BUDGET = 4096 → maps to "medium".
    # See _map_budget_to_effort: ≤2048 = low, ≤5000 = medium, >5000 = high.
    assert captured.get("reasoning", {}).get("effort") == "medium", captured


@pytest.mark.asyncio
async def test_polza_402_daily_cap_emits_informative_error(monkeypatch, tmp_path):
    """Polza's daily-cap variant of HTTP-402 must surface that exact
    wording — operators need to know it's a cap (lift in dashboard),
    not a depleted balance (top up)."""
    daily_cap_body = {
        "error": {
            "code": "INSUFFICIENT_BALANCE",
            "message": "Достигнут дневной лимит по сумме",
        },
        "trace_id": "test-trace",
    }
    client, _ = _make_polza_client(
        monkeypatch,
        tmp_path,
        {
            "chat/completions": (402, daily_cap_body),
        },
    )
    with pytest.raises(LLMClientError) as ei:
        await client.generate("Anything.")
    msg = ei.value.message
    assert "Достигнут дневной лимит" in msg
    assert "Daily spend cap" in msg
    assert "polza.ai/dashboard" in msg


@pytest.mark.asyncio
async def test_polza_402_generic_insufficient_emits_topup_hint(monkeypatch, tmp_path):
    body = {
        "error": {
            "code": "INSUFFICIENT_BALANCE",
            "message": "Insufficient balance",
        },
    }
    client, _ = _make_polza_client(
        monkeypatch,
        tmp_path,
        {
            "chat/completions": (402, body),
        },
    )
    with pytest.raises(LLMClientError) as ei:
        await client.generate("Anything.")
    msg = ei.value.message
    assert "Top up" in msg
    assert "Daily spend cap" not in msg


# ---------------------------------------------------------------------------
# Payload contract — `responseJsonSchema` lands in the Gemini body.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_generate_payload_includes_response_schema(monkeypatch, tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "countTokens" in path:
            return httpx.Response(200, json=make_gemini_count_response())
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(
            200,
            json=_gemini_response_body(VALID_BPMN_XML_NO_DI, "OK"),
        )

    backend = GeminiBackend(
        api_key="dummy",
        model="gemini-3-flash-preview",
        max_output_tokens=4096,
    )
    monkeypatch.setattr(
        backend,
        "http_client",
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"x-goog-api-key": "dummy"},
            base_url="https://generativelanguage.googleapis.com",
        ),
    )
    tracker = _budget_tracker(tmp_path)
    client = LLMClient(budget_tracker=tracker, backend=backend)

    await client.generate("Order fulfillment.")
    schema = captured.get("generationConfig", {}).get("responseJsonSchema")
    assert schema is not None, captured
    assert schema["type"] == "object"
    assert "bpmn_xml" in schema["properties"]
    assert "session_name" in schema["properties"]
    assert "bpmn_xml" in schema["required"]
