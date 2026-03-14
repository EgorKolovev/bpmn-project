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
