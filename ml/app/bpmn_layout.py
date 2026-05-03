"""Server-side BPMN auto-layout with lane (swimlane) support.

The upstream `bpmn-auto-layout` JS package currently flattens diagrams
that contain a `<bpmn:laneSet>` — it lays out flow nodes in a single
horizontal row and emits no lane shapes. For role-rich customer specs
(командировка, отправка документов) this loses the most valuable
visual information: who does what.

This module fills that gap. Given a BPMN 2.0 XML string from the LLM:

  * Detects whether the process declares a `<bpmn:laneSet>`.
  * Assigns each flow node a horizontal column via topological depth
    (cycles are tolerated — back-edges are skipped for ordering).
  * Stacks nodes vertically inside their lane's Y band, splitting into
    sub-rows if multiple nodes land in the same (lane × column) cell.
  * Emits `<bpmndi:BPMNDiagram>` with one BPMNShape per lane and per
    flow node, plus orthogonal BPMNEdges with waypoints.

Output is a complete BPMN 2.0 XML string that bpmn-js can render
directly — no client-side layout pass needed.

If the input has no laneSet we still lay out nodes in one row, matching
the simpler behaviour the LLM produced. Front-end keeps its existing
`bpmn-auto-layout` fallback for any edge case where this module bails
out (returns input unchanged on parse error).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# --- Namespaces ---------------------------------------------------------------
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

# --- Default sizes (px), aligned with bpmn-js conventions --------------------
W_TASK = 100
H_TASK = 80
W_GW = 50
H_GW = 50
W_EVENT = 36
H_EVENT = 36

# Grid spacing between adjacent columns and lane padding.
COL_WIDTH = 180  # x-distance between consecutive column centers
LANE_PAD_TOP = 20
LANE_PAD_BOTTOM = 20
LANE_LABEL_WIDTH = 30  # left strip with rotated lane name
SUB_ROW_HEIGHT = 110  # extra vertical space per stacked node within a lane

# Lane height auto-grows to fit its tallest sub-row, but never less than this:
LANE_MIN_HEIGHT = 140

# Left margin where the first column lives, after the lane label strip.
PROCESS_LEFT_MARGIN = LANE_LABEL_WIDTH + 60


@dataclass
class FlowNode:
    id: str
    tag: str  # local tag — "task", "exclusiveGateway", "startEvent" …
    name: str = ""
    lane_id: Optional[str] = None
    width: int = W_TASK
    height: int = H_TASK
    x: int = 0
    y: int = 0
    column: int = 0


@dataclass
class Lane:
    id: str
    name: str
    node_ids: list[str] = field(default_factory=list)
    y: int = 0
    height: int = LANE_MIN_HEIGHT


@dataclass
class Edge:
    id: str
    source: str
    target: str


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _shape_dims(tag: str) -> tuple[int, int]:
    if tag in ("startEvent", "endEvent", "intermediateThrowEvent",
               "intermediateCatchEvent", "boundaryEvent"):
        return W_EVENT, H_EVENT
    if "Gateway" in tag or tag.endswith("gateway"):
        return W_GW, H_GW
    return W_TASK, H_TASK


def _parse_process(root: ET.Element) -> tuple[ET.Element, list[FlowNode], list[Edge], list[Lane]]:
    """Pull flow nodes / edges / lanes out of the first executable process."""
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        raise ValueError("No <bpmn:process> element found")

    lanes: list[Lane] = []
    lane_for_node: dict[str, str] = {}
    laneset = process.find(f"{{{BPMN_NS}}}laneSet")
    if laneset is not None:
        for lane_elem in laneset.findall(f"{{{BPMN_NS}}}lane"):
            lane_id = lane_elem.get("id") or f"Lane_{len(lanes)}"
            lane_name = lane_elem.get("name", "")
            node_ids = [
                ref.text for ref in lane_elem.findall(f"{{{BPMN_NS}}}flowNodeRef")
                if ref.text
            ]
            lanes.append(Lane(id=lane_id, name=lane_name, node_ids=list(node_ids)))
            for nid in node_ids:
                lane_for_node[nid] = lane_id

    nodes: list[FlowNode] = []
    edges: list[Edge] = []
    flow_node_tags = {
        "startEvent", "endEvent", "intermediateThrowEvent",
        "intermediateCatchEvent", "boundaryEvent",
        "task", "userTask", "serviceTask", "scriptTask", "sendTask",
        "receiveTask", "manualTask", "businessRuleTask", "callActivity",
        "subProcess",
        "exclusiveGateway", "inclusiveGateway", "parallelGateway",
        "eventBasedGateway", "complexGateway",
    }
    for elem in list(process):
        tag = _local(elem.tag)
        if tag in flow_node_tags:
            nid = elem.get("id")
            if not nid:
                continue
            w, h = _shape_dims(tag)
            nodes.append(FlowNode(
                id=nid, tag=tag, name=elem.get("name", ""),
                lane_id=lane_for_node.get(nid),
                width=w, height=h,
            ))
        elif tag == "sequenceFlow":
            sid = elem.get("id")
            src = elem.get("sourceRef")
            tgt = elem.get("targetRef")
            if sid and src and tgt:
                edges.append(Edge(id=sid, source=src, target=tgt))
    return process, nodes, edges, lanes


def _assign_columns(nodes: list[FlowNode], edges: list[Edge]) -> dict[str, int]:
    """Topological-depth column assignment, robust to cycles.

    Algorithm:
      1. Find back-edges via DFS from start events (or any source-only nodes).
      2. Treat back-edges as non-existent for the depth pass.
      3. BFS in topological order; column[v] = max(column[u]) + 1 over
         non-back predecessors.
      4. Nodes unreachable from any start get column 0.
    """
    successors: dict[str, list[str]] = defaultdict(list)
    predecessors: dict[str, list[str]] = defaultdict(list)
    node_ids = {n.id for n in nodes}
    for e in edges:
        if e.source in node_ids and e.target in node_ids:
            successors[e.source].append(e.target)
            predecessors[e.target].append(e.source)

    # Detect back-edges via DFS coloring.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    back_edges: set[tuple[str, str]] = set()
    starts = [n.id for n in nodes if n.tag == "startEvent"] or [
        n.id for n in nodes if not predecessors[n.id]
    ] or [nodes[0].id] if nodes else []

    def dfs(start: str) -> None:
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, idx = stack.pop()
            if idx == 0:
                if color.get(node) == BLACK:
                    continue
                color[node] = GRAY
            children = successors.get(node, [])
            if idx < len(children):
                stack.append((node, idx + 1))
                child = children[idx]
                c = color.get(child, WHITE)
                if c == GRAY:
                    back_edges.add((node, child))
                elif c == WHITE:
                    stack.append((child, 0))
            else:
                color[node] = BLACK

    for s in starts:
        if color.get(s, WHITE) == WHITE:
            dfs(s)

    # Topological depth ignoring back-edges.
    in_deg: dict[str, int] = {n.id: 0 for n in nodes}
    for e in edges:
        if e.source in node_ids and e.target in node_ids and (e.source, e.target) not in back_edges:
            in_deg[e.target] += 1

    queue: deque[str] = deque(nid for nid, d in in_deg.items() if d == 0)
    column: dict[str, int] = {nid: 0 for nid in in_deg}
    while queue:
        u = queue.popleft()
        for v in successors[u]:
            if (u, v) in back_edges:
                continue
            column[v] = max(column[v], column[u] + 1)
            in_deg[v] -= 1
            if in_deg[v] == 0:
                queue.append(v)
    # Any remaining (cycle without start) get column 0 already.
    return column


def _layout(nodes: list[FlowNode], edges: list[Edge], lanes: list[Lane]) -> tuple[int, int]:
    """Compute (x, y) for every node and (y, height) for every lane.

    Returns the (total_width, total_height) of the laid-out diagram.
    """
    columns = _assign_columns(nodes, edges)
    for n in nodes:
        n.column = columns.get(n.id, 0)

    # Group nodes by (lane_id, column) → list of nodes in that cell.
    if not lanes:
        # No-laneset case: single virtual lane covering everything.
        lanes = [Lane(id="__virtual_lane__", name="", node_ids=[n.id for n in nodes])]
        for n in nodes:
            n.lane_id = "__virtual_lane__"

    cell: dict[tuple[str, int], list[FlowNode]] = defaultdict(list)
    for n in nodes:
        cell[(n.lane_id or lanes[0].id, n.column)].append(n)

    # For each lane, count max sub-rows needed.
    lane_subrows: dict[str, int] = {}
    for lane in lanes:
        max_subrows = 1
        for col_idx in range(0, max((c for _, c in cell if _ == lane.id), default=0) + 1):
            n_in_cell = len(cell.get((lane.id, col_idx), []))
            if n_in_cell > max_subrows:
                max_subrows = n_in_cell
        lane_subrows[lane.id] = max_subrows
        lane.height = max(LANE_MIN_HEIGHT, max_subrows * SUB_ROW_HEIGHT + LANE_PAD_TOP + LANE_PAD_BOTTOM)

    # Stack lanes top-to-bottom.
    cur_y = 0
    for lane in lanes:
        lane.y = cur_y
        cur_y += lane.height
    total_height = cur_y if cur_y > 0 else LANE_MIN_HEIGHT

    # Determine total columns (max across lanes).
    max_col = max((n.column for n in nodes), default=0)
    total_width = PROCESS_LEFT_MARGIN + (max_col + 1) * COL_WIDTH

    # Place each node.
    for lane in lanes:
        for col_idx in range(max_col + 1):
            cell_nodes = cell.get((lane.id, col_idx), [])
            if not cell_nodes:
                continue
            cell_x_center = PROCESS_LEFT_MARGIN + col_idx * COL_WIDTH + COL_WIDTH // 2
            n_sub = len(cell_nodes)
            slot_h = (lane.height - LANE_PAD_TOP - LANE_PAD_BOTTOM) / max(n_sub, 1)
            for sub_idx, n in enumerate(cell_nodes):
                cell_y_center = lane.y + LANE_PAD_TOP + slot_h * (sub_idx + 0.5)
                n.x = int(cell_x_center - n.width / 2)
                n.y = int(cell_y_center - n.height / 2)

    return total_width, total_height


def _route_edge(src: FlowNode, tgt: FlowNode) -> list[tuple[int, int]]:
    """Orthogonal waypoint routing for one BPMN sequence flow.

    Three cases:

    * **Forward, same row** (`abs(sy-ty) < 5` and `tgt.x > src.x`):
      Straight line from source's right edge to target's left edge.

    * **Forward, different row** (`tgt.x > src.x`, but lanes differ):
      Elbow connector — exit source right, midpoint bend, enter target left.

    * **Back-edge / loop** (`tgt.x <= src.x`):
      Routes UNDER the source's lane: leaves source from the bottom,
      travels back-left at a fixed offset below the source row, climbs
      up into the target from below. This avoids crossing through
      intermediate task shapes — the visual issue with the naive
      "horizontal at source Y" routing.

    Bpmn-js draws labels relative to the second-to-last waypoint, so the
    final segment is always horizontal entering the target's left edge
    (or vertical entering its top, for back-edges that loop under).
    """
    sx_left, sx_right = src.x, src.x + src.width
    sy_top, sy_bottom = src.y, src.y + src.height
    sy = src.y + src.height // 2
    tx_left = tgt.x
    tx_right = tgt.x + tgt.width
    ty_top = tgt.y
    ty_bottom = tgt.y + tgt.height
    ty = tgt.y + tgt.height // 2

    # Back-edge: target sits to the LEFT of source's right edge.
    if tx_right <= sx_right:
        # Drop a U-bend below the source. Route: source bottom →
        # offset below source → far left → up alongside target →
        # into target's bottom.
        loop_y = max(sy_bottom, ty_bottom) + 30
        return [
            (sx_left + src.width // 2, sy_bottom),
            (sx_left + src.width // 2, loop_y),
            (tx_left + tgt.width // 2, loop_y),
            (tx_left + tgt.width // 2, ty_bottom),
        ]

    # Forward edges from here on — target is to the right.
    if abs(sy - ty) < 5:
        return [(sx_right, sy), (tx_left, ty)]

    mid_x = sx_right + (tx_left - sx_right) // 2
    return [
        (sx_right, sy),
        (mid_x, sy),
        (mid_x, ty),
        (tx_left, ty),
    ]


def _strip_existing_di(root: ET.Element) -> None:
    """Remove any existing BPMNDiagram so we don't double-layout."""
    for diag in list(root.findall(f"{{{BPMNDI_NS}}}BPMNDiagram")):
        root.remove(diag)


