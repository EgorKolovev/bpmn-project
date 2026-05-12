"""Post-generation guard: did the user explicitly ask for roles, and
did the model produce a `<bpmn:laneSet>` in response?

Used as a belt-and-suspenders check on top of the prompt instruction —
patterns are tight to avoid false-positives on casual mentions.
"""

import re

# Trigger phrases that force a `<bpmn:laneSet>` in the generated diagram.
_EXPLICIT_ROLE_PATTERNS = [
    re.compile(r"\bисполь[зс]уй\s+рол[иея]", re.IGNORECASE),
    re.compile(r"\bс\s+рол[яеия][хм]?\b", re.IGNORECASE),
    re.compile(r"\bв\s+\d+\s+рол[яеия][хм]?\b", re.IGNORECASE),
    re.compile(r"\bрол[иея]\s*:", re.IGNORECASE),
    re.compile(r"\bучастник[иа]?\s*:", re.IGNORECASE),
    re.compile(r"\bактёр[ыа]?\s*:", re.IGNORECASE),
    re.compile(r"\buse\s+roles?\b", re.IGNORECASE),
    re.compile(r"\bwith\s+roles?\b", re.IGNORECASE),
    re.compile(r"\bin\s+\d+\s+roles?\b", re.IGNORECASE),
    re.compile(r"\broles?\s*:", re.IGNORECASE),
    re.compile(r"\bactors?\s*:", re.IGNORECASE),
    re.compile(r"\bparticipants?\s*:", re.IGNORECASE),
    re.compile(r"\bswimlanes?\s*:", re.IGNORECASE),
    re.compile(r"\bby\s+role\b", re.IGNORECASE),
]

_LANESET_MARKER = re.compile(r"<\s*(?:bpmn:)?laneSet\b", re.IGNORECASE)


def description_requires_lanes(description: str) -> bool:
    """True if the description contains an explicit role-enumeration hint.

    Used as a post-generation guard: if the user clearly asked for roles
    but the LLM produced a flat process (no laneSet), we retry with an
    explicit correction prompt instead of silently shipping a degraded
    diagram.
    """
    for pat in _EXPLICIT_ROLE_PATTERNS:
        if pat.search(description):
            return True
    return False


def xml_has_lanes(xml: str) -> bool:
    """True if the BPMN XML contains a non-empty `<bpmn:laneSet>`."""
    return bool(_LANESET_MARKER.search(xml))
