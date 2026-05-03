"""Unit tests for the lane-aware server-side BPMN layouter.

Covers:
  * No laneSet (linear / branching) — should add BPMNDiagram with shapes
    and edges, no lane shapes, no isHorizontal=true.
  * laneSet present — every lane gets its own BPMNShape with
    `isHorizontal="true"`, every flow node gets a shape inside its lane,
    every sequence flow gets a BPMNEdge with waypoints.
  * Cycle (back-edge) — column assignment is robust and the U-bend
    routing emits 4 waypoints with the right shape.
  * Idempotence — re-laying out an already-laid-out XML strips the old
    DI and rebuilds it, never producing duplicates.
  * Defensive: malformed XML / no process / no flow nodes — returns
    input unchanged so the front-end fallback can still render.

Pure unit tests: no LLM, no Docker, no network.
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("GEMINI_API_KEY", "test-key-for-unit-tests")

import xml.etree.ElementTree as ET

import pytest

from app.bpmn_layout import (
    BPMN_NS,
    BPMNDI_NS,
    DC_NS,
    DI_NS,
    has_layout,
    layout_bpmn,
)


def _count(xml: str, pattern: str) -> int:
    return len(re.findall(pattern, xml))


# --- Sample inputs ----------------------------------------------------------

LINEAR_NO_LANES = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:startEvent id="Start_1"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:task id="Task_1" name="Place Order">
      <bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
    <bpmn:task id="Task_2" name="Verify">
      <bpmn:incoming>Flow_2</bpmn:incoming><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:task>
    <bpmn:endEvent id="End_1"><bpmn:incoming>Flow_3</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1"/>
    <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="Task_2"/>
    <bpmn:sequenceFlow id="Flow_3" sourceRef="Task_2" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""

THREE_LANES_WITH_CYCLE = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:laneSet id="LaneSet_1">
      <bpmn:lane id="Lane_Manager" name="Менеджер">
        <bpmn:flowNodeRef>Start_1</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>Task_Submit</bpmn:flowNodeRef>
      </bpmn:lane>
      <bpmn:lane id="Lane_Lawyer" name="Юрист">
        <bpmn:flowNodeRef>Task_Review</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>Gateway_1</bpmn:flowNodeRef>
      </bpmn:lane>
      <bpmn:lane id="Lane_Director" name="Директор">
        <bpmn:flowNodeRef>Task_Sign</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>End_1</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
    <bpmn:startEvent id="Start_1"><bpmn:outgoing>F1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:task id="Task_Submit"><bpmn:incoming>F1</bpmn:incoming><bpmn:outgoing>F2</bpmn:outgoing></bpmn:task>
    <bpmn:task id="Task_Review">
      <bpmn:incoming>F2</bpmn:incoming><bpmn:incoming>F5</bpmn:incoming>
      <bpmn:outgoing>F3</bpmn:outgoing></bpmn:task>
    <bpmn:exclusiveGateway id="Gateway_1">
      <bpmn:incoming>F3</bpmn:incoming>
      <bpmn:outgoing>F4</bpmn:outgoing><bpmn:outgoing>F5</bpmn:outgoing></bpmn:exclusiveGateway>
    <bpmn:task id="Task_Sign"><bpmn:incoming>F4</bpmn:incoming><bpmn:outgoing>F6</bpmn:outgoing></bpmn:task>
    <bpmn:endEvent id="End_1"><bpmn:incoming>F6</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="F1" sourceRef="Start_1" targetRef="Task_Submit"/>
    <bpmn:sequenceFlow id="F2" sourceRef="Task_Submit" targetRef="Task_Review"/>
    <bpmn:sequenceFlow id="F3" sourceRef="Task_Review" targetRef="Gateway_1"/>
    <bpmn:sequenceFlow id="F4" name="OK" sourceRef="Gateway_1" targetRef="Task_Sign"/>
    <bpmn:sequenceFlow id="F5" name="Rework" sourceRef="Gateway_1" targetRef="Task_Review"/>
    <bpmn:sequenceFlow id="F6" sourceRef="Task_Sign" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""


# --- Tests ------------------------------------------------------------------


class TestLinearNoLanes:
    def test_adds_diagram(self):
        out = layout_bpmn(LINEAR_NO_LANES)
        assert has_layout(out)
        assert _count(out, r"<bpmndi:BPMNDiagram") == 1

    def test_one_shape_per_node(self):
        out = layout_bpmn(LINEAR_NO_LANES)
        # 1 start + 2 tasks + 1 end = 4 flow nodes → 4 shapes
        assert _count(out, r"<bpmndi:BPMNShape") == 4

    def test_no_lane_shapes(self):
        out = layout_bpmn(LINEAR_NO_LANES)
        assert _count(out, r'isHorizontal="true"') == 0

    def test_one_edge_per_flow(self):
        out = layout_bpmn(LINEAR_NO_LANES)
        assert _count(out, r"<bpmndi:BPMNEdge") == 3

    def test_each_edge_has_waypoints(self):
        out = layout_bpmn(LINEAR_NO_LANES)
        # Same-row forward edges emit 2 waypoints each → 3*2 = 6.
        assert _count(out, r"<di:waypoint") == 6

    def test_x_progresses_left_to_right(self):
        """Tasks must have monotonically increasing x positions."""
        out = layout_bpmn(LINEAR_NO_LANES)
        # Find shape blocks by id and pull x.
        ids = ["Start_1", "Task_1", "Task_2", "End_1"]
        xs: list[int] = []
        for nid in ids:
            m = re.search(
                rf'<bpmndi:BPMNShape[^>]+bpmnElement="{nid}"[^>]*>\s*<dc:Bounds[^>]+x="(\d+)"',
                out,
            )
            assert m, f"no shape found for {nid}"
            xs.append(int(m.group(1)))
        assert xs == sorted(xs), f"x positions not monotonic: {xs}"


class TestThreeLanesWithCycle:
    def test_adds_lane_shapes(self):
        out = layout_bpmn(THREE_LANES_WITH_CYCLE)
        assert _count(out, r'isHorizontal="true"') == 3
        # All three lane labels present in the lane shape stack.
        for lane_id in ("Lane_Manager", "Lane_Lawyer", "Lane_Director"):
            assert re.search(
                rf'<bpmndi:BPMNShape[^>]+bpmnElement="{lane_id}"[^>]+isHorizontal="true"',
                out,
            ), f"no lane shape for {lane_id}"

    def test_one_shape_per_flow_node(self):
        out = layout_bpmn(THREE_LANES_WITH_CYCLE)
        # 1 start + 3 tasks + 1 gateway + 1 end + 3 lanes = 9 BPMNShape
        assert _count(out, r"<bpmndi:BPMNShape") == 9

    def test_cycle_has_back_edge_routing(self):
        """The Rework back-edge (F5) should emit 4 waypoints — the
        U-bend routing for back-edges, not the 2-waypoint straight line
        for forward same-row flows.
        """
        out = layout_bpmn(THREE_LANES_WITH_CYCLE)
        edge_match = re.search(
            r'<bpmndi:BPMNEdge[^>]+bpmnElement="F5"[^>]*>(.*?)</bpmndi:BPMNEdge>',
            out, re.DOTALL,
        )
        assert edge_match, "no edge for F5"
        n_waypoints = len(re.findall(r"<di:waypoint", edge_match.group(1)))
        assert n_waypoints == 4, (
            f"expected 4 waypoints on back-edge F5 (U-bend), got {n_waypoints}"
        )

    def test_lanes_stacked_vertically(self):
        """Each lane Y must be greater than the previous (top to bottom)."""
        out = layout_bpmn(THREE_LANES_WITH_CYCLE)
        ys: list[int] = []
        for lane_id in ("Lane_Manager", "Lane_Lawyer", "Lane_Director"):
            m = re.search(
                rf'<bpmndi:BPMNShape[^>]+bpmnElement="{lane_id}"[^>]*>\s*<dc:Bounds[^>]+y="(\d+)"',
                out,
            )
            assert m, f"no bounds for {lane_id}"
            ys.append(int(m.group(1)))
        assert ys == sorted(ys), f"lanes not stacked top→bottom: {ys}"

    def test_node_inside_its_lane(self):
        """Task_Submit (in Manager lane) must have y between Manager's y and
        Manager.y + Manager.height — i.e. it physically sits inside its lane."""
        out = layout_bpmn(THREE_LANES_WITH_CYCLE)
        lane_m = re.search(
            r'<bpmndi:BPMNShape[^>]+bpmnElement="Lane_Manager"[^>]*>\s*'
            r'<dc:Bounds[^>]+x="\d+"\s+y="(\d+)"\s+width="\d+"\s+height="(\d+)"',
            out,
        )
        assert lane_m
        lane_y, lane_h = int(lane_m.group(1)), int(lane_m.group(2))
        node_m = re.search(
            r'<bpmndi:BPMNShape[^>]+bpmnElement="Task_Submit"[^>]*>\s*'
            r'<dc:Bounds[^>]+x="\d+"\s+y="(\d+)"',
            out,
        )
        assert node_m
        node_y = int(node_m.group(1))
        assert lane_y <= node_y <= lane_y + lane_h, (
            f"Task_Submit y={node_y} not inside Lane_Manager [{lane_y}, {lane_y+lane_h}]"
        )

    def test_xml_parses_after_layout(self):
        """Output must be well-formed XML that bpmn-js can ingest."""
        out = layout_bpmn(THREE_LANES_WITH_CYCLE)
        ET.fromstring(out)  # raises if malformed


class TestIdempotence:
    def test_relayout_preserves_counts(self):
        """Running layout twice should give a single BPMNDiagram, not two."""
        once = layout_bpmn(THREE_LANES_WITH_CYCLE)
        twice = layout_bpmn(once)
        assert _count(twice, r"<bpmndi:BPMNDiagram") == 1
        assert _count(twice, r"<bpmndi:BPMNShape") == _count(once, r"<bpmndi:BPMNShape")
        assert _count(twice, r"<bpmndi:BPMNEdge") == _count(once, r"<bpmndi:BPMNEdge")

    def test_has_layout_helper(self):
        assert not has_layout(LINEAR_NO_LANES)
        assert has_layout(layout_bpmn(LINEAR_NO_LANES))


class TestSiblingSpread:
    """Multiple edges leaving the same gateway (or arriving at same merge)
    must NOT share the bend's vertical x-coordinate — that's the visible-
    as-bug case where Курьер/ЭДО/Почта lines stacked into a single column.
    """

    THREE_BRANCHES = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:laneSet id="LaneSet_1">
      <bpmn:lane id="Lane_A" name="A">
        <bpmn:flowNodeRef>Start_1</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>GW_Split</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>Task_Top</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>GW_Merge</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>End_1</bpmn:flowNodeRef>
      </bpmn:lane>
      <bpmn:lane id="Lane_B" name="B">
        <bpmn:flowNodeRef>Task_Mid</bpmn:flowNodeRef>
      </bpmn:lane>
      <bpmn:lane id="Lane_C" name="C">
        <bpmn:flowNodeRef>Task_Bot</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
    <bpmn:startEvent id="Start_1"><bpmn:outgoing>F1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:exclusiveGateway id="GW_Split">
      <bpmn:incoming>F1</bpmn:incoming>
      <bpmn:outgoing>F2</bpmn:outgoing><bpmn:outgoing>F3</bpmn:outgoing><bpmn:outgoing>F4</bpmn:outgoing></bpmn:exclusiveGateway>
    <bpmn:task id="Task_Top"><bpmn:incoming>F2</bpmn:incoming><bpmn:outgoing>F5</bpmn:outgoing></bpmn:task>
    <bpmn:task id="Task_Mid"><bpmn:incoming>F3</bpmn:incoming><bpmn:outgoing>F6</bpmn:outgoing></bpmn:task>
    <bpmn:task id="Task_Bot"><bpmn:incoming>F4</bpmn:incoming><bpmn:outgoing>F7</bpmn:outgoing></bpmn:task>
    <bpmn:exclusiveGateway id="GW_Merge">
      <bpmn:incoming>F5</bpmn:incoming><bpmn:incoming>F6</bpmn:incoming><bpmn:incoming>F7</bpmn:incoming>
      <bpmn:outgoing>F8</bpmn:outgoing></bpmn:exclusiveGateway>
    <bpmn:endEvent id="End_1"><bpmn:incoming>F8</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="F1" sourceRef="Start_1" targetRef="GW_Split"/>
    <bpmn:sequenceFlow id="F2" name="Top" sourceRef="GW_Split" targetRef="Task_Top"/>
    <bpmn:sequenceFlow id="F3" name="Mid" sourceRef="GW_Split" targetRef="Task_Mid"/>
    <bpmn:sequenceFlow id="F4" name="Bot" sourceRef="GW_Split" targetRef="Task_Bot"/>
    <bpmn:sequenceFlow id="F5" sourceRef="Task_Top" targetRef="GW_Merge"/>
    <bpmn:sequenceFlow id="F6" sourceRef="Task_Mid" targetRef="GW_Merge"/>
    <bpmn:sequenceFlow id="F7" sourceRef="Task_Bot" targetRef="GW_Merge"/>
    <bpmn:sequenceFlow id="F8" sourceRef="GW_Merge" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""

    def _bend_xs(self, xml: str, edge_ids: list[str]) -> list[int]:
        """Pull the second waypoint's x for each edge (the bend point
        after exiting the source)."""
        out = []
        for eid in edge_ids:
            edge_match = re.search(
                rf'<bpmndi:BPMNEdge[^>]+bpmnElement="{eid}"[^>]*>(.*?)</bpmndi:BPMNEdge>',
                xml, re.DOTALL,
            )
            assert edge_match, f"no edge for {eid}"
            wps = re.findall(r'<di:waypoint[^>]+x="(\d+)"[^>]+y="(\d+)"', edge_match.group(1))
            assert len(wps) >= 2
            out.append(int(wps[1][0]))
        return out

    def test_three_branches_have_distinct_bends(self):
        """F2 (Top), F3 (Mid), F4 (Bot) all leave the same GW_Split.
        Their second waypoint (the bend after the gateway) must have
        DISTINCT x values — otherwise lines stack.
        """
        out = layout_bpmn(self.THREE_BRANCHES)
        bends = self._bend_xs(out, ["F2", "F3", "F4"])
        assert len(set(bends)) == 3, (
            f"expected 3 distinct bend xs, got {bends}"
        )

    def test_three_merges_have_distinct_bends(self):
        """F5, F6, F7 all arrive at GW_Merge. Their second waypoint
        (where each one bends to enter the merge) must be distinct."""
        out = layout_bpmn(self.THREE_BRANCHES)
        bends = self._bend_xs(out, ["F5", "F6", "F7"])
        assert len(set(bends)) == 3, (
            f"expected 3 distinct merge bends, got {bends}"
        )

    def test_single_outgoing_edge_unaffected(self):
        """A node with exactly one outgoing edge must NOT pay the
        spread cost — its bend stays at the geometric midpoint."""
        out = layout_bpmn(THREE_LANES_WITH_CYCLE)
        # F1 (Start_1 → Task_Submit) is a single outgoing.
        bend_x = self._bend_xs(out, ["F1"])[0]
        # Just sanity-check the bend exists and is positive.
        # Single-sibling spread offset is 0 by design (n=1).
        assert bend_x > 0


class TestDefensive:
    def test_empty_xml_returned_as_is(self):
        assert layout_bpmn("") == ""

    def test_malformed_xml_returned_as_is(self):
        bad = "<not-bpmn><unclosed>"
        out = layout_bpmn(bad)
        assert out == bad  # parse failed → unchanged

    def test_no_process_returned_as_is(self):
        no_proc = '<?xml version="1.0"?><root xmlns="x"/>'
        out = layout_bpmn(no_proc)
        # No process found → returned unchanged.
        assert "<bpmndi:BPMNDiagram" not in out

    def test_empty_process_returned_as_is(self):
        """A process with no flow nodes must not crash."""
        empty = """<?xml version="1.0"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <bpmn:process id="Process_1" isExecutable="true"/>
</bpmn:definitions>"""
        out = layout_bpmn(empty)
        # No flow nodes → returned unchanged (no crash).
        assert "<bpmndi:BPMNDiagram" not in out
