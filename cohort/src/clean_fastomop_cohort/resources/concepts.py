"""The retrieval resource: concept-name -> OMOP concept_id via the concept-omcp turbovec store.

Thin wrapper over `concept_omcp.ConceptStore`. Loaded once. We deliberately return a
single best STANDARD concept (or None) and let the caller treat None as a hard failure —
silently dropping an unresolved term is exactly the bug that produced empty codesets.

Granularity: the top semantic match is sometimes a standard concept the CDM never codes
against (e.g. the SNOMED Clinical-Finding "Hyponatremia" (0 patients) when the data is
coded to the Disorder "Hypo-osmolality and/or hyponatremia" (20k)). When a CDM is supplied
and the top concept's codeset (with descendants) matches ZERO patients, we snap to the
highest-coverage candidate that still shares a significant name token — using the hierarchy
to roll coverage over descendants. Populated concepts are NEVER overridden (so a correct
narrow concept, including a genuinely rare phenotype, is left untouched).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..settings import settings


@dataclass
class ResolvedConcept:
    concept_id: int
    concept_name: str
    domain_id: Optional[str]
    vocabulary_id: Optional[str]
    standard_concept: Optional[str]
    score: float


class ConceptResolver:
    # domain -> (cdm clinical table, its standard concept_id column) for coverage lookups
    _DOMAIN_TABLE = {
        "Condition": ("condition_occurrence", "condition_concept_id"),
        "Drug": ("drug_exposure", "drug_concept_id"),
        "Procedure": ("procedure_occurrence", "procedure_concept_id"),
        "Measurement": ("measurement", "measurement_concept_id"),
        "Observation": ("observation", "observation_concept_id"),
        "Visit": ("visit_occurrence", "visit_concept_id"),
        "Device": ("device_exposure", "device_concept_id"),
    }

    def __init__(self, cdm=None):
        settings.ensure_concept_mcp_on_path()
        from concept_omcp import ConceptStore, Settings as ConceptOmcpSettings  # noqa: WPS433

        # Explicit artifact dir from our .env (else concept-omcp's own default/env).
        vm = ConceptOmcpSettings(artifact_dir=settings.concept_artifact_dir) \
            if settings.concept_artifact_dir else ConceptOmcpSettings()
        self._store = ConceptStore.load(vm)
        # Optional CDM handle. When present it powers coverage lookups (_coverage / coverage)
        # that back the hierarchy tools (HierarchyResource) and add_concept_set's zero-coverage
        # warning. Resolution itself NO LONGER auto-broadens — picking the right granularity is
        # the architect's decision, made by reasoning over the hierarchy tools (see resolve()).
        self._cdm = cdm
        self._cov_cache: Dict[Tuple[int, str], Optional[int]] = {}
        self._anc_cache: Dict[Tuple[int, int], Optional[int]] = {}

    def search(self, name: str, domain: Optional[str] = None, top_k: int = 10,
               min_similarity: float = 0.0) -> List[ResolvedConcept]:
        matches = self._store.search_concepts(
            name, domain_filter=domain, top_k=top_k, min_similarity=min_similarity,
            use_hybrid_search=True,
        )
        out: List[ResolvedConcept] = []
        for m in matches:
            out.append(ResolvedConcept(
                concept_id=int(m.concept_id),
                concept_name=getattr(m, "concept_name", None),
                domain_id=getattr(m, "domain_id", None),
                vocabulary_id=getattr(m, "vocabulary_id", None),
                standard_concept=getattr(m, "standard_concept", None),
                score=float(getattr(m, "score", 0.0) or 0.0),
            ))
        return out

    def _coverage(self, concept_id: int, domain: Optional[str]) -> Optional[int]:
        """Distinct patients matched by this concept WITH descendants in the CDM, or None
        if no CDM / unknown domain. The descendant roll-up uses the concept hierarchy
        (concept_ancestor), matching how the codeset actually executes (includeDescendants).
        Used only as a 0-vs-not-0 / ranking signal, so the result is cached."""
        if self._cdm is None or domain not in self._DOMAIN_TABLE:
            return None
        key = (int(concept_id), domain)
        if key in self._cov_cache:
            return self._cov_cache[key]
        table, col = self._DOMAIN_TABLE[domain]
        cdm = self._cdm
        n = cdm.scalar(
            f"select count(distinct t.person_id) from {cdm.cdm_schema}.{table} t "
            f"where t.{col} in (select descendant_concept_id from {cdm.vocab_schema}."
            f"concept_ancestor where ancestor_concept_id = {int(concept_id)})") or 0
        self._cov_cache[key] = int(n)
        return int(n)

    def _ancestor_level(self, ancestor_id: int, descendant_id: int) -> Optional[int]:
        """Levels of separation if `ancestor_id` is a (proper) ancestor of `descendant_id`
        in the concept hierarchy, else None. Cached."""
        if self._cdm is None or ancestor_id == descendant_id:
            return None
        key = (int(ancestor_id), int(descendant_id))
        if key in self._anc_cache:
            return self._anc_cache[key]
        cdm = self._cdm
        lev = cdm.scalar(
            f"select min_levels_of_separation from {cdm.vocab_schema}.concept_ancestor "
            f"where ancestor_concept_id = {int(ancestor_id)} and "
            f"descendant_concept_id = {int(descendant_id)} and min_levels_of_separation >= 1")
        lev = int(lev) if lev is not None else None
        self._anc_cache[key] = lev
        return lev

    def resolve(self, name: str, domain: Optional[str] = None,
                for_exclusion: bool = False) -> Optional[ResolvedConcept]:
        """Best single STANDARD concept for `name` (optionally scoped to a domain), or None.

        Deliberately NO auto-broadening: choosing the right granularity (a curated
        multi-concept family vs a single narrow code) is the architect's decision, made by
        REASONING over the hierarchy tools (HierarchyResource.get_parents / get_descendants /
        concept_coverage). resolve() just returns the best name-faithful STANDARD concept the
        model asked for. A concept that matches 0 patients on the CDM is surfaced as a warning
        by add_concept_set (so the model walks the hierarchy) — never silently swapped, which
        preserves the 'no silent empty codeset' invariant without hiding the granularity call."""
        hits = self.search(name, domain=domain, top_k=10)
        if not hits:
            return None
        standard = [h for h in hits if (h.standard_concept or "S") == "S"] or hits
        return standard[0]

    def coverage(self, concept_id: int, domain: Optional[str]) -> Optional[int]:
        """Public coverage: distinct patients matched by this concept WITH descendants on the
        CDM (None if no CDM / unknown domain). Cached. Used by add_concept_set's zero-coverage
        warning and by HierarchyResource."""
        return self._coverage(concept_id, domain)

    def name_of(self, concept_id: int) -> Optional[str]:
        c = self._store.get_concept_by_id(int(concept_id))
        return getattr(c, "concept_name", None) if c else None