def _ensure_namespaces(root: ET.Element) -> None:
    """Make sure root declares all DI namespaces we're about to use."""
    if root.tag != f"{{{BPMN_NS}}}definitions":
        return
    # ElementTree handles xmlns via the curly-brace prefix in tags; xsi/
    # bpmndi/dc/di will be auto-declared on serialization. We just need
    # to make sure they're present in the root attribs to keep prefixes
    # readable. ET's serializer does this when it encounters the URI on
    # any tag, so we don't need to set anything manually here.
    pass


def _build_di(
    process_id: str,
    nodes: list[FlowNode],
    edges: list[Edge],
    lanes: list[Lane],
    node_by_id: dict[str, FlowNode],
) -> ET.Element:
    """Build a fresh <bpmndi:BPMNDiagram> element."""
    diagram = ET.Element(
        f"{{{BPMNDI_NS}}}BPMNDiagram",
        {"id": "BPMNDiagram_1"},
    )
    plane = ET.SubElement(
        diagram,
        f"{{{BPMNDI_NS}}}BPMNPlane",
        {"id": "BPMNPlane_1", "bpmnElement": process_id},
    )

    # Lane shapes: full process width, lane's own Y / height.
    if lanes and not (len(lanes) == 1 and lanes[0].id == "__virtual_lane__"):
        max_col = max((n.column for n in nodes), default=0)
        total_w = PROCESS_LEFT_MARGIN + (max_col + 1) * COL_WIDTH
        for lane in lanes:
            shape = ET.SubElement(
                plane,
                f"{{{BPMNDI_NS}}}BPMNShape",
                {
                    "id": f"{lane.id}_di",
                    "bpmnElement": lane.id,
                    "isHorizontal": "true",
                },
            )
            ET.SubElement(
                shape,
                f"{{{DC_NS}}}Bounds",
                {"x": "0", "y": str(lane.y),
                 "width": str(total_w), "height": str(lane.height)},
            )

    # Flow node shapes.
    for n in nodes:
        shape = ET.SubElement(
            plane,
            f"{{{BPMNDI_NS}}}BPMNShape",
            {"id": f"{n.id}_di", "bpmnElement": n.id},
        )
        ET.SubElement(
            shape,
            f"{{{DC_NS}}}Bounds",
            {"x": str(n.x), "y": str(n.y),
             "width": str(n.width), "height": str(n.height)},
        )

    # Sequence flow edges with waypoints.
    for e in edges:
        src = node_by_id.get(e.source)
        tgt = node_by_id.get(e.target)
        if src is None or tgt is None:
            continue
        edge_el = ET.SubElement(
            plane,
            f"{{{BPMNDI_NS}}}BPMNEdge",
            {"id": f"{e.id}_di", "bpmnElement": e.id},
        )
        for x, y in _route_edge(src, tgt):
            ET.SubElement(
                edge_el,
                f"{{{DI_NS}}}waypoint",
                {"x": str(x), "y": str(y)},
            )
    return diagram


