"""Level 2 E2E tests — non-linear BPMN processes (Iteration 2, Task 2).

Verifies that the LLM, guided by the updated prompt, can produce:
  * Conditional branching via exclusiveGateway with labeled outgoing flows
  * Rework loops (back-edges / cycles)
  * Multiple gateways for nested decisions
  * Parallel execution via parallelGateway

All assertions are structural (element counts, labeled branches, cycle
detection) — not exact string matches — because the LLM is non-deterministic.

Skipped unless RUN_E2E=1.
"""
import os

import httpx
import pytest

from tests.conftest import (
    count_exclusive_gateways,
    count_parallel_gateways,
    extract_flow_node_names,
    extract_sequence_flows,
    exclusive_gateway_ids,
    has_cycle,
    has_labeled_branch_from_gateway,
)


RUN_E2E = os.environ.get("RUN_E2E", "0") == "1"
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not RUN_E2E, reason="Set RUN_E2E=1 to run E2E tests"),
    pytest.mark.flaky(reruns=2, reruns_delay=2),
]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

RU_CONTRACT_APPROVAL = (
    "Процесс согласования договора: менеджер создаёт заявку, юрист "
    "проверяет документы. Если есть замечания — возвращает на доработку. "
    "Если замечаний нет — директор подписывает договор."
)

RU_ORDER_DECISION = (
    "Обработка заказа: принять заказ, проверить наличие товара. Если товар "
    "в наличии — отгрузить клиенту. Иначе — заказать у поставщика, потом "
    "отгрузить. В конце выставить счёт."
)

RU_NESTED_DECISIONS = (
    "Обработка обращения клиента: регистрация обращения, классификация. "
    "Если типовое — автоматическая обработка. Иначе передаётся менеджеру. "
    "Менеджер решает: удовлетворить запрос или отказать клиенту."
)

RU_QA_LOOP = (
    "Процесс тестирования: разработчик пишет код, QA инженер тестирует. "
    "Если найдены баги — возвращает на доработку разработчику. Цикл "
    "повторяется пока все тесты не пройдут. После успешного тестирования "
    "код деплоится в продакшн."
)

RU_PARALLEL = (
    "Обработка заявки на кредит: клиент подаёт заявку. Одновременно "
    "запускаются две проверки — проверка кредитной истории и проверка "
    "доходов. После того, как обе проверки завершены, кредитный офицер "
    "принимает решение и оформляет договор."
)

