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


def validate_bpmn_xml(xml_string: str) -> Optional[str]:
    """Validate BPMN 2.0 XML. Returns None if valid, error message if invalid."""
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as e:
        return f"XML parse error: {e}"

    local_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if local_tag != "definitions":
        return f"Root element must be 'definitions', got '{local_tag}'"

    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return "No <process> element found"

    all_elements = list(process)
    flow_node_ids = set()
    sequence_flows = []
    has_start = False
    has_end = False

    for elem in all_elements:
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
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
            sequence_flows.append((source, target))
        elif tag not in NON_FLOW_NODE_TAGS:
            # Treat any other element with an id as a potential flow node.
            # This covers task, userTask, serviceTask, scriptTask, sendTask,
            # receiveTask, manualTask, businessRuleTask, callActivity,
            # subProcess, transaction, adHocSubProcess, exclusiveGateway,
            # parallelGateway, inclusiveGateway, complexGateway,
            # eventBasedGateway, intermediateCatchEvent,
            # intermediateThrowEvent, and any other BPMN 2.0 element.
            if elem_id:
                flow_node_ids.add(elem_id)

    if not has_start:
        return "No startEvent found in process"
    if not has_end:
        return "No endEvent found in process"

    for source, target in sequence_flows:
        if source not in flow_node_ids:
            return f"sequenceFlow references unknown sourceRef '{source}'"
        if target not in flow_node_ids:
            return f"sequenceFlow references unknown targetRef '{target}'"

    return None