def has_layout(xml: str) -> bool:
    """Cheap pre-flight check — does the XML already have BPMNDiagram?"""
    return bool(re.search(r"<\s*(?:bpmndi:)?BPMNDiagram\b", xml))


def layout_bpmn(xml: str) -> str:
    """Lay out BPMN XML in place. Returns new XML with BPMNDiagram appended.

    On any parse / structural error we log a warning and return the
    input unchanged so the front-end can fall back to its own layouter.
    """
    if not xml or not xml.strip():
        return xml
    try:
        # Register namespace prefixes BEFORE parsing so serialization
        # uses the conventional `bpmn:`, `bpmndi:` etc. instead of `ns0`.
        ET.register_namespace("", BPMN_NS)
        ET.register_namespace("bpmn", BPMN_NS)
        ET.register_namespace("bpmndi", BPMNDI_NS)
        ET.register_namespace("di", DI_NS)
        ET.register_namespace("dc", DC_NS)
        ET.register_namespace("xsi", XSI_NS)
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.warning("bpmn_layout: parse error, skipping (%s)", exc)
        return xml

    try:
        process, nodes, edges, lanes = _parse_process(root)
    except ValueError as exc:
        logger.warning("bpmn_layout: %s — skipping", exc)
        return xml

    if not nodes:
        logger.warning("bpmn_layout: no flow nodes — skipping")
        return xml

    _layout(nodes, edges, lanes)
    node_by_id = {n.id: n for n in nodes}
    _strip_existing_di(root)
    diagram = _build_di(process.get("id") or "Process_1", nodes, edges, lanes, node_by_id)
    root.append(diagram)

    # Serialize. ET drops the XML declaration unless asked.
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
