"""Execute a CIRCE cohort exactly (via circe-be) against the local DuckDB CDM.

The compiled OHDSI script writes the cohort into `results.cohort`. We ATTACH an in-memory
`results` database (writable) so the 95GB CDM stays read-only, run the multi-statement
script on the ONE persistent connection (so its TEMP tables + the attached schema are
visible across statements), then SELECT the person set back.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Set

from ..resources.cdm import CDM
from .compiler import CirceCompiler

# circe-be's working TEMP tables — dropped defensively in case a prior run died mid-script.
_TEMP_TABLES = [
    "Codesets", "qualified_events", "inclusion_events", "included_events",
    "cohort_rows", "final_cohort", "best_events", "inclusion_rules",
]


class CirceExecutor:
    def __init__(self, cdm: CDM, compiler: Optional[CirceCompiler] = None):
        self.cdm = cdm
        self.compiler = compiler or CirceCompiler()
        self._ready = False

    def _ensure_results(self) -> None:
        # Cheap (IF NOT EXISTS) and TEMP tables are per-connection, so with thread-local
        # cursors (CDM._con) every worker thread creates its own on first use.
        # TEMP table (in-memory, connection-scoped) — allowed on a read-only DuckDB,
        # unlike ATTACH ':memory:'. circe-be's unqualified `cohort` resolves here.
        # subject_id is HUGEINT because this CDM's person_id is UBIGINT (UINT64 > INT64).
        self.cdm.query(
            "CREATE TEMP TABLE IF NOT EXISTS cohort "
            "(cohort_definition_id BIGINT, subject_id HUGEINT, "
            " cohort_start_date DATE, cohort_end_date DATE)"
        )
        self._ready = True

    def execute(self, circe_json: Dict[str, Any], cohort_id: int) -> Dict[str, Any]:
        """Returns {ok, size, person_ids, seconds, error}."""
        self._ensure_results()
        t0 = time.time()
        try:
            sql = self.compiler.compile(
                circe_json, cohort_id, self.cdm.cdm_schema, self.cdm.vocab_schema)
        except Exception as exc:
            return {"ok": False, "size": None, "person_ids": set(),
                    "seconds": round(time.time() - t0, 1), "error": f"compile: {exc}"}

        pre = "".join(f"DROP TABLE IF EXISTS {t};\n" for t in _TEMP_TABLES)
        # The script populates `cohort`; the person set is SELECTed separately afterwards, so
        # a DDL/INSERT row-count can never masquerade as a subject (that produced a phantom
        # subject 0 for every EMPTY cohort before 2026-09-08).
        final = (f"SELECT subject_id, cohort_start_date, cohort_end_date "
                 f"FROM cohort WHERE cohort_definition_id = {int(cohort_id)}")
        try:
            self.cdm.query(f"DELETE FROM cohort WHERE cohort_definition_id = {int(cohort_id)}")
            self.cdm.run_script(pre + sql)
            rows = self.cdm.query(final)
        except Exception as exc:
            return {"ok": False, "size": None, "person_ids": set(),
                    "seconds": round(time.time() - t0, 1), "error": f"execute: {exc}"}

        persons: Set[int] = {int(r[0]) for r in rows}
        return {"ok": True, "size": len(persons), "person_ids": persons,
                "seconds": round(time.time() - t0, 1), "error": None}
