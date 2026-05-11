"""Slim Layer-3 E2E suite — the only tests that talk to a real LLM.

Replaces the original `test_complex_e2e.py` + `test_i18n_e2e.py` +
`test_lanes_e2e.py` (≈ 29 tests, 25 min wall-clock, ~$0.50/run).

Six tests kept here cover the contract the slice is actually
defending in production:

  1. **Russian generate (simple)** — produces a diagram with Russian
     `name` attributes. The product's most important promise.
  2. **Russian generate with lanes (PDF1 командировка)** — real
     customer spec, ≥3 lanes, ≥5 gateways, has cycle (rework loop).
     This is the spec that broke flash-lite-preview earlier and
     drove the move to gemini-3-flash-preview.
  3. **Russian edit** — given a Russian diagram, an English
     modification instruction should still produce Russian labels.
     The most subtle prompt behavior we promise.
  4. **Classify rejects garbage on generate** — `/generate` is
     gated by `/classify`; weather questions never reach the LLM.
  5. **Classify rejects garbage on edit-attempt** — same gate must
     apply when the user has an open session and types nonsense.
  6. **English smoke** — single canonical English description, to
     confirm the prompt's EN mode hasn't silently broken.

Every other behaviour (branching, cycles, retries, lane-guard
fallback, JSON-recovery, Polza ↔ Gemini parity, error envelopes)
is now covered by the mocked integration layer:

  * `test_integration_llm.py`   — LLM HTTP / retry / 402 / schema.
  * `test_integration_backend.py` — Socket.IO + DB + ml errors.
  * `test_bpmn_layout.py`         — server-side lane-aware layout.
  * `test_bpmn_fix.py` / `test_validator.py` — post-processing.
  * `test_json_recovery.py`       — three JSON repair paths.
  * `test_lane_guard.py`          — explicit-role retry.

Run with `RUN_E2E=1 pytest ml/tests/test_e2e.py -v`. The tests
skip otherwise and the suite passes instantly.
"""
import pytest

from tests.conftest import (
    E2E_MARKERS,
    EN_LINEAR_PROCESS,
    RU_INVALID_GARBAGE,
    RU_PROCESS_WITH_LANES,
    RU_VALID_PROCESS,
    _classify,
    _edit,
    _generate,
    all_flow_nodes_in_lanes,
    cyrillic_ratio,
    extract_flow_node_names,
    extract_lanes,
    has_cycle,
    count_exclusive_gateways,
)


pytestmark = E2E_MARKERS

# Customer PDF1 description (Служебная командировка) inlined so the
# test doesn't depend on the file existing inside the container.
PDF1_KOMANDIROVKA = (
    "Используй роли:\n\n"
    "Схема Fly описывает процесс командировки в 3 ролях: сотрудник, "
    "руководитель/CEO ЦФО и бухгалтерия/кадры. Сотрудник инициирует процесс: "
    "согласует с руководителем цель, сроки, бюджет и при необходимости ЦФО, "
    "от которого едет. Если согласования нет, поездка не оформляется. После "
    "согласования сотрудник заполняет заявление в NP, при поездке не от "
    "своего ЦФО указывает это отдельно. Далее руководитель проверяет и "
    "согласует заявку либо возвращает на доработку. После одобрения "
    "сотрудник получает доступ к Aviasales, подбирает билеты и проживание "
    "в пределах лимитов; при превышении лимита нужно дополнительное "
    "согласование. Затем оформляется покупка билетов и бронирование "
    "гостиницы. Кадры/бухгалтерия формируют приказ и рассчитывают суточные, "
    "при зарубежной поездке корректируют сумму вручную. После этого "
    "сотрудник вносит отсутствие в календарь и уезжает в командировку. По "
    "завершении поездки он собирает подтверждающие документы, направляет "
    "отчет и чеки через SD/NP, а бухгалтерия проверяет документы, "
    "возмещает допустимые расходы или, при отмене/опоздании по вине "
    "сотрудника, оформляет удержание/возврат средств."
)


# ---------------------------------------------------------------------------
# 1. Russian generate (simple)
# ---------------------------------------------------------------------------


