"""Cohort-building tools the architect LLM calls. Each mutates a CohortBuilder and
returns a JSON result. Concept names are resolved through concept-omcp; an UNRESOLVED
name is a HARD ERROR (never silently dropped -> never an empty codeset)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..resources.concepts import ConceptResolver
from .circe_model import CohortBuilder, ConceptItem, OCCURRENCE_TYPES

# occurrence type -> concept domain (for resolution scoping)
OCC_DOMAIN = {
    "ConditionOccurrence": "Condition", "DrugExposure": "Drug",
    "ProcedureOccurrence": "Procedure", "Measurement": "Measurement",
    "Observation": "Observation", "VisitOccurrence": "Visit", "DeviceExposure": "Device",
}


class CohortToolset:
    def __init__(self, resolver: ConceptResolver, executor=None):
        self.resolver = resolver
        self.executor = executor   # CirceExecutor (unused now; kept for plumbing)
        self.builder: Optional[CohortBuilder] = None

    # ── tools ──────────────────────────────────────────────────────────────
    def start_cohort(self, name: str, description: str = "") -> str:
        self.builder = CohortBuilder(name=name, description=description)
        return json.dumps({"ok": True, "message": f"started cohort {name!r}"})

    def add_concept_set(self, name: str, concept_names: List[str], domain: str,
                        exclude_names: Optional[List[str]] = None,
                        include_descendants: bool = True) -> str:
        if self.builder is None:
            return json.dumps({"error": "call start_cohort first"})
        items, unresolved, resolved = [], [], []
        for nm in concept_names:
            r = self.resolver.resolve(nm, domain=domain)
            if not r:
                unresolved.append(nm); continue
            items.append(ConceptItem.make(r.concept_id, r.concept_name, r.domain_id,
                                           r.vocabulary_id, include_descendants=include_descendants))
            resolved.append({"name": nm, "concept_id": r.concept_id, "resolved_to": r.concept_name,
                             "coverage": self.resolver.coverage(r.concept_id, domain)})
        for nm in (exclude_names or []):
            r = self.resolver.resolve(nm, domain=domain, for_exclusion=True)
            if not r:
                unresolved.append(nm); continue
            items.append(ConceptItem.make(r.concept_id, r.concept_name, r.domain_id,
                                          r.vocabulary_id, excluded=True,
                                          include_descendants=include_descendants))
            resolved.append({"name": nm, "concept_id": r.concept_id, "resolved_to": r.concept_name, "excluded": True})
        if unresolved:
            return json.dumps({"error": "could not resolve concept name(s) — fix the wording and retry",
                               "unresolved": unresolved, "resolved": resolved})
        cs_id = self.builder.add_concept_set(name, items)
        out: Dict[str, Any] = {"ok": True, "codeset_id": cs_id, "resolved": resolved}
        zero = [x["resolved_to"] for x in resolved if x.get("coverage") == 0]
        if zero:
            out["warning"] = (
                f"resolved concept(s) match 0 patients on the CDM: {zero}. The data likely codes "
                f"this phenotype at a different granularity — call get_parents / get_descendants / "
                f"lookup_concept to find the populated concept, then add_concept_set_by_ids.")
        return json.dumps(out)

    def add_primary_criteria(self, occurrence_type: str, codeset_id: int,
                             limit_type: str = "First", prior_days: int = 0,
                             post_days: int = 0, value_op: Optional[str] = None,
                             value: Optional[float] = None,
                             value_extent: Optional[float] = None) -> str:
        if self.builder is None:
            return json.dumps({"error": "call start_cohort first"})
        if codeset_id not in self.builder.codeset_ids():
            return json.dumps({"error": f"codeset_id {codeset_id} does not exist"})
        try:
            self.builder.add_primary(occurrence_type, codeset_id, limit_type, prior_days,
                                     post_days, value_op, value, value_extent)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        n = len(self.builder._primaries)
        return json.dumps({"ok": True, "n_entry_events": n,
                           "message": f"entry event #{n} = {occurrence_type} on codeset {codeset_id} "
                                      f"({'OR-ed with the others' if n > 1 else 'index event'})"})

    def add_demographic_rule(self, name: str, age_min: Optional[int] = None,
                             age_max: Optional[int] = None,
                             gender_concept_ids: Optional[List[int]] = None) -> str:
        if self.builder is None:
            return json.dumps({"error": "call start_cohort first"})
        self.builder.add_demographic_rule(name, age_min, age_max, gender_concept_ids)
        return json.dumps({"ok": True, "message": f"added demographic rule {name!r}"})

    def add_event_rule(self, name: str, codeset_id: int, occurrence_type: str = "ConditionOccurrence",
                       start_days: int = -365, end_days: int = 0,
                       count_op: str = "at_least", count: int = 1,
                       value_op: Optional[str] = None, value: Optional[float] = None,
                       value_extent: Optional[float] = None) -> str:
        if self.builder is None:
            return json.dumps({"error": "call start_cohort first"})
        if codeset_id not in self.builder.codeset_ids():
            return json.dumps({"error": f"codeset_id {codeset_id} does not exist"})
        try:
            self.builder.add_event_rule(name, codeset_id, occurrence_type, start_days,
                                        end_days, count_op, count,
                                        value_op, value, value_extent)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"ok": True, "message": f"added event rule {name!r}"})

    def add_arm_correlated_criteria(self, entry_event_index: int, codeset_id: int,
                                    occurrence_type: str = "ConditionOccurrence",
                                    start_days: int = -365, end_days: int = 0,
                                    count_op: str = "at_least", count: int = 1,
                                    group_type: str = "ALL",
                                    value_op: Optional[str] = None, value: Optional[float] = None,
                                    value_extent: Optional[float] = None) -> str:
        if self.builder is None:
            return json.dumps({"error": "call start_cohort first"})
        if codeset_id not in self.builder.codeset_ids():
            return json.dumps({"error": f"codeset_id {codeset_id} does not exist"})
        try:
            nc = self.builder.add_arm_correlated_criteria(
                entry_event_index, codeset_id, occurrence_type,
                start_days, end_days, count_op, count, group_type,
                value_op, value, value_extent)
        except (ValueError, KeyError) as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"ok": True, "entry_event_index": entry_event_index,
                           "n_correlated_criteria_on_arm": nc,
                           "message": f"correlated criterion bound to entry event "
                                      f"#{entry_event_index} ONLY (not the whole cohort)"})

    def finalize(self) -> str:
        if self.builder is None or not self.builder.is_ready():
            return json.dumps({"error": "cohort incomplete — need >=1 concept set and primary criteria"})
        return json.dumps({"ok": True, "message": "cohort complete", "codesets": self.builder.codeset_ids()})


# ── OpenAI tool schemas ─────────────────────────────────────────────────────
def tool_schemas() -> List[Dict[str, Any]]:
    occ = {"type": "string", "enum": OCCURRENCE_TYPES}
    val = {
        "value_op": {"type": "string", "enum": ["gt", "gte", "lt", "lte", "eq", "bt", "!bt"],
                     "description": "Numeric ValueAsNumber test on a Measurement/Observation only "
                                    "(e.g. lt for 'hemoglobin < 11', gte for 'HbA1c >= 6.5'); use bt "
                                    "with value_extent for a range. Prefer bt/gte/lte over eq."},
        "value": {"type": "number", "description": "Threshold value on the measurement scale."},
        "value_extent": {"type": "number", "description": "Upper bound; only with value_op bt/!bt."},
    }
    def fn(name, desc, props, required):
        return {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}
    return [
        fn("start_cohort", "Begin a new cohort. Call FIRST.",
           {"name": {"type": "string"}, "description": {"type": "string"}}, ["name"]),
        fn("add_concept_set",
           "Resolve clinical term(s) to an OMOP concept set (descendants included by default). "
           "domain MUST match the clinical type. Returns codeset_id. Errors (with 'unresolved') "
           "if a name can't be resolved — fix the wording and retry; never proceed with a missing concept.",
           {"name": {"type": "string"},
            "concept_names": {"type": "array", "items": {"type": "string"}},
            "domain": {"type": "string", "enum": ["Condition", "Drug", "Procedure", "Measurement", "Observation", "Visit", "Device"]},
            "exclude_names": {"type": "array", "items": {"type": "string"}, "description": "Optional terms to exclude."},
            "include_descendants": {"type": "boolean", "default": True}},
           ["name", "concept_names", "domain"]),
        fn("add_primary_criteria",
           "Add an ENTRY (index) event: one occurrence_type on an existing codeset_id. "
           "If the cohort is 'indexed on A OR B OR C' (e.g. 'condition or observation of X', "
           "'diagnosis or a symptom or a drug'), call this ONCE PER ALTERNATIVE — the entry "
           "events are OR'd. Use this for alternative entry events; do NOT put them in inclusion "
           "rules (that would wrongly REQUIRE all of them). prior_days/post_days = required "
           "continuous observation around index (cohort-level).",
           {"occurrence_type": occ, "codeset_id": {"type": "integer"},
            "limit_type": {"type": "string", "enum": ["First", "All", "Last"], "default": "First"},
            "prior_days": {"type": "integer", "default": 0}, "post_days": {"type": "integer", "default": 0},
            **val},
           ["occurrence_type", "codeset_id"]),
        fn("add_demographic_rule", "Inclusion rule on age-at-index / gender (8507=M, 8532=F).",
           {"name": {"type": "string"}, "age_min": {"type": "integer"}, "age_max": {"type": "integer"},
            "gender_concept_ids": {"type": "array", "items": {"type": "integer"}}},
           ["name"]),
        fn("add_event_rule",
           "Correlated inclusion rule: occurrences of a codeset within [start_days,end_days] of index "
           "(negative=before). count_op in {at_least,at_most,exactly} with count (e.g. at_most 0 = exclusion).",
           {"name": {"type": "string"}, "codeset_id": {"type": "integer"}, "occurrence_type": occ,
            "start_days": {"type": "integer", "default": -365}, "end_days": {"type": "integer", "default": 0},
            "count_op": {"type": "string", "enum": ["at_least", "at_most", "exactly"], "default": "at_least"},
            "count": {"type": "integer", "default": 1}, **val},
           ["name", "codeset_id"]),
        fn("add_arm_correlated_criteria",
           "Attach a correlated requirement to ONE specific entry-event arm "
           "(entry_event_index, as returned by add_primary_criteria), NOT the whole cohort. "
           "Use when an alternative index event carries its OWN restriction — e.g. the cohort "
           "is '(drug A in patients who also have condition X) OR (drug B in patients with Y)': "
           "bind X to arm 1 and Y to arm 2. This restricts only that arm's index events; a "
           "global inclusion rule (add_event_rule) would wrongly force the requirement on EVERY "
           "arm. Window [start_days,end_days] is relative to that arm's index (negative=before). "
           "count_op 'at_most' with count 0 = a per-arm exclusion.",
           {"entry_event_index": {"type": "integer"}, "codeset_id": {"type": "integer"},
            "occurrence_type": occ,
            "start_days": {"type": "integer", "default": -365}, "end_days": {"type": "integer", "default": 0},
            "count_op": {"type": "string", "enum": ["at_least", "at_most", "exactly"], "default": "at_least"},
            "count": {"type": "integer", "default": 1},
            "group_type": {"type": "string", "enum": ["ALL", "ANY"], "default": "ALL"}, **val},
           ["entry_event_index", "codeset_id"]),
        fn("finalize", "Finish: validate the cohort is complete. Call LAST.", {}, []),
    ]
