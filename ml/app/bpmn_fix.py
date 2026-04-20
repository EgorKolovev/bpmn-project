"""
Post-processing module to ensure BPMN XML has proper <incoming>/<outgoing>
child elements on every flow node. This is required by bpmn-auto-layout
to traverse the graph and generate correct edge waypoints.
"""

import logging
import re
from typing import Any
import xml.etree.ElementTree as XmlET
from defusedxml import ElementTree as SafeET

logger = logging.getLogger(__name__)

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Tags that are NOT flow nodes — everything else with an id is treated as one
NON_FLOW_NODE_TAGS = {
    "sequenceFlow", "messageFlow", "association", "dataObject",
    "dataObjectReference", "dataStoreReference", "textAnnotation",
    "incoming", "outgoing", "documentation", "extensionElements",
    "conditionExpression", "multiInstanceLoopCharacteristics",
    "standardLoopCharacteristics", "ioSpecification", "dataInput",
    "dataOutput", "inputSet", "outputSet", "property",
    "laneSet", "lane", "flowNodeRef",
}


def _get_local_tag(elem: Any) -> str:
    """Extract local tag name without namespace."""
    tag = elem.tag
    if "}" in tag:
        return tag.split("}")[-1]
    return tag


def _get_namespace(elem: Any) -> str:
    """Extract namespace URI from element tag."""
    tag = elem.tag
    if "}" in tag:
        return tag.split("}")[0][1:]
    return ""


def ensure_incoming_outgoing(xml_string: str) -> str:
    """
    Parse BPMN XML and ensure every flow node has correct
    <incoming> and <outgoing> child elements based on sequenceFlow definitions.

    This is idempotent — existing incoming/outgoing elements are removed
    and regenerated from sequenceFlow sourceRef/targetRef attributes.
    """
    try:
        root = SafeET.fromstring(xml_string)
    except SafeET.ParseError as e:
        logger.warning(f"Cannot fix incoming/outgoing - XML parse error: {e}")
        return xml_string

    # Find the process element
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        logger.warning("Cannot fix incoming/outgoing - no process element found")
        return xml_string

    # Determine the namespace prefix used for BPMN elements
    process_ns = _get_namespace(process)

    # Collect all sequenceFlow information
    outgoing_map: dict[str, list[str]] = {}  # sourceRef -> [flow_ids]
    incoming_map: dict[str, list[str]] = {}  # targetRef -> [flow_ids]

    for elem in list(process):
        local_tag = _get_local_tag(elem)
        if local_tag == "sequenceFlow":
            flow_id = elem.get("id", "")
            source = elem.get("sourceRef", "")
            target = elem.get("targetRef", "")
            if source and flow_id:
                outgoing_map.setdefault(source, []).append(flow_id)
            if target and flow_id:
                incoming_map.setdefault(target, []).append(flow_id)

    # For each flow node, remove existing incoming/outgoing and add correct ones
    for elem in list(process):
        local_tag = _get_local_tag(elem)
        if local_tag in NON_FLOW_NODE_TAGS:
            continue

        elem_id = elem.get("id", "")
        if not elem_id:
            continue

        # Remove existing incoming/outgoing children
        to_remove = []
        for child in list(elem):
            child_tag = _get_local_tag(child)
            if child_tag in ("incoming", "outgoing"):
                to_remove.append(child)
        for child in to_remove:
            elem.remove(child)

        # Add correct incoming elements (insert at beginning for clean ordering)
        incoming_flows = incoming_map.get(elem_id, [])
        outgoing_flows = outgoing_map.get(elem_id, [])

        # Insert incoming first, then outgoing (standard BPMN ordering)
        insert_idx = 0
        for flow_id in incoming_flows:
            inc_elem = XmlET.SubElement(
                elem,
                f"{{{process_ns}}}incoming" if process_ns else "incoming",
            )
            inc_elem.text = flow_id
            elem.remove(inc_elem)
            elem.insert(insert_idx, inc_elem)
            insert_idx += 1

        for flow_id in outgoing_flows:
            out_elem = XmlET.SubElement(
                elem,
                f"{{{process_ns}}}outgoing" if process_ns else "outgoing",
            )
            out_elem.text = flow_id
            elem.remove(out_elem)
            elem.insert(insert_idx, out_elem)
            insert_idx += 1

    # Serialize back to string
    XmlET.register_namespace("bpmn", BPMN_NS)
    XmlET.register_namespace("bpmndi", "http://www.omg.org/spec/BPMN/20100524/DI")
    XmlET.register_namespace("dc", "http://www.omg.org/spec/DD/20100524/DC")
    XmlET.register_namespace("di", "http://www.omg.org/spec/DD/20100524/DI")

    result = XmlET.tostring(root, encoding="unicode", xml_declaration=True)

    logger.info(
        f"Ensured incoming/outgoing refs: "
        f"{sum(len(v) for v in incoming_map.values())} incoming, "
        f"{sum(len(v) for v in outgoing_map.values())} outgoing"
    )

    return result


