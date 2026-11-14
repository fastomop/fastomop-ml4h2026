"""Structural complexity of a gold CIRCE definition — used to stratify the gold set.

A cohort is SIMPLE if it is a single-phenotype, single-entry-event definition with no
inclusion rules, correlated criteria, or concept exclusions. Otherwise it scores on those
structural features and is COMPLEX (>=2 sources of structure) or MODERATE (exactly 1).
This is the SAME definition used in the gold-set characterization.
"""
from __future__ import annotations

from typing import Any, Dict

BUCKETS = ("SIMPLE", "MODERATE", "COMPLEX")


def _count_key(obj: Any, key: str) -> int:
    """Count truthy occurrences of `key` anywhere in a nested dict/list."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and v:
                n += 1
            n += _count_key(v, key)
    elif isinstance(obj, list):
        for x in obj:
            n += _count_key(x, key)
    return n


def circe_features(circe: Dict[str, Any]) -> Dict[str, int]:
    cs = circe.get("ConceptSets") or []
    items = [it for x in cs for it in (x.get("expression", {}) or {}).get("items", []) or []]
    pc = (circe.get("PrimaryCriteria") or {}).get("CriteriaList") or []
    inc = circe.get("InclusionRules") or []
    return {
        "n_concept_sets": len(cs),
        "n_concepts": len(items),
        "n_excluded": sum(1 for it in items if it.get("isExcluded")),
        "n_arms": len(pc),
        "n_inclusion_rules": len(inc),
        "n_correlated": _count_key(circe.get("PrimaryCriteria"), "CorrelatedCriteria")
        + _count_key(inc, "CorrelatedCriteria"),
    }


def complexity_bucket(circe: Dict[str, Any]) -> str:
    f = circe_features(circe)
    if (f["n_arms"] <= 1 and f["n_inclusion_rules"] == 0
            and f["n_correlated"] == 0 and f["n_excluded"] == 0):
        return "SIMPLE"
    score = ((f["n_arms"] > 1) + (f["n_inclusion_rules"] >= 1)
             + (f["n_correlated"] >= 1) + (f["n_excluded"] >= 1)
             + (f["n_inclusion_rules"] >= 3))
    return "COMPLEX" if score >= 2 else "MODERATE"
