"""Agent-facing tools beyond the 7 core builder mutators: read-only vocabulary-hierarchy
exploration (so the model REASONS about granularity), a precise id-based codeset commit, and
a rule-relax mutator for the Critic. Wraps `HierarchyResource` + the core `CohortToolset`.

The core mutators (start_cohort, add_concept_set, add_primary_criteria, add_event_rule, ...)
stay in tools.py; this module only adds what the multi-agent loop needs on top.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..resources.concepts import ConceptResolver
from ..resources.hierarchy import HierarchyResource
from .circe_model import ConceptItem
from .tools import CohortToolset

# tool names handled by AgentToolset (everything else routes to the core CohortToolset)
AGENT_TOOLS = {
    "lookup_concept", "get_children", "get_descendants", "get_parents", "get_ancestors",
    "concept_coverage", "preview_codeset", "attrition_estimate",
    "add_concept_set_by_ids", "replace_concept_set_by_ids", "relax_inclusion_rule",
}


class AgentToolset:
    """Read-only hierarchy/estimate tools + two narrow mutators, sharing the core toolset's
    builder. Every method returns a JSON string (the tool result fed back to the LLM)."""

    def __init__(self, core: CohortToolset, hierarchy: HierarchyResource,
                 resolver: ConceptResolver):
        self.core = core
        self.h = hierarchy
        self.resolver = resolver

    @property
    def builder(self):
        return self.core.builder

    # ── read-only exploration ────────────────────────────────────────────────
    def lookup_concept(self, name: str, domain: str, top_k: int = 8) -> str:
        """Name -> candidate standard concepts, each with its CDM patient coverage. The entry
        point for exploring granularity BEFORE committing a codeset (no side effects)."""
        cands = []
        for h in self.resolver.search(name, domain=domain, top_k=top_k):
            cands.append({"concept_id": h.concept_id, "name": h.concept_name,
                          "vocabulary": h.vocabulary_id,
                          "coverage": self.resolver.coverage(h.concept_id, domain)})
        return json.dumps({"query": name, "domain": domain, "candidates": cands})

    def get_children(self, concept_id: int, domain: Optional[str] = None) -> str:
        return json.dumps(self.h.get_children(concept_id, domain))

    def get_descendants(self, concept_id: int, domain: Optional[str] = None,
                        max_levels: Optional[int] = None) -> str:
        return json.dumps(self.h.get_descendants(concept_id, domain, max_levels))

    def get_parents(self, concept_id: int, domain: Optional[str] = None) -> str:
        return json.dumps(self.h.get_parents(concept_id, domain))

    def get_ancestors(self, concept_id: int, domain: Optional[str] = None,
                      max_levels: int = 3) -> str:
        return json.dumps(self.h.get_ancestors(concept_id, domain, max_levels))

    def concept_coverage(self, concept_id: int, domain: str) -> str:
        return json.dumps(self.h.concept_coverage(concept_id, domain))

    def preview_codeset(self, concept_ids: List[int], domain: str,
                        exclude_ids: Optional[List[int]] = None,
                        include_descendants: bool = True) -> str:
        """Fast distinct-patient count for a candidate codeset (no circe-be). Granularity
        signal only — ignores entry/observation logic."""
        items = [{"concept_id": int(c), "include_descendants": include_descendants,
                  "is_excluded": False} for c in (concept_ids or [])]
        items += [{"concept_id": int(c), "include_descendants": include_descendants,
                   "is_excluded": True} for c in (exclude_ids or [])]
        return json.dumps(self.h.preview_codeset(items, domain))

    def attrition_estimate(self) -> str:
        """Per-inclusion-rule attrition chart (real circe-be) on the cohort built so far."""
        if self.builder is None:
            return json.dumps({"error": "no cohort started"})
        return json.dumps(self.h.attrition_estimate(self.builder))

    # ── narrow mutators ──────────────────────────────────────────────────────
    def add_concept_set_by_ids(self, name: str, concept_ids: List[int], domain: str,
                               exclude_ids: Optional[List[int]] = None,
                               include_descendants: bool = True) -> str:
        """Commit a codeset from EXPLICIT concept ids (the ones the model chose after exploring
        the hierarchy) — the precise alternative to fuzzy name resolution. Returns codeset_id +
        the candidate codeset's patient coverage so the choice is verified."""
        if self.builder is None:
            return json.dumps({"error": "call start_cohort first"})
        ids = [int(c) for c in (concept_ids or [])]
        exc = [int(c) for c in (exclude_ids or [])]
        names = self.h._names(ids + exc)
        missing = [c for c in ids + exc if c not in names]
        if missing:
            return json.dumps({"error": "unknown concept_id(s)", "unknown": missing})
        items = [ConceptItem.make(c, names.get(c, ""), domain,
                                  include_descendants=include_descendants) for c in ids]
        items += [ConceptItem.make(c, names.get(c, ""), domain, excluded=True,
                                   include_descendants=include_descendants) for c in exc]
        cs_id = self.builder.add_concept_set(name, items)
        preview = self.h.preview_codeset(
            [{"concept_id": c, "include_descendants": include_descendants} for c in ids] +
            [{"concept_id": c, "include_descendants": include_descendants, "is_excluded": True}
             for c in exc], domain)
        return json.dumps({"ok": True, "codeset_id": cs_id,
                           "concepts": [{"concept_id": c, "name": names.get(c)} for c in ids],
                           "preview_patients": preview.get("patients")})

    def replace_concept_set_by_ids(self, codeset_id: int, concept_ids: List[int], domain: str,
                                   exclude_ids: Optional[List[int]] = None,
                                   include_descendants: bool = True) -> str:
        """Replace the concepts of an EXISTING codeset in place (the surgical broaden/narrow:
        e.g. swap a too-narrow 'Left heart failure' codeset for the 'Heart failure' family).
        The entry/inclusion criteria that reference this codeset_id are unchanged."""
        if self.builder is None:
            return json.dumps({"error": "no cohort started"})
        target = next((cs for cs in self.builder._concept_sets
                       if cs["id"] == int(codeset_id)), None)
        if target is None:
            return json.dumps({"error": f"no codeset {codeset_id}",
                               "codesets": self.builder.codeset_ids()})
        ids = [int(c) for c in (concept_ids or [])]
        exc = [int(c) for c in (exclude_ids or [])]
        names = self.h._names(ids + exc)
        missing = [c for c in ids + exc if c not in names]
        if missing:
            return json.dumps({"error": "unknown concept_id(s)", "unknown": missing})
        target["expression"]["items"] = (
            [ConceptItem.make(c, names.get(c, ""), domain, include_descendants=include_descendants)
             for c in ids] +
            [ConceptItem.make(c, names.get(c, ""), domain, excluded=True,
                              include_descendants=include_descendants) for c in exc])
        preview = self.h.preview_codeset(
            [{"concept_id": c, "include_descendants": include_descendants} for c in ids] +
            [{"concept_id": c, "include_descendants": include_descendants, "is_excluded": True}
             for c in exc], domain)
        return json.dumps({"ok": True, "codeset_id": int(codeset_id),
                           "concepts": [{"concept_id": c, "name": names.get(c)} for c in ids],
                           "preview_patients": preview.get("patients")})

    def relax_inclusion_rule(self, name: str) -> str:
        """Drop one global inclusion rule by name (the Critic's remedy for an over-restrictive
        rule it judged not clinically required). Does not execute — pair with attrition_estimate."""
        if self.builder is None:
            return json.dumps({"error": "no cohort started"})
        rules = self.builder.inclusion_rules()
        kept = [r for r in rules if r.get("name") != name]
        if len(kept) == len(rules):
            return json.dumps({"error": f"no inclusion rule named {name!r}",
                               "rules": [r.get("name") for r in rules]})
        self.builder.set_inclusion_rules(kept)
        return json.dumps({"ok": True, "dropped": name,
                           "remaining_rules": [r.get("name") for r in kept]})


