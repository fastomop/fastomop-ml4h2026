"""The CDM resource: one persistent read-only DuckDB connection to the local MIMIC OMOP.

A single long-lived connection is the whole point of running locally — it removes the
per-query cold-open + SSH overhead that dominated the remote setup, and it lets a
multi-statement cohort script (TEMP table DDL + inserts + final SELECT) share one
session so the TEMP table is visible across statements.
"""
from __future__ import annotations

import re
import threading
from typing import List, Optional, Sequence, Tuple

import duckdb

from ..settings import settings


# Split a SQL script into statements on top-level semicolons, ignoring semicolons
# inside single-quoted string literals. circe-be SQL has no dollar-quoting or
# semicolons-in-identifiers, so this is sufficient and avoids a full SQL parser.
def _split_statements(script: str) -> List[str]:
    """Split on top-level semicolons. Tracks single-quoted literals AND `--` line comments
    (an apostrophe inside a comment must not open a literal — that bug used to glue the
    tail of every circe-be script, including our final SELECT, into one chunk)."""
    out, buf = [], []
    in_str = in_comment = False
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if in_comment:
            buf.append(ch)
            if ch == "\n":
                in_comment = False
        elif in_str:
            buf.append(ch)
            if ch == "'":
                in_str = False
        elif ch == "-" and script.startswith("--", i):
            in_comment = True
            buf.append(ch)
        elif ch == "'":
            in_str = True
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


class CDM:
    """Persistent read-only handle on the OMOP CDM DuckDB."""

    def __init__(self, path: Optional[str] = None,
                 cdm_schema: Optional[str] = None,
                 vocab_schema: Optional[str] = None):
        self.path = path or settings.duckdb_path
        self.cdm_schema = cdm_schema or settings.cdm_schema
        self.vocab_schema = vocab_schema or settings.vocab_schema
        if not self.path:
            raise ValueError("DUCKDB_PATH is not set (see .env).")
        # read_only so we can never mutate the 95GB CDM; TEMP tables still work
        # (they live in the in-memory temp schema, not the file).
        self._base = duckdb.connect(self.path, read_only=True)
        # Optional session limits (see settings): keep DuckDB inside its RAM budget on small nodes.
        for key, val in (("memory_limit", settings.duckdb_memory_limit),
                         ("temp_directory", settings.duckdb_temp_dir),
                         ("max_temp_directory_size", settings.duckdb_max_temp_size)):
            if val:
                self._base.execute(f"SET {key}='{val}'")
        self._base.execute("SET enable_progress_bar=false")
        # One cursor per thread (a DuckDB cursor is a separate connection on the same database,
        # with its own TEMP schema), so `--workers N` can execute N cohorts concurrently while
        # the single-threaded path keeps using the base connection unchanged.
        self._local = threading.local()
        self._owner = threading.get_ident()

    @property
    def _con(self):
        if threading.get_ident() == self._owner:
            return self._base
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = self._base.cursor()
            con.execute("SET enable_progress_bar=false")
        return con

    def query(self, sql: str, params: Optional[Sequence] = None) -> List[Tuple]:
        """Run a single statement and return all rows."""
        cur = self._con.execute(sql, params) if params else self._con.execute(sql)
        return cur.fetchall()

    def run_script(self, script: str) -> None:
        """Run a multi-statement script on the one connection. Returns nothing on purpose:
        DuckDB answers DDL/INSERT/CTAS with a row-count row, so "last result set" was
        ambiguous — callers SELECT what they need afterwards (see CirceExecutor)."""
        for stmt in _split_statements(script):
            self._con.execute(stmt)

    def scalar(self, sql: str) -> object:
        rows = self.query(sql)
        return rows[0][0] if rows else None

    def close(self) -> None:
        self._base.close()

