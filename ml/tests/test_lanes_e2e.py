"""Level 2 E2E tests — swimlanes / roles (Iteration 2, Task 3).

Verifies the LLM emits <bpmn:laneSet> when the description mentions
multiple roles, and that the structural invariants hold (every flow
node in exactly one lane).

Skipped unless RUN_E2E=1.
"""
import os

import httpx
import pytest

from tests.conftest import (
    all_flow_nodes_in_lanes,
    cyrillic_ratio,
    extract_lanes,
    has_lanes,
)


RUN_E2E = os.environ.get("RUN_E2E", "0") == "1"
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not RUN_E2E, reason="Set RUN_E2E=1 to run E2E tests"),
    pytest.mark.flaky(reruns=2, reruns_delay=2),
]


RU_TWO_ROLES = (
    "Согласование заявки: Менеджер создаёт заявку. Директор рассматривает "
    "и принимает решение. Если одобрено — менеджер отправляет клиенту. "
    "Если отклонено — менеджер уведомляет клиента об отказе."
)

RU_THREE_ROLES = (
    "Оформление командировки: Сотрудник подаёт заявку. Руководитель отдела "
    "рассматривает и утверждает. Если утверждено — Бухгалтерия оформляет "
    "документы и выдаёт аванс."
)

RU_NO_ROLES = (
    "Процесс приёма заказа: принять заказ, обработать, отправить клиенту."
)


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


class TestLaneGeneration:
    async def test_two_roles_produces_two_lanes(self, ml_client):
        result = await _generate(ml_client, RU_TWO_ROLES)
        xml = result["bpmn_xml"]
        lanes = extract_lanes(xml)
        assert lanes, f"Expected lanes for two-role process, got none. XML: {xml[:500]}"
        assert len(lanes) >= 2, f"Expected ≥2 lanes, got {len(lanes)}: {list(lanes)}"

    async def test_two_roles_all_nodes_in_exactly_one_lane(self, ml_client):
        result = await _generate(ml_client, RU_TWO_ROLES)
        xml = result["bpmn_xml"]
        ok, bad = all_flow_nodes_in_lanes(xml)
        assert ok, (
            f"Not every flow node is in exactly one lane. Offenders: {bad}. "
            f"Lanes: {extract_lanes(xml)}"
        )

    async def test_two_roles_lane_names_in_russian(self, ml_client):
        result = await _generate(ml_client, RU_TWO_ROLES)
        xml = result["bpmn_xml"]
        lanes = extract_lanes(xml)
        for name in lanes.keys():
            assert cyrillic_ratio(name) >= 0.7, (
                f"Lane name '{name}' should be in Russian (ratio={cyrillic_ratio(name):.2f})"
            )

    async def test_three_roles_produces_three_lanes(self, ml_client):
        result = await _generate(ml_client, RU_THREE_ROLES)
        xml = result["bpmn_xml"]
        lanes = extract_lanes(xml)
        assert len(lanes) >= 3, (
            f"Expected ≥3 lanes for three-role process, got {len(lanes)}: {list(lanes)}"
        )

    async def test_no_roles_no_lanes(self, ml_client):
        """Processes without explicit roles should NOT get lanes."""
        result = await _generate(ml_client, RU_NO_ROLES)
        xml = result["bpmn_xml"]
        assert not has_lanes(xml), (
            f"Unexpected lanes for role-less process: {extract_lanes(xml)}"
        )


class TestLaneEdit:
    async def test_edit_preserves_lanes(self, ml_client):
        """Editing a lane-based diagram should keep the laneSet intact."""
        gen = await _generate(ml_client, RU_TWO_ROLES)
        original_xml = gen["bpmn_xml"]
        original_lanes = extract_lanes(original_xml)
        assert len(original_lanes) >= 2

        edited = await _edit(
            ml_client,
            "Добавь шаг архивации договора после отправки клиенту.",
            original_xml,
        )
        new_xml = edited["bpmn_xml"]
        new_lanes = extract_lanes(new_xml)
        assert len(new_lanes) >= 2, (
            f"Edit lost the laneSet. Original lanes: {list(original_lanes)}, "
            f"new lanes: {list(new_lanes)}"
        )
        ok, bad = all_flow_nodes_in_lanes(new_xml)
        assert ok, (
            f"After edit, some flow nodes aren't in exactly one lane: {bad}. "
            f"Lanes: {new_lanes}"
        )
