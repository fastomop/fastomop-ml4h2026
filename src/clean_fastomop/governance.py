"""Pre-generation governance screen.

The semantic agent triages every request into `request_class` (see the `semantic-role`
skill). This module reads that field **deterministically** and decides whether the
pipeline continues. Nothing here calls a model: the classification is the model's, the
control-flow decision is not.

Placement matters. This runs BEFORE the database agent is constructed, so a screened
request never reaches SQL generation, the SQL validator, or the database. It is
complementary to — not a replacement for — the post-generation controls (the FastSSV
gate, the SQL server's AST allowlist, and the read-only connection), which remain the
guarantee: they operate on the generated artefact and cannot be argued out of by the
request text.

Fail-closed: a missing, unparseable, or unrecognised `request_class` is treated as
`out_of_scope` rather than assumed answerable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

#: Values the semantic agent may return. Anything else is coerced to `out_of_scope`.
ANSWERABLE = "answerable"
OUT_OF_SCOPE = "out_of_scope"
ADVERSARIAL = "adversarial"
KNOWN_CLASSES = frozenset({ANSWERABLE, OUT_OF_SCOPE, ADVERSARIAL})

#: Returned to the caller in place of an answer. Deliberately uninformative about *why*
#: beyond the class, so a refusal does not coach an attacker toward a working rephrasing.
REFUSAL_TEXT = {
    OUT_OF_SCOPE: (
        "This request is out of scope for this database. It answers population-level "
        "questions about patients recorded in the OMOP CDM."
    ),
    ADVERSARIAL: (
        "This request was declined. This interface answers population-level questions "
        "about patients recorded in the OMOP CDM and does not modify data, return "
        "identifiers, or disclose system internals."
    ),
}


@dataclass
class GovernanceDecision:
    """Outcome of the screen."""

    request_class: str
    allowed: bool
    reason: Optional[str] = None
    message: Optional[str] = None
    #: True when the class had to be inferred because the field was absent or malformed.
    defaulted: bool = False

    def as_dict(self) -> dict:
        return {
            "request_class": self.request_class,
            "allowed": self.allowed,
            "reason": self.reason,
            "defaulted": self.defaulted,
        }


def _coerce_json(semantic_context: Any) -> Optional[dict]:
    """Best-effort parse of the semantic agent's output into a dict."""
    if isinstance(semantic_context, dict):
        return semantic_context
    if not isinstance(semantic_context, str):
        return None
    s = semantic_context.strip()
    if not s:
        return None
    # Tolerate fenced or prose-wrapped JSON: take the outermost object.
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def screen(semantic_context: Any) -> GovernanceDecision:
    """Decide whether a request may proceed to SQL generation.

    Reads `request_class` from the semantic agent's output. Fails closed: if the field is
    missing, not a string, or not one of the known classes, the request is treated as
    `out_of_scope`. A pipeline that silently defaulted to `answerable` would let a
    malformed extraction bypass the screen entirely.
    """
    obj = _coerce_json(semantic_context)
    if obj is None:
        return GovernanceDecision(
            request_class=OUT_OF_SCOPE,
            allowed=False,
            reason="semantic context was not parseable JSON",
            message=REFUSAL_TEXT[OUT_OF_SCOPE],
            defaulted=True,
        )

    raw = obj.get("request_class")
    cls = raw.strip().lower() if isinstance(raw, str) else None
    defaulted = cls not in KNOWN_CLASSES
    if defaulted:
        return GovernanceDecision(
            request_class=OUT_OF_SCOPE,
            allowed=False,
            reason=(
                "request_class missing"
                if raw is None
                else f"unrecognised request_class {raw!r}"
            ),
            message=REFUSAL_TEXT[OUT_OF_SCOPE],
            defaulted=True,
        )

    if cls == ANSWERABLE:
        return GovernanceDecision(request_class=ANSWERABLE, allowed=True)

    reason = obj.get("request_class_reason")
    return GovernanceDecision(
        request_class=cls,
        allowed=False,
        reason=reason if isinstance(reason, str) else None,
        message=REFUSAL_TEXT[cls],
    )
