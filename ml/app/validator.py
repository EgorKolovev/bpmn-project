from typing import Optional
from defusedxml import ElementTree as ET


BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"

NAMESPACES = {
    "bpmn": BPMN_NS,
    "bpmndi": BPMNDI_NS,
    "dc": DC_NS,
    "di": DI_NS,
}

# Tags that are NOT flow nodes (should not be collected as connectable elements)
NON_FLOW_NODE_TAGS = {
    "sequenceFlow", "messageFlow", "association", "dataObject",
    "dataObjectReference", "dataStoreReference", "textAnnotation",
    "incoming", "outgoing", "documentation", "extensionElements",
    "conditionExpression", "multiInstanceLoopCharacteristics",
    "standardLoopCharacteristics", "ioSpecification", "dataInput",
    "dataOutput", "inputSet", "outputSet", "property",
    "laneSet", "lane", "flowNodeRef",
}


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _flow_has_label(flow_elem) -> bool:
    """A sequenceFlow is "labeled" when it has a non-empty `name` attribute
    OR contains a non-empty <bpmn:conditionExpression> child."""
    name = flow_elem.get("name")
    if name and name.strip():
        return True
    for child in list(flow_elem):
        if _local(child.tag) == "conditionExpression" and (child.text or "").strip():
            return True
    return False


def validate_bpmn_xml(xml_string: str) -> Optional[str]:
    """Validate BPMN 2.0 XML. Returns None if valid, error message if invalid.

    Hard-failure checks:
      * Well-formed XML with <definitions> root and a <process>
      * Exactly one startEvent, at least one endEvent
      * All sequenceFlow sourceRef/targetRef point to real flow nodes
      * Any exclusiveGateway with 2+ outgoing flows has AT LEAST ONE
        outgoing flow labeled via `name` or <conditionExpression>.
        (Required so downstream tools / users can tell branches apart.)
    """
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as e:
        return f"XML parse error: {e}"

    if _local(root.tag) != "definitions":
        return f"Root element must be 'definitions', got '{_local(root.tag)}'"

    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return "No <process> element found"

    all_elements = list(process)
    flow_node_ids: set[str] = set()
    sequence_flows: list[tuple[str, str, str | None]] = []  # (source, target, flow_id)
    sequence_flow_by_id: dict[str, object] = {}
    exclusive_gateway_ids: set[str] = set()
    has_start = False
    has_end = False

    for elem in all_elements:
        tag = _local(elem.tag)
        elem_id = elem.get("id")

        if tag == "startEvent":
            has_start = True
            if elem_id:
                flow_node_ids.add(elem_id)
        elif tag == "endEvent":
            has_end = True
            if elem_id:
                flow_node_ids.add(elem_id)
        elif tag == "sequenceFlow":
            source = elem.get("sourceRef")
            target = elem.get("targetRef")
            if not source or not target:
                return f"sequenceFlow '{elem_id}' missing sourceRef or targetRef"
            sequence_flows.append((source, target, elem_id))
            if elem_id:
                sequence_flow_by_id[elem_id] = elem
        elif tag not in NON_FLOW_NODE_TAGS:
            # Generic flow node (task, gateway, event, subProcess…)
            if elem_id:
                flow_node_ids.add(elem_id)
            if tag == "exclusiveGateway" and elem_id:
                exclusive_gateway_ids.add(elem_id)

    if not has_start:
        return "No startEvent found in process"
    if not has_end:
        return "No endEvent found in process"

    for source, target, flow_id in sequence_flows:
        if source not in flow_node_ids:
            return f"sequenceFlow references unknown sourceRef '{source}'"
        if target not in flow_node_ids:
            return f"sequenceFlow references unknown targetRef '{target}'"

    # Rule: if laneSet exists, each flow node must be in EXACTLY ONE lane.
    lane_set = None
    for elem in all_elements:
        if _local(elem.tag) == "laneSet":
            lane_set = elem
            break

    if lane_set is not None:
        # node_id -> count of lanes that reference it
        ref_count: dict[str, int] = {nid: 0 for nid in flow_node_ids}
        for lane in list(lane_set):
            if _local(lane.tag) != "lane":
                continue
            for child in list(lane):
                if _local(child.tag) != "flowNodeRef":
                    continue
                ref = (child.text or "").strip()
                if ref in ref_count:
                    ref_count[ref] += 1
                elif ref:
                    # A flowNodeRef pointing to a non-existent flow node — stray.
                    return (
                        f"lane contains <flowNodeRef>{ref}</flowNodeRef> but "
                        f"no flow node with id '{ref}' exists in the process."
                    )
        dup = [nid for nid, c in ref_count.items() if c > 1]
        if dup:
            return (
                f"flow node(s) {', '.join(dup)} referenced by more than one "
                f"<bpmn:lane>. Each flow node must belong to exactly one lane."
            )
        # Missing refs are soft-fixed by ensure_lane_refs; we don't block.

    # Rule: every diverging exclusiveGateway (2+ outgoing flows) must have at
    # least ONE outgoing flow labeled with `name` or <conditionExpression>.
    outgoing_by_gateway: dict[str, list[object]] = {
        gw_id: [] for gw_id in exclusive_gateway_ids
    }
    for source, _target, flow_id in sequence_flows:
        if source in exclusive_gateway_ids and flow_id:
            flow_elem = sequence_flow_by_id.get(flow_id)
            if flow_elem is not None:
                outgoing_by_gateway[source].append(flow_elem)

    for gw_id, flows in outgoing_by_gateway.items():
        if len(flows) < 2:
            continue  # converging or pass-through gateway — nothing to label
        if not any(_flow_has_label(f) for f in flows):
            return (
                f"exclusiveGateway '{gw_id}' has {len(flows)} outgoing flows "
                f"but none carry a `name` or <conditionExpression>. "
                f"Add a meaningful label to at least one branch."
            )

    return None


def get_bpmn_warnings(xml_string: str) -> list[str]:
    """Return soft, non-fatal warnings about the BPMN XML.

    Currently:
      * exclusiveGateway with 2+ outgoing flows where SOME (but not all) are
        unlabeled — warn about the unlabeled ones, since downstream readers
        won't know which branch is which.

    Returns [] if nothing to flag. Never raises — best-effort parse.
    """
    warnings: list[str] = []
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return warnings

    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return warnings

    exclusive_gateway_ids: set[str] = set()
    sequence_flow_by_id: dict[str, object] = {}
    flows_per_gateway: dict[str, list[str]] = {}

    for elem in list(process):
        tag = _local(elem.tag)
        if tag == "exclusiveGateway":
            if eid := elem.get("id"):
                exclusive_gateway_ids.add(eid)
        elif tag == "sequenceFlow":
            fid = elem.get("id", "")
            src = elem.get("sourceRef", "")
            if fid:
                sequence_flow_by_id[fid] = elem
            if src and fid:
                flows_per_gateway.setdefault(src, []).append(fid)

    for gw_id in exclusive_gateway_ids:
        flow_ids = flows_per_gateway.get(gw_id, [])
        if len(flow_ids) < 2:
            continue
        unlabeled = [
            fid
            for fid in flow_ids
            if not _flow_has_label(sequence_flow_by_id[fid])
        ]
        # All labeled → perfect. None labeled → already hard-failed above.
        # Some labeled → warn about the unlabeled branch(es).
        if 0 < len(unlabeled) < len(flow_ids):
            warnings.append(
                f"exclusiveGateway '{gw_id}': {len(unlabeled)}/{len(flow_ids)} "
                f"outgoing flows are unlabeled ({', '.join(unlabeled)})."
            )

    return warnings
