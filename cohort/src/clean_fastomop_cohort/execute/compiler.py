"""Compile native CIRCE JSON -> DuckDB SQL via the OHDSI circe-be JVM CLI.

No Maven/Gradle: we use the pre-built circe-be + SqlRender JARs (bundled in the OHDSI
CirceR/SqlRender R packages) under tools/circe-compiler/lib, plus our one compiled
CompileCohort class. This is the exact compiler ATLAS uses.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

# This MIMIC->OMOP build hashed every *_id into UINT64 (UBIGINT), with values above
# INT64_MAX. circe-be emits `cast(... as bigint)` (INT64) on those id columns, which
# overflows on DuckDB. Widen those casts to HUGEINT (INT128), which holds UINT64.
_BIGINT_CAST = re.compile(r"\bas\s+bigint\b", re.IGNORECASE)

from ..settings import settings


class CirceCompiler:
    MAIN_CLASS = "org.ohdsi.lab.CompileCohort"
    # Unqualified -> circe-be writes to `cohort`, which we pre-create as a TEMP table
    # (ATTACH ':memory:' is refused on a read-only DuckDB; TEMP tables are allowed).
    TARGET_TABLE = "cohort"

    def __init__(self, tool_dir: Optional[str] = None, java_bin: Optional[str] = None):
        self.tool_dir = Path(tool_dir or settings.circe_tool_dir)
        self.java_bin = java_bin or settings.java_bin
        classes = self.tool_dir / "build" / "classes"
        libglob = self.tool_dir / "lib" / "*"
        if not classes.exists():
            raise FileNotFoundError(f"compiled CompileCohort not found at {classes} "
                                    f"(run: clean-fastomop-cohort build-jar)")
        self.classpath = f"{classes}:{libglob}"

    def compile(self, circe_json: Dict[str, Any], cohort_id: int,
                cdm_schema: str, vocab_schema: str, timeout: float = 120.0) -> str:
        proc = subprocess.run(
            [self.java_bin, "-cp", self.classpath, self.MAIN_CLASS,
             "--cdm-schema", cdm_schema, "--vocab-schema", vocab_schema,
             "--target-table", self.TARGET_TABLE, "--target-cohort-id", str(int(cohort_id))],
            input=json.dumps(circe_json), capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"circe-be compile failed (rc={proc.returncode}): "
                               f"{(proc.stderr or '')[:800]}")
        sql = proc.stdout
        if not sql.strip():
            raise RuntimeError("circe-be produced empty SQL")
        return _BIGINT_CAST.sub("as hugeint", sql)
