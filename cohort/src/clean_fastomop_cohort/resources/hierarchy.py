"""Read-only OMOP vocabulary-hierarchy + coverage tools for the cohort architect.

The architect picks concept-set granularity by REASONING over the hierarchy rather than a
hard-coded resolver heuristic. These tools let it walk the OMOP hierarchy (descendants,
children, parents, ancestors) and see, for each concept, how many patients the local CDM
actually codes to it — so it can bridge the literal phenotype name to the granularity the
data uses (the OHDSI "review the descendants" / "source-data granularity" practice).

Backed entirely by the local DuckDB `athena` vocab (`concept_ancestor`, `concept`) plus the
per-domain clinical tables, reusing the exact SQL patterns proven in `ConceptResolver._coverage`
and `_ancestor_level`. Everything here is read-only; `attrition_estimate` temporarily mutates
the shared builder to measure per-rule attrition and restores it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .cdm import CDM
from .concepts import ConceptResolver

# cohort id for throw-away attrition executions (never collides with experiment ids)
_ATTRITION_COHORT_ID = 9_999_997


class HierarchyResource:
    def __init__(self, cdm: CDM, resolver: ConceptResolver, executor=None):
        self.cdm = cdm
        self.resolver = resolver
        self.executor = executor
        self._domain_table = resolver._DOMAIN_TABLE  # reuse the proven domain->table map
        self._cache: Dict[tuple, Any] = {}

    # ── small helpers ────────────────────────────────────────────────────────
    def _names(self, ids: List[int]) -> Dict[int, str]:
        if not ids:
            return {}
        idlist = ",".join(str(int(i)) for i in ids)
        rows = self.cdm.query(
            f"select concept_id, concept_name from {self.cdm.vocab_schema}.concept "
            f"where concept_id in ({idlist})")
        return {int(r[0]): r[1] for r in rows}

    def _direct_counts(self, ids: List[int], domain: Optional[str]) -> Dict[int, int]:
        """Distinct patients coded DIRECTLY to each concept_id (no descendant roll-up), in one
        grouped pass — the 'which of these codes actually carry patients' review signal."""
        if not ids or domain not in self._domain_table:
            return {}
        table, col = self._domain_table[domain]
        idlist = ",".join(str(int(i)) for i in ids)
        rows = self.cdm.query(
            f"select t.{col}, count(distinct t.person_id) "
            f"from {self.cdm.cdm_schema}.{table} t where t.{col} in ({idlist}) "
            f"group by t.{col}")
        return {int(r[0]): int(r[1]) for r in rows}

    # ── walk DOWN ────────────────────────────────────────────────────────────
    def get_descendants(self, concept_id: int, domain: Optional[str] = None,
                        max_levels: Optional[int] = None, limit: int = 200) -> Dict[str, Any]:
        """Standard concepts the codeset expands to under includeDescendants, each with the
        distinct-patient count the CDM codes directly to it (closest levels first)."""
        cid = int(concept_id)
        key = ("desc", cid, domain, max_levels, limit)
        if key in self._cache:
            return self._cache[key]
        lvl = f"and ca.min_levels_of_separation <= {int(max_levels)}" if max_levels else ""
        rows = self.cdm.query(
            f"select ca.descendant_concept_id, ca.min_levels_of_separation "
            f"from {self.cdm.vocab_schema}.concept_ancestor ca "
            f"where ca.ancestor_concept_id = {cid} and ca.min_levels_of_separation >= 1 {lvl} "
            f"order by ca.min_levels_of_separation limit {int(limit) + 1}")
        truncated = len(rows) > limit
        rows = rows[:limit]
        ids = [int(r[0]) for r in rows]
        names, counts = self._names(ids), self._direct_counts(ids, domain)
        descendants = [{"concept_id": i, "name": names.get(i), "levels": int(r[1]),
                        "patients": counts.get(i, 0)} for i, r in zip(ids, rows)]
        descendants.sort(key=lambda d: -d["patients"])  # most-populated first (review priority)
        out = {"concept_id": cid, "name": (self._names([cid]).get(cid)),
               "total_coverage_with_descendants": self.resolver.coverage(cid, domain),
               "n_descendants": len(descendants), "truncated": truncated,
               "descendants": descendants}
        self._cache[key] = out
        return out

    def get_children(self, concept_id: int, domain: Optional[str] = None,
                     limit: int = 200) -> Dict[str, Any]:
        """Direct children only (one level down) with per-child patient counts."""
        out = self.get_descendants(concept_id, domain, max_levels=1, limit=limit)
        return {**out, "children": out.pop("descendants"), "n_children": out.pop("n_descendants")}

    # ── walk UP ──────────────────────────────────────────────────────────────
    def get_ancestors(self, concept_id: int, domain: Optional[str] = None,
                      max_levels: int = 3, limit: int = 50) -> Dict[str, Any]:
        """Ancestors up to `max_levels` levels, each annotated with the coverage of its WHOLE
        descendant family on the CDM — so the model sees 'Heart failure family ~30k' next to
        'Left heart failure ~282' and can bridge to the granularity the data uses."""
        cid = int(concept_id)
        key = ("anc", cid, domain, max_levels, limit)
        if key in self._cache:
            return self._cache[key]
        rows = self.cdm.query(
            f"select ca.ancestor_concept_id, ca.min_levels_of_separation "
            f"from {self.cdm.vocab_schema}.concept_ancestor ca "
            f"where ca.descendant_concept_id = {cid} and ca.min_levels_of_separation between 1 "
            f"and {int(max_levels)} order by ca.min_levels_of_separation limit {int(limit)}")
        ids = [int(r[0]) for r in rows]
        names = self._names(ids)
        ancestors = [{"concept_id": i, "name": names.get(i), "levels": int(r[1]),
                      "family_coverage": self.resolver.coverage(i, domain)}
                     for i, r in zip(ids, rows)]
        out = {"concept_id": cid, "name": self._names([cid]).get(cid),
               "coverage": self.resolver.coverage(cid, domain), "ancestors": ancestors}
        self._cache[key] = out
        return out

    def get_parents(self, concept_id: int, domain: Optional[str] = None) -> Dict[str, Any]:
        """Direct parents only (one level up) with each parent's family coverage."""
        out = self.get_ancestors(concept_id, domain, max_levels=1)
        return {**out, "parents": [a for a in out.pop("ancestors") if a["levels"] == 1]}

    # ── coverage ─────────────────────────────────────────────────────────────
    def concept_coverage(self, concept_id: int, domain: Optional[str]) -> Dict[str, Any]:
        """Patients matched WITH descendants vs coded DIRECTLY to the concept — the spread
        tells the model whether the phenotype lives at this node or in its subtree."""
        cid = int(concept_id)
        direct = self._direct_counts([cid], domain).get(cid, 0)
        return {"concept_id": cid, "name": self._names([cid]).get(cid),
                "coverage_with_descendants": self.resolver.coverage(cid, domain),
                "coverage_direct_only": direct}

    def _expand_select(self, desc_ids: List[int], plain_ids: List[int]) -> Optional[str]:
        """A SELECT producing a single `cid` column = the concept ids of a (partial) codeset:
        every descendant of `desc_ids` (includeDescendants) plus the bare `plain_ids`."""
        parts = []
        if desc_ids:
            parts.append(
                f"select descendant_concept_id as cid from {self.cdm.vocab_schema}."
                f"concept_ancestor where ancestor_concept_id in "
                f"({','.join(str(int(i)) for i in desc_ids)})")
        if plain_ids:
            parts.append("select * from (values " +
                         ",".join(f"({int(i)})" for i in plain_ids) + ") v(cid)")
        return " union ".join(parts) if parts else None

    def preview_codeset(self, items: List[Dict[str, Any]], domain: str) -> Dict[str, Any]:
        """Fast distinct-patient count for a CANDIDATE codeset (includes/excludes/descendants),
        computed directly in DuckDB — NO circe-be compile. A granularity signal only: it
        ignores entry/observation/index logic, so the Critic uses the real executor for final
        attrition. `items` = [{concept_id, include_descendants?, is_excluded?}, ...]."""
        if domain not in self._domain_table:
            return {"error": f"unknown domain {domain!r}", "patients": None}
        inc_d = [int(it["concept_id"]) for it in items
                 if not it.get("is_excluded") and it.get("include_descendants", True)]
        inc_p = [int(it["concept_id"]) for it in items
                 if not it.get("is_excluded") and not it.get("include_descendants", True)]
        exc_d = [int(it["concept_id"]) for it in items
                 if it.get("is_excluded") and it.get("include_descendants", True)]
        exc_p = [int(it["concept_id"]) for it in items
                 if it.get("is_excluded") and not it.get("include_descendants", True)]
        inc_sql = self._expand_select(inc_d, inc_p)
        if inc_sql is None:
            return {"patients": 0, "note": "no included concepts"}
        table, col = self._domain_table[domain]
        where = f"t.{col} in ({inc_sql})"
        exc_sql = self._expand_select(exc_d, exc_p)
        if exc_sql is not None:
            where += f" and t.{col} not in ({exc_sql})"
        n = self.cdm.scalar(
            f"select count(distinct t.person_id) from {self.cdm.cdm_schema}.{table} t where {where}")
        return {"patients": int(n or 0), "n_included": len(inc_d) + len(inc_p),
                "n_excluded": len(exc_d) + len(exc_p)}

    # ── attrition (real executor) ────────────────────────────────────────────
    def attrition_estimate(self, builder) -> Dict[str, Any]:
        """Per-inclusion-rule attrition chart, executed by the REAL circe-be engine on the
        cohort built so far. Entry-events-only first, then each rule prefix — so the Critic
        can spot a rule that empties/collapses the cohort (OHDSI 'review attrition' practice).
        Temporarily mutates the builder's inclusion rules and restores them."""
        if self.executor is None:
            return {"error": "no executor configured"}

        def size(circe) -> Optional[int]:
            ex = self.executor.execute(circe, cohort_id=_ATTRITION_COHORT_ID)
            return ex["size"] if ex.get("ok") else None

        rules = builder.inclusion_rules()
        entry = size(builder.to_circe(with_inclusion_rules=False))
        steps: List[Dict[str, Any]] = []
        prev = entry
        try:
            for k in range(1, len(rules) + 1):
                builder.set_inclusion_rules(rules[:k])
                sz = size(builder.to_circe())
                steps.append({
                    "rule": rules[k - 1].get("name"),
                    "size": sz,
                    "kept_frac_of_entry": (round(sz / entry, 3) if entry and sz is not None else None),
                    "drop_from_prev": (round(1 - sz / prev, 3) if prev and sz is not None and prev > 0 else None),
                })
                prev = sz
        finally:
            builder.set_inclusion_rules(rules)  # always restore
        full = prev if rules else entry
        biggest = max(steps, key=lambda s: (s["drop_from_prev"] or 0), default=None)
        return {
            "entry_size": entry, "full_size": full, "n_rules": len(rules),
            "collapsed": (full is not None and full <= 10 and (entry or 0) > 50),
            "steps": steps,
            "biggest_killer": biggest["rule"] if biggest and (biggest["drop_from_prev"] or 0) > 0.5 else None,
        }