# ── OpenAI tool schemas ──────────────────────────────────────────────────────
_DOMAINS = ["Condition", "Drug", "Procedure", "Measurement", "Observation", "Visit", "Device"]


def _fn(name: str, desc: str, props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


def _explore_schemas() -> List[Dict[str, Any]]:
    cid = {"type": "integer", "description": "an OMOP concept_id"}
    dom = {"type": "string", "enum": _DOMAINS}
    return [
        _fn("lookup_concept",
            "Resolve a clinical term to candidate STANDARD concepts, each with how many CDM "
            "patients it covers (with descendants). Use this to explore granularity before "
            "committing a codeset.",
            {"name": {"type": "string"}, "domain": dom}, ["name", "domain"]),
        _fn("get_ancestors",
            "Walk UP the hierarchy: ancestors of a concept (up to max_levels), each annotated "
            "with the patient coverage of its WHOLE descendant family. Use to decide whether the "
            "data codes the phenotype at a broader parent (e.g. 'Heart failure' ~30k vs 'Left "
            "heart failure' ~282).",
            {"concept_id": cid, "domain": dom, "max_levels": {"type": "integer", "default": 3}},
            ["concept_id", "domain"]),
        _fn("get_parents", "Direct parents (one level up) of a concept, with family coverage.",
            {"concept_id": cid, "domain": dom}, ["concept_id", "domain"]),
        _fn("get_descendants",
            "Walk DOWN: the standard concepts a codeset expands to under includeDescendants, each "
            "with its direct patient count (most-populated first) — REVIEW these and exclude "
            "unwanted children.",
            {"concept_id": cid, "domain": dom, "max_levels": {"type": "integer"}},
            ["concept_id", "domain"]),
        _fn("get_children", "Direct children (one level down) of a concept, with patient counts.",
            {"concept_id": cid, "domain": dom}, ["concept_id", "domain"]),
        _fn("concept_coverage",
            "Patients matched WITH descendants vs coded DIRECTLY to a concept — tells you if the "
            "phenotype lives at this node or in its subtree.",
            {"concept_id": cid, "domain": dom}, ["concept_id", "domain"]),
        _fn("preview_codeset",
            "Fast distinct-patient count for a CANDIDATE codeset (a list of concept_ids, optional "
            "exclude_ids, descendants). No circe-be — a granularity check only.",
            {"concept_ids": {"type": "array", "items": {"type": "integer"}}, "domain": dom,
             "exclude_ids": {"type": "array", "items": {"type": "integer"}},
             "include_descendants": {"type": "boolean", "default": True}},
            ["concept_ids", "domain"]),
    ]


def _commit_by_ids_schema() -> Dict[str, Any]:
    return _fn("add_concept_set_by_ids",
               "Commit a codeset from EXPLICIT concept ids you chose after exploring the "
               "hierarchy (descendants included by default). Returns codeset_id + the codeset's "
               "patient coverage. Prefer this over add_concept_set when you picked specific ids.",
               {"name": {"type": "string"},
                "concept_ids": {"type": "array", "items": {"type": "integer"}},
                "domain": {"type": "string", "enum": _DOMAINS},
                "exclude_ids": {"type": "array", "items": {"type": "integer"}},
                "include_descendants": {"type": "boolean", "default": True}},
               ["name", "concept_ids", "domain"])


def _replace_schema() -> Dict[str, Any]:
    return _fn("replace_concept_set_by_ids",
               "Replace the concepts of an EXISTING codeset in place — the surgical broaden/narrow "
               "(e.g. swap a too-narrow 'Left heart failure' codeset for the 'Heart failure' family). "
               "Entry/inclusion criteria referencing this codeset_id are unchanged.",
               {"codeset_id": {"type": "integer"},
                "concept_ids": {"type": "array", "items": {"type": "integer"}},
                "domain": {"type": "string", "enum": _DOMAINS},
                "exclude_ids": {"type": "array", "items": {"type": "integer"}},
                "include_descendants": {"type": "boolean", "default": True}},
               ["codeset_id", "concept_ids", "domain"])


def builder_extra_schemas() -> List[Dict[str, Any]]:
    """Hierarchy exploration + id-based commit/replace, added to the core 7 for the Builder."""
    return _explore_schemas() + [_commit_by_ids_schema(), _replace_schema()]


def critic_schemas() -> List[Dict[str, Any]]:
    """Read-only diagnostics + in-place granularity fix + relax-rule for the Critic agent."""
    attrition = _fn("attrition_estimate",
                    "Run the real circe-be engine on the cohort so far and return a per-inclusion-"
                    "rule attrition chart (entry-only, then each rule). Use to find a rule that "
                    "empties/collapses the cohort.", {}, [])
    relax = _fn("relax_inclusion_rule",
                "Drop one global inclusion rule by name (use when attrition shows it over-restricts "
                "and it is not clinically required).",
                {"name": {"type": "string"}}, ["name"])
    return _explore_schemas() + [_replace_schema(), attrition, relax]