def strip_bpmn_diagram(xml_string: str) -> str:
    """Remove any BPMNDiagram section from the XML, since layout is auto-generated."""
    return re.sub(
        r"<bpmndi:BPMNDiagram[\s\S]*?</bpmndi:BPMNDiagram>",
        "",
        xml_string,
        flags=re.IGNORECASE,
    )


# Namespaces that LLMs commonly use but sometimes forget to declare on the
# root element (causing "unbound prefix" XML parse errors). We pre-emptively
# inject declarations when the prefix is used somewhere in the body but not
# bound on the root element.
_WELL_KNOWN_NAMESPACES = {
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
    "dc": "http://www.omg.org/spec/DD/20100524/DC",
    "di": "http://www.omg.org/spec/DD/20100524/DI",
}


def fix_missing_namespace_declarations(xml_string: str) -> str:
    """If the XML uses a `prefix:` on attributes/elements but doesn't declare
    `xmlns:prefix=...` on the root element, inject the declaration.

    This is a textual fix applied BEFORE parsing — catches errors like
    ``<bpmn:definitions xmlns:bpmn="..."><sequenceFlow><conditionExpression
    xsi:type="...">…`` where the LLM used `xsi:` without binding it.

    Only well-known BPMN-related prefixes are auto-bound. Unknown prefixes
    are left alone so we don't mask real errors.
    """
    if not xml_string or "<" not in xml_string:
        return xml_string

    # Find the first tag that looks like the root element (first `<…>`
    # not counting declarations/comments).
    m = re.search(r"<([A-Za-z_][\w\-.]*:)?([A-Za-z_][\w\-.]*)([^>]*)>", xml_string)
    if not m:
        return xml_string

    # Skip XML decl <?xml …?> and comments
    # (regex above already matches element tags only, since it captures name)
    root_full = m.group(0)
    root_attrs = m.group(3) or ""

    # Which prefixes does the document reference anywhere?
    used_prefixes = set(re.findall(r"(?<![A-Za-z0-9_.-])([A-Za-z_][\w\-.]*):[A-Za-z_]", xml_string))
    # Which are already declared on the root?
    declared_prefixes = set(re.findall(r'xmlns:([A-Za-z_][\w\-.]*)\s*=', root_attrs))

    missing = [
        p for p in used_prefixes
        if p in _WELL_KNOWN_NAMESPACES
        and p not in declared_prefixes
        and p != "xmlns"
    ]
    if not missing:
        return xml_string

    injection = "".join(
        f' xmlns:{p}="{_WELL_KNOWN_NAMESPACES[p]}"' for p in missing
    )
    fixed_root = root_full[:-1] + injection + ">"
    result = xml_string.replace(root_full, fixed_root, 1)
    logger.info("Injected missing namespace declarations: %s", ", ".join(missing))
    return result


