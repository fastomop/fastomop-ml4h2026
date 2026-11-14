"""A minimal, ATLAS-compatible CIRCE builder.

Accumulates concept sets + primary criteria + inclusion rules and emits a native CIRCE
JSON dict that circe-be parses and executes directly (same path as the gold cohorts).
Deliberately a subset of full CIRCE — the common phenotype patterns — but every cohort
it emits is valid OHDSI CIRCE.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# native CIRCE occurrence wrappers we support
OCCURRENCE_TYPES = [
    "ConditionOccurrence", "DrugExposure", "ProcedureOccurrence",
    "Measurement", "Observation", "VisitOccurrence", "DeviceExposure",
]
# OHDSI Occurrence.Type codes
OCC_COUNT = {"exactly": 0, "at_most": 1, "at_least": 2}


class ConceptItem(dict):
    @staticmethod
    def make(concept_id: int, name: str = "", domain: str = "", vocab: str = "",
             concept_class: str = "", code: str = "", excluded: bool = False,
             include_descendants: bool = True) -> Dict[str, Any]:
        return {
            "concept": {
                "CONCEPT_ID": int(concept_id),
                "CONCEPT_NAME": name or "",
                "STANDARD_CONCEPT": "S",
                "INVALID_REASON": "V",
                "CONCEPT_CODE": code or "",
                "DOMAIN_ID": domain or "",
                "VOCABULARY_ID": vocab or "",
                "CONCEPT_CLASS_ID": concept_class or "",
            },
            "isExcluded": bool(excluded),
            "includeDescendants": bool(include_descendants),
            "includeMapped": False,
        }


class CohortBuilder:
    def __init__(self, name: str = "Generated Cohort", description: str = ""):
        self.name = name
        self.description = description
        self._concept_sets: List[Dict[str, Any]] = []
        self._next_cs_id = 0
        # CIRCE PrimaryCriteria.CriteriaList is a LIST — multiple entries are OR'd
        # (alternative entry events). Accumulate, never overwrite.
        self._primaries: List[Dict[str, Any]] = []
        self._observation_window = {"PriorDays": 0, "PostDays": 0}
        self._limit_type = "First"
        self._inclusion_rules: List[Dict[str, Any]] = []

    # --- concept sets ---
    def add_concept_set(self, name: str, items: List[Dict[str, Any]]) -> int:
        cs_id = self._next_cs_id
        self._next_cs_id += 1
        self._concept_sets.append(
            {"id": cs_id, "name": name, "expression": {"items": items}})
        return cs_id

    def codeset_ids(self) -> List[int]:
        return [cs["id"] for cs in self._concept_sets]

    # --- primary criteria ---
    def add_primary(self, occurrence_type: str, codeset_id: int,
                    limit_type: str = "First", prior_days: int = 0, post_days: int = 0,
                    value_op: Optional[str] = None, value: Optional[float] = None,
                    value_extent: Optional[float] = None) -> None:
        """Append an entry event. Multiple entries are OR'd (alternative index events).
        limit_type / observation window are cohort-level (shared) — last call wins.
        value_op/value (+ value_extent for a range) add a ValueAsNumber constraint on a
        Measurement/Observation index record (e.g. hemoglobin < 11)."""
        if occurrence_type not in OCCURRENCE_TYPES:
            raise ValueError(f"bad occurrence_type {occurrence_type!r}")
        occ: Dict[str, Any] = {"CodesetId": int(codeset_id)}
        if value is not None and value_op and occurrence_type in ("Measurement", "Observation"):
            occ["ValueAsNumber"] = self._value_clause(value_op, value, value_extent)
        self._primaries.append({occurrence_type: occ})
        self._limit_type = limit_type if limit_type in ("First", "All", "Last") else "First"
        self._observation_window = {"PriorDays": int(prior_days), "PostDays": int(post_days)}

    # --- inclusion rules ---
    def add_demographic_rule(self, name: str, age_min: Optional[int] = None,
                             age_max: Optional[int] = None,
                             gender_concept_ids: Optional[List[int]] = None) -> None:
        demo: Dict[str, Any] = {}
        if age_min is not None and age_max is not None:
            demo["Age"] = {"Value": int(age_min), "Extent": int(age_max), "Op": "bt"}
        elif age_min is not None:
            demo["Age"] = {"Value": int(age_min), "Op": "gte"}
        elif age_max is not None:
            demo["Age"] = {"Value": int(age_max), "Op": "lte"}
        if gender_concept_ids:
            demo["Gender"] = [{"CONCEPT_ID": int(g)} for g in gender_concept_ids]
        self._inclusion_rules.append({
            "name": name,
            "expression": {"Type": "ALL", "CriteriaList": [],
                           "DemographicCriteriaList": [demo], "Groups": []},
        })

    @staticmethod
    def _value_clause(value_op: str, value: float,
                      value_extent: Optional[float] = None) -> Dict[str, Any]:
        """A ValueAsNumber constraint (Op in gt|gte|lt|lte|eq|bt|!bt; Extent only for bt/!bt)."""
        d: Dict[str, Any] = {"Value": float(value), "Op": value_op}
        if value_op in ("bt", "!bt") and value_extent is not None:
            d["Extent"] = float(value_extent)
        return d

    @staticmethod
    def _event_criterion(codeset_id: int, occurrence_type: str, start_days: int,
                         end_days: int, count_op: str, count: int,
                         value_op: Optional[str] = None, value: Optional[float] = None,
                         value_extent: Optional[float] = None) -> Dict[str, Any]:
        """A single windowed correlated criterion — shared by global inclusion rules
        (add_event_rule) and per-arm correlated criteria (add_arm_correlated_criteria).
        An optional ValueAsNumber applies to Measurement/Observation criteria."""
        if occurrence_type not in OCCURRENCE_TYPES:
            raise ValueError(f"bad occurrence_type {occurrence_type!r}")
        occ: Dict[str, Any] = {"CodesetId": int(codeset_id)}
        if value is not None and value_op and occurrence_type in ("Measurement", "Observation"):
            occ["ValueAsNumber"] = CohortBuilder._value_clause(value_op, value, value_extent)
        return {
            "Criteria": {occurrence_type: occ},
            "StartWindow": {
                "Start": {"Days": abs(int(start_days)), "Coeff": -1 if start_days < 0 else 1},
                "End": {"Days": abs(int(end_days)), "Coeff": -1 if end_days < 0 else 1},
                "UseEventEnd": False,
            },
            "Occurrence": {"Type": OCC_COUNT.get(count_op, 2), "Count": int(count)},
        }

    def add_event_rule(self, name: str, codeset_id: int, occurrence_type: str = "ConditionOccurrence",
                       start_days: int = -365, end_days: int = 0,
                       count_op: str = "at_least", count: int = 1,
                       value_op: Optional[str] = None, value: Optional[float] = None,
                       value_extent: Optional[float] = None) -> None:
        criterion = self._event_criterion(codeset_id, occurrence_type, start_days,
                                           end_days, count_op, count,
                                           value_op, value, value_extent)
        self._inclusion_rules.append({
            "name": name,
            "expression": {"Type": "ALL", "CriteriaList": [criterion],
                           "DemographicCriteriaList": [], "Groups": []},
        })

    def add_arm_correlated_criteria(self, entry_index: int, codeset_id: int,
                                    occurrence_type: str = "ConditionOccurrence",
                                    start_days: int = -365, end_days: int = 0,
                                    count_op: str = "at_least", count: int = 1,
                                    group_type: str = "ALL",
                                    value_op: Optional[str] = None, value: Optional[float] = None,
                                    value_extent: Optional[float] = None) -> int:
        """Attach a correlated criterion to ONE entry-event arm (1-based entry_index),
        not the whole cohort. It lives inside that entry event's occurrence object as
        CorrelatedCriteria, so it restricts ONLY that arm's index events — the way real
        gold cohorts express '(drug A in patients with X) OR (drug B in patients with Y)'.
        Returns the number of correlated criteria now on that arm."""
        if not self._primaries:
            raise ValueError("no entry events yet — call add_primary_criteria first")
        if not (1 <= int(entry_index) <= len(self._primaries)):
            raise ValueError(
                f"entry_index {entry_index} out of range 1..{len(self._primaries)}")
        arm = self._primaries[int(entry_index) - 1]
        occ_obj = arm[next(iter(arm))]   # the arm's occurrence-type wrapper (one key)
        group = occ_obj.setdefault(
            "CorrelatedCriteria",
            {"Type": "ALL", "CriteriaList": [], "DemographicCriteriaList": [], "Groups": []})
        group["Type"] = group_type if group_type in ("ALL", "ANY") else "ALL"
        group["CriteriaList"].append(
            self._event_criterion(codeset_id, occurrence_type, start_days,
                                   end_days, count_op, count,
                                   value_op, value, value_extent))
        return len(group["CriteriaList"])

    def clear_inclusion_rules(self) -> int:
        n = len(self._inclusion_rules)
        self._inclusion_rules = []
        return n

    def n_inclusion_rules(self) -> int:
        return len(self._inclusion_rules)

    def inclusion_rules(self) -> List[Dict[str, Any]]:
        return list(self._inclusion_rules)

    def set_inclusion_rules(self, rules: List[Dict[str, Any]]) -> None:
        """Replace the global inclusion rules (used by the architect's collapse guard to
        drop an over-restrictive rule and re-emit, without rebuilding the whole cohort)."""
        self._inclusion_rules = list(rules)

    def _has_index_filter(self, with_inclusion_rules: bool) -> bool:
        """Membership depends on WHICH index event is picked when an event-based inclusion
        rule or a per-arm correlated criterion exists. There PrimaryCriteriaLimit must be
        'All' (test every index event), not 'First' — else a person whose FIRST index record
        lacks the qualifying neighbor is wrongly dropped."""
        if with_inclusion_rules and any(
                (r.get("expression") or {}).get("CriteriaList") for r in self._inclusion_rules):
            return True
        return any("CorrelatedCriteria" in arm[next(iter(arm))] for arm in self._primaries)

    # --- emit ---
    def to_circe(self, with_inclusion_rules: bool = True) -> Dict[str, Any]:
        if not self._primaries:
            raise ValueError("no primary criteria set")
        # Upgrade the default First -> All when an index-filtering rule exists (person-set
        # safe: with no such filter, All == First for distinct persons). Respect an explicit
        # non-First choice.
        limit = ("All" if (self._limit_type == "First"
                           and self._has_index_filter(with_inclusion_rules))
                 else self._limit_type)
        return {
            "ConceptSets": self._concept_sets,
            "PrimaryCriteria": {
                "CriteriaList": list(self._primaries),
                "ObservationWindow": self._observation_window,
                "PrimaryCriteriaLimit": {"Type": limit},
            },
            "QualifiedLimit": {"Type": "First"},
            "ExpressionLimit": {"Type": "First"},
            "InclusionRules": self._inclusion_rules if with_inclusion_rules else [],
            "CensoringCriteria": [],
            "CollapseSettings": {"CollapseType": "ERA", "EraPad": 0},
            "CensorWindow": {},
        }

    def is_ready(self) -> bool:
        return len(self._primaries) > 0 and len(self._concept_sets) > 0