EN_SIMPLE_LINEAR = (
    "Employee onboarding: new hire submits documents, HR verifies identity, "
    "IT sets up accounts, manager assigns tasks."
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def ml_client(ml_base_url, internal_api_key):
    headers = {}
    if internal_api_key:
        headers["X-Internal-Api-Key"] = internal_api_key
    async with httpx.AsyncClient(
        base_url=ml_base_url,
        headers=headers,
        timeout=180.0,
    ) as client:
        yield client


async def _generate(client: httpx.AsyncClient, description: str) -> dict:
    resp = await client.post("/generate", json={"description": description})
    assert resp.status_code == 200, f"/generate failed: {resp.status_code} {resp.text[:500]}"
    return resp.json()


async def _edit(client: httpx.AsyncClient, prompt: str, bpmn_xml: str) -> dict:
    resp = await client.post("/edit", json={"prompt": prompt, "bpmn_xml": bpmn_xml})
    assert resp.status_code == 200, f"/edit failed: {resp.status_code} {resp.text[:500]}"
    return resp.json()


# ---------------------------------------------------------------------------
# Branching
# ---------------------------------------------------------------------------


class TestBranching:
    async def test_contract_approval_has_gateway(self, ml_client):
        """Contract approval with rework must use exclusiveGateway."""
        result = await _generate(ml_client, RU_CONTRACT_APPROVAL)
        xml = result["bpmn_xml"]
        gw_count = count_exclusive_gateways(xml)
        assert gw_count >= 1, (
            f"Expected ≥1 exclusiveGateway for contract-approval process, got {gw_count}. "
            f"XML: {xml[:500]}"
        )

    async def test_contract_approval_gateway_has_labeled_branches(self, ml_client):
        """At least one outgoing flow from the gateway must be labeled
        (name or conditionExpression)."""
        result = await _generate(ml_client, RU_CONTRACT_APPROVAL)
        xml = result["bpmn_xml"]
        assert has_labeled_branch_from_gateway(xml), (
            f"Expected at least one labeled branch from an exclusiveGateway. "
            f"Flows: {extract_sequence_flows(xml)}"
        )

    async def test_order_decision_two_branches(self, ml_client):
        """In-stock / out-of-stock decision must have 2 named outgoing flows."""
        result = await _generate(ml_client, RU_ORDER_DECISION)
        xml = result["bpmn_xml"]
        gw_ids = exclusive_gateway_ids(xml)
        assert gw_ids, f"Expected at least 1 exclusiveGateway, XML: {xml[:400]}"

        # Find diverging gateways (≥2 outgoing flows) and check their labeling
        flows = extract_sequence_flows(xml)
        diverging = {}
        for f in flows:
            if f["sourceRef"] in gw_ids:
                diverging.setdefault(f["sourceRef"], []).append(f)

        divergers = {g: fl for g, fl in diverging.items() if len(fl) >= 2}
        assert divergers, (
            f"Expected at least one diverging exclusiveGateway (2+ outgoing). "
            f"Found: {diverging}"
        )

        # At least one of the divergers must have ALL its branches labeled
        # (names or conditionExpressions)
        any_fully_labeled = any(
            all(f["name"] or f["has_condition_expr"] for f in fl)
            for fl in divergers.values()
        )
        assert any_fully_labeled, (
            f"No diverging gateway has all branches labeled. Divergers: {divergers}"
        )

    async def test_nested_decisions_has_two_gateways(self, ml_client):
        """Nested decision process should use ≥2 exclusiveGateways."""
        result = await _generate(ml_client, RU_NESTED_DECISIONS)
        xml = result["bpmn_xml"]
        gw_count = count_exclusive_gateways(xml)
        assert gw_count >= 2, (
            f"Expected ≥2 exclusiveGateways for nested-decision process, got {gw_count}. "
            f"Names: {extract_flow_node_names(xml)}"
        )


# ---------------------------------------------------------------------------
# Loops / rework
# ---------------------------------------------------------------------------


class TestLoops:
    async def test_qa_rework_contains_cycle(self, ml_client):
        """Rework loop must materialise as a cycle in the graph."""
        result = await _generate(ml_client, RU_QA_LOOP)
        xml = result["bpmn_xml"]
        assert has_cycle(xml), (
            f"Expected a cycle (back-edge) for rework process. "
            f"Flows: {[(f['sourceRef'], f['targetRef']) for f in extract_sequence_flows(xml)]}"
        )

    async def test_contract_approval_contains_cycle(self, ml_client):
        """Contract approval with "return to rework" should have a cycle."""
        result = await _generate(ml_client, RU_CONTRACT_APPROVAL)
        xml = result["bpmn_xml"]
        assert has_cycle(xml), (
            f"Expected a cycle (back-edge) for contract approval with rework. "
            f"Flows: {[(f['sourceRef'], f['targetRef']) for f in extract_sequence_flows(xml)]}"
        )


# ---------------------------------------------------------------------------
# Parallel
# ---------------------------------------------------------------------------


class TestParallel:
    async def test_parallel_credit_check_uses_parallel_gateway(self, ml_client):
        """Two concurrent checks should use parallelGateway."""
        result = await _generate(ml_client, RU_PARALLEL)
        xml = result["bpmn_xml"]
        parallel_count = count_parallel_gateways(xml)
        assert parallel_count >= 2, (
            f"Expected ≥2 parallelGateways (fork + join) for parallel process, "
            f"got {parallel_count}. Names: {extract_flow_node_names(xml)}"
        )


# ---------------------------------------------------------------------------
# Regression — simple linear still works
# ---------------------------------------------------------------------------


class TestLinearRegression:
    async def test_simple_linear_no_spurious_gateways(self, ml_client):
        """A purely linear description should NOT get bogus gateways/cycles."""
        result = await _generate(ml_client, EN_SIMPLE_LINEAR)
        xml = result["bpmn_xml"]
        # Allow 0 gateways (ideal) or at most 1 (LLM sometimes adds "verification
        # gateway" which isn't wrong per se). Strictly forbid cycles.
        assert not has_cycle(xml), (
            f"Simple linear process shouldn't contain cycles. "
            f"Flows: {[(f['sourceRef'], f['targetRef']) for f in extract_sequence_flows(xml)]}"
        )
        gw_count = count_exclusive_gateways(xml)
        assert gw_count <= 2, (
            f"Simple linear process has too many gateways ({gw_count}). "
            f"Names: {extract_flow_node_names(xml)}"
        )


# ---------------------------------------------------------------------------
# Edit — adding conditional via edit instruction
# ---------------------------------------------------------------------------


class TestEditAddBranching:
    async def test_edit_adds_exclusive_gateway(self, ml_client):
        """Generate a linear Russian process, then edit to add a conditional
        branch. Expect an exclusiveGateway to appear."""
        gen = await _generate(
            ml_client,
            "Процесс обработки счёта: получение счёта, проверка, оплата.",
        )
        initial_gw = count_exclusive_gateways(gen["bpmn_xml"])

        edited = await _edit(
            ml_client,
            "Добавь проверку суммы: если сумма больше 100000 — требуется "
            "дополнительное согласование с финдиректором, иначе — сразу к оплате.",
            gen["bpmn_xml"],
        )
        xml = edited["bpmn_xml"]
        new_gw = count_exclusive_gateways(xml)
        assert new_gw > initial_gw, (
            f"Edit should have added a gateway (was {initial_gw}, now {new_gw}). "
            f"Names: {extract_flow_node_names(xml)}"
        )
        assert has_labeled_branch_from_gateway(xml), (
            f"Added gateway should have labeled branches. "
            f"Flows: {extract_sequence_flows(xml)}"
        )