def ensure_lane_refs(xml_string: str) -> str:
    """If the process has a <bpmn:laneSet>, make sure EVERY flow node is
    referenced in exactly ONE <bpmn:flowNodeRef>. Fix-ups:

      * Flow node referenced in ZERO lanes → append to the FIRST lane.
      * Flow node referenced in MORE THAN ONE lane → keep only the first
        reference, drop duplicates.
      * Stray flowNodeRef pointing to a non-existent id → drop it.

    If there's no laneSet, return the XML unchanged.

    Idempotent.
    """
    try:
        root = SafeET.fromstring(xml_string)
    except SafeET.ParseError as e:
        logger.warning(f"Cannot fix lane refs - XML parse error: {e}")
        return xml_string

    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return xml_string

    # Find laneSet (allow either namespaced or unnamespaced)
    lane_set = None
    for elem in list(process):
        if _get_local_tag(elem) == "laneSet":
            lane_set = elem
            break
    if lane_set is None:
        return xml_string

    process_ns = _get_namespace(process)

    # Collect every flow node id (non-flow tags excluded)
    flow_node_ids: list[str] = []
    flow_node_id_set: set[str] = set()
    for elem in list(process):
        tag = _get_local_tag(elem)
        if tag in NON_FLOW_NODE_TAGS:
            continue
        if eid := elem.get("id"):
            flow_node_ids.append(eid)
            flow_node_id_set.add(eid)

    # Enumerate lanes and their current flowNodeRef lists
    lanes: list[object] = [
        child for child in list(lane_set) if _get_local_tag(child) == "lane"
    ]
    if not lanes:
        # laneSet exists but has no lanes — leave as-is (unusual)
        return xml_string

    # Build current assignment: node_id -> [lane_idx] (first-seen-first)
    assignments: dict[str, list[int]] = {nid: [] for nid in flow_node_ids}
    for lane_idx, lane in enumerate(lanes):
        for child in list(lane):
            if _get_local_tag(child) == "flowNodeRef":
                ref = (child.text or "").strip()
                if ref in assignments:
                    assignments[ref].append(lane_idx)

    # Rebuild each lane's flowNodeRef list:
    #   * Remove duplicates (keep the first lane that claims the node)
    #   * Drop stray refs (to non-existent ids) — implicitly dropped because
    #     we rebuild from a curated list
    claimed: set[str] = set()
    # First pass: each node goes to its first-claiming lane (stable order)
    node_to_lane: dict[str, int] = {}
    for lane_idx, lane in enumerate(lanes):
        for child in list(lane):
            if _get_local_tag(child) != "flowNodeRef":
                continue
            ref = (child.text or "").strip()
            if ref in flow_node_id_set and ref not in claimed:
                node_to_lane[ref] = lane_idx
                claimed.add(ref)

    # Second pass: any unclaimed flow node → first lane
    for nid in flow_node_ids:
        if nid not in claimed:
            node_to_lane[nid] = 0
            claimed.add(nid)

    # Wipe existing flowNodeRef children and re-populate
    for lane in lanes:
        for child in list(lane):
            if _get_local_tag(child) == "flowNodeRef":
                lane.remove(child)

    # Append refs back in the order flow nodes appear in the process
    tag_name = f"{{{process_ns}}}flowNodeRef" if process_ns else "flowNodeRef"
    for nid in flow_node_ids:
        target_lane = lanes[node_to_lane[nid]]
        ref_elem = XmlET.SubElement(target_lane, tag_name)
        ref_elem.text = nid

    # Serialize back
    XmlET.register_namespace("bpmn", BPMN_NS)
    XmlET.register_namespace("bpmndi", "http://www.omg.org/spec/BPMN/20100524/DI")
    XmlET.register_namespace("dc", "http://www.omg.org/spec/DD/20100524/DC")
    XmlET.register_namespace("di", "http://www.omg.org/spec/DD/20100524/DI")

    result = XmlET.tostring(root, encoding="unicode", xml_declaration=True)
    logger.info(
        "Ensured lane refs: %d flow nodes across %d lanes",
        len(flow_node_ids),
        len(lanes),
    )
    return result