async def test_russian_generate_simple(ml_client):
    """Russian description → diagram with Russian labels on every flow
    node and a Russian session name."""
    result = await _generate(ml_client, RU_VALID_PROCESS)
    assert "bpmn_xml" in result
    assert "session_name" in result

    names = extract_flow_node_names(result["bpmn_xml"])
    assert names, "no flow-node names extracted"
    # All flow-node names must be predominantly Cyrillic.
    offenders = [n for n in names if cyrillic_ratio(n) < 0.7]
    assert not offenders, f"non-Russian names: {offenders!r}"

    # Session name follows the input language too.
    assert cyrillic_ratio(result["session_name"]) >= 0.5, (
        f"session_name not Russian: {result['session_name']!r}"
    )


# ---------------------------------------------------------------------------
# 2. Russian generate with lanes — real customer PDF
# ---------------------------------------------------------------------------


async def test_russian_generate_with_lanes_pdf(ml_client):
    """The exact PDF1 (командировка) spec the client hit us with.
    Production target: ≥ 3 lanes, ≥ 5 exclusive gateways, a rework
    cycle. This is the highest-value E2E assertion."""
    result = await _generate(ml_client, PDF1_KOMANDIROVKA)
    xml = result["bpmn_xml"]

    lanes = extract_lanes(xml)
    assert len(lanes) >= 3, f"expected ≥3 lanes, got {len(lanes)}: {list(lanes)}"

    # Every flow node assigned to exactly one lane.
    ok, bad = all_flow_nodes_in_lanes(xml)
    assert ok, f"flow nodes not properly assigned to lanes: {bad!r}"

    # Branching: ≥ 5 exclusive gateways (decisions in the spec —
    # согласовано? / лимит? / результат? / зарубеж? / вина?).
    gw = count_exclusive_gateways(xml)
    assert gw >= 5, f"expected ≥5 exclusive gateways, got {gw}"

    # Rework loop ("на доработку") → graph has a back-edge.
    assert has_cycle(xml), "expected cycle (на доработку), found none"


# ---------------------------------------------------------------------------
# 3. Russian edit — language sticks
# ---------------------------------------------------------------------------


async def test_russian_edit_language_preserved(ml_client):
    """Generate Russian → edit with English instruction → output
    must still have Russian labels. Tests the most subtle prompt
    behaviour ('detect dominant language, keep it')."""
    gen = await _generate(ml_client, RU_PROCESS_WITH_LANES)
    edited = await _edit(
        ml_client,
        "Add a step that archives the request after notification.",
        gen["bpmn_xml"],
    )
    names = extract_flow_node_names(edited["bpmn_xml"])
    assert names
    offenders = [n for n in names if cyrillic_ratio(n) < 0.5]
    # Allow at most one non-Cyrillic stray (e.g. the LLM kept some
    # English from the instruction): main body must stay Russian.
    assert len(offenders) <= 1, (
        f"too many non-Russian names after Russian → English edit: {offenders!r}"
    )


# ---------------------------------------------------------------------------
# 4. Classify rejection — /classify is the front-line gate
# ---------------------------------------------------------------------------


async def test_classify_rejects_weather_question(ml_client):
    """`/classify` must reject 'what is the weather' so we never
    spend LLM tokens generating a BPMN for nonsense input."""
    result = await _classify(ml_client, RU_INVALID_GARBAGE)
    assert result["is_valid"] is False
    # Reason should be in Russian (matches input language).
    assert cyrillic_ratio(result.get("reason", "")) > 0.3


async def test_classify_rejects_for_edit_intent(ml_client):
    """Per `_classify_input` design, the same classifier runs on
    every user message — including edit attempts. A user with a
    live session who types 'погода' must be rejected, not have
    the edit forwarded to ml."""
    result = await _classify(ml_client, "Сделай мне аватарку.")
    assert result["is_valid"] is False


# ---------------------------------------------------------------------------
# 6. English smoke
# ---------------------------------------------------------------------------


async def test_english_smoke(ml_client):
    """One English-only test, exercising the 'detect language → keep
    it English' branch of the prompt. If this breaks we know the EN
    prompt regressed even though the rest of the suite is RU-only."""
    result = await _generate(ml_client, EN_LINEAR_PROCESS)
    names = extract_flow_node_names(result["bpmn_xml"])
    assert names
    # Most names should be non-Cyrillic. Empty `cyrillic_ratio` returns
    # 0 for pure Latin → tolerate up to 0.1 to allow stray characters.
    cyrillic_offenders = [n for n in names if cyrillic_ratio(n) > 0.1]
    assert not cyrillic_offenders, (
        f"English request produced Russian labels: {cyrillic_offenders!r}"
    )
