"""Evaluation harness for the OMOP NL->SQL pipeline.

Runs a dataset of `{id, input, expected_output (gold SQL), execution_result (gold rows)}`
through the pipeline and, per query, compares against the gold:

  1. **Database results** — the rows the agent's `Select_Query` returned vs `execution_result`.
  2. **The SQL** — the agent's `Select_Query` argument vs `expected_output` (soft signal).

Execution accuracy (do the agent's rows match the gold rows?) is the headline metric.
The gold dataset must be generated against the **same database** the agent queries (see
`foem` + `datasets/mimic_duckdb_gold.json`), so the row comparison is valid by construction.

Outputs (under eval_runs/<timestamp>/): records.jsonl, report.json, report.md.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from clean_fastomop.agents import (
    build_database_agent,
    build_naive_database_agent,
    build_semantic_agent,
)
from clean_fastomop.config import ROOT_DIR, get_agent_config, get_mcp_config, get_validation_config
from clean_fastomop.integrations import connect_concept_omcp, connect_omcp, unwrap_mcp_result_text
from clean_fastomop.observability import setup_observability
from clean_fastomop.refinement import (
    RefineTrace,
    make_refinement_gate,
    run_refinement_pipeline,
)
from clean_fastomop.validation import conformance
from clean_fastomop.workflow import run_pipeline

DEFAULT_DATASET = ROOT_DIR / "datasets" / "mimic_duckdb_gold.json"


# --------------------------------------------------------------------------- #
# Result / SQL comparison
# --------------------------------------------------------------------------- #

def _norm_cell(v: Any) -> Any:
    """Canonicalise one cell so '318265' (CSV str) and 318265 (gold int) compare equal."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("none", "null", "nan"):
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() else round(f, 6)
    except (ValueError, OverflowError):
        return s.lower()


def _canon_cell(v: Any) -> str:
    n = _norm_cell(v)
    if n is None:
        return "∅"
    if isinstance(n, bool):
        return f"b:{int(n)}"
    if isinstance(n, int):
        return f"n:{n}"
    if isinstance(n, float):
        return f"f:{n!r}"
    return f"s:{n}"


def _canon_rows(rows: List[List[Any]], *, cells_ordered: bool) -> List[tuple]:
    """A sorted (order-insensitive across rows) list of canonical row tuples.

    cells_ordered=False also sorts cells within each row, tolerating column reordering.
    """
    out = []
    for row in rows:
        cells = [_canon_cell(c) for c in row]
        if not cells_ordered:
            cells = sorted(cells)
        out.append(tuple(cells))
    return sorted(out)


def compare_rows(agent_rows: List[List[Any]], gold_rows: List[List[Any]]) -> Dict[str, bool]:
    """Return strict (column-order-sensitive) and relaxed (order-insensitive) match flags."""
    strict = _canon_rows(agent_rows, cells_ordered=True) == _canon_rows(gold_rows, cells_ordered=True)
    relaxed = _canon_rows(agent_rows, cells_ordered=False) == _canon_rows(gold_rows, cells_ordered=False)
    return {"strict": strict, "relaxed": relaxed}


def count_match(agent_rows: List[List[Any]], gold_rows: List[List[Any]]) -> bool:
    """Did the agent get the aggregate (count) column right, even if labels differ?

    A looser-than-exact signal for grouped queries: gold's COUNT is its LAST column (foem's
    SELECT lists dimensions first, the aggregate last). Returns True iff some column of the
    agent result has the SAME value-multiset as gold's count column. This credits results
    where the counts/grouping are correct but the dimension labels are formatted differently
    (concept_id vs name, 'No matching concept' vs 'Unknown', an extra id column). It still
    fails when the grouping differs (row counts differ → multisets can't match).
    """
    if not gold_rows:
        return len(agent_rows) == 0
    gold_counts = sorted(_canon_cell(r[-1]) for r in gold_rows if r)
    ncols = max((len(r) for r in agent_rows), default=0)
    for ci in range(ncols):
        col = sorted(_canon_cell(r[ci]) for r in agent_rows if ci < len(r))
        if col == gold_counts:
            return True
    return False


def _coerce_result_text(result: Any) -> Optional[str]:
    """Agent/tool results arrive as omcp's double-wrapped JSON; peel to the inner text."""
    if result is None:
        return None
    text, _ = unwrap_mcp_result_text(result)
    return text


def parse_csv_rows(text: Optional[str]) -> List[List[str]]:
    """Parse omcp's `df.to_csv(index=False)` output into data rows (header dropped)."""
    text = _coerce_result_text(text)
    if not text or not text.strip():
        return []
    rows = list(csv.reader(io.StringIO(text.strip())))
    return rows[1:] if len(rows) > 1 else []


def _norm_sql(sql: Optional[str]) -> str:
    if not sql:
        return ""
    s = sql.strip().rstrip(";").strip()
    return re.sub(r"\s+", " ", s).lower()


def sql_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Token Jaccard over normalised SQL — a soft 'how close' signal, not a pass/fail."""
    ta, tb = set(_norm_sql(a).split()), set(_norm_sql(b).split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --------------------------------------------------------------------------- #
# FastSSV conformance scoring (post-hoc, pure)
# --------------------------------------------------------------------------- #
#
# Scoring is deliberately decoupled from the run: it reads `agent_sql` out of records and
# re-validates it, so (a) historical runs can be scored retroactively and (b) both arms of a
# gated-vs-ungated comparison are always judged by the SAME ruler, whatever validation config
# each arm actually ran under. Comparing arms scored under different rules would compare
# rulers as much as queries.

CONFORMANCE_FIELDS = (
    "fastssv_scored",
    "fastssv_n_errors",
    "fastssv_n_warnings",
    "fastssv_error_rules",
    "fastssv_warning_rules",
)


def score_conformance(
    records: List[Dict[str, Any]],
    *,
    dialect: str = "duckdb",
    strict: bool = True,
    policy: str = "epidemiological",
    force: bool = False,
) -> int:
    """Attach FastSSV conformance fields to each record in place; return the number scored.

    Records without analytic SQL (`no_sql`, pipeline errors, introspection-only) get
    `fastssv_scored=False` and are excluded from conformance denominators — they are the
    *cost* side of gating and are counted separately in the summary.
    """
    n = 0
    for rec in records:
        if not force and "fastssv_scored" in rec:
            n += 1 if rec.get("fastssv_scored") else 0
            continue
        c = conformance(rec.get("agent_sql") or "", dialect=dialect, strict=strict, policy=policy)
        rec["fastssv_scored"] = c["scored"]
        rec["fastssv_n_errors"] = c["n_errors"]
        rec["fastssv_n_warnings"] = c["n_warnings"]
        rec["fastssv_error_rules"] = c["error_rules"]
        rec["fastssv_warning_rules"] = c["warning_rules"]
        n += 1 if c["scored"] else 0
    return n


def _rule_query_counts(records: List[Dict[str, Any]], field: str) -> Dict[str, int]:
    """Rule id -> number of QUERIES firing it (not number of violations)."""
    counter: Counter = Counter()
    for r in records:
        counter.update(set(r.get(field) or []))
    return dict(counter.most_common())


def _conformance_summary(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Warning-focused FastSSV conformance stats (None if nothing was scored)."""
    scored = [r for r in records if r.get("fastssv_scored")]
    if not scored:
        return None
    n = len(scored)
    warns = [r.get("fastssv_n_warnings", 0) for r in scored]
    errs = [r.get("fastssv_n_errors", 0) for r in scored]
    warn_clean = sum(1 for w in warns if w == 0)
    err_clean = sum(1 for e in errs if e == 0)
    both_clean = sum(1 for r in scored if not r.get("fastssv_n_warnings") and not r.get("fastssv_n_errors"))
    return {
        "scored": n,
        "unscored_no_sql": len(records) - n,
        # Headline: warnings are never blocked by the gate, so this is comparable across arms.
        "warning_clean": {"n": warn_clean, "of": n, "rate": _pct(warn_clean, n)},
        "warnings_per_query": {
            "total": sum(warns),
            "mean": round(statistics.mean(warns), 3) if warns else None,
            "median": statistics.median(warns) if warns else None,
            "max": max(warns) if warns else None,
        },
        # Mechanical under a gated run (the gate blocks errors), reported for completeness.
        "error_clean": {"n": err_clean, "of": n, "rate": _pct(err_clean, n)},
        "errors_per_query": {
            "total": sum(errs),
            "mean": round(statistics.mean(errs), 3) if errs else None,
        },
        "fully_clean": {"n": both_clean, "of": n, "rate": _pct(both_clean, n)},
        "warning_rules": _rule_query_counts(scored, "fastssv_warning_rules"),
        "error_rules": _rule_query_counts(scored, "fastssv_error_rules"),
    }


# --------------------------------------------------------------------------- #
# Per-query evaluation
# --------------------------------------------------------------------------- #

# status values
OK = "ok"
RESULT_MISMATCH = "result_mismatch"
SQL_EXEC_ERROR = "sql_exec_error"
NO_SQL = "no_sql"
PIPELINE_ERROR = "pipeline_error"


def _preview(rows: List[List[Any]], n: int = 5) -> List[List[Any]]:
    return rows[:n]


def _init_record(item: Dict[str, Any]) -> Dict[str, Any]:
    gold_rows = item.get("execution_result") or []
    return {
        "id": item.get("id"),
        "input": item.get("input", ""),
        "gold_sql": item.get("expected_output"),
        "agent_sql": None,
        "status": None,
        "error": None,
        "sql_exact_match": False,
        "sql_similarity": None,
        "exec_match": False,
        "exec_match_strict": False,
        "count_match": False,  # aggregate column correct even if labels/columns differ
        "agent_row_count": None,
        "gold_row_count": len(gold_rows),
        "agent_rows_preview": None,
        "latency_s": None,
    }


def _score_run(rec: Dict[str, Any], run: Any, gold_rows: List[List[Any]]) -> Dict[str, Any]:
    """Fill `rec` (SQL match + execution match) from a completed run.

    Accepts any run object exposing `.sql`, `.sql_error`, `.result` (PipelineResult or
    RefinementResult), so the production and refinement paths share one scorer.
    """
    rec["agent_sql"] = run.sql
    rec["sql_exact_match"] = bool(run.sql) and _norm_sql(run.sql) == _norm_sql(rec["gold_sql"])
    rec["sql_similarity"] = round(sql_similarity(run.sql, rec["gold_sql"]), 4)

    if run.sql is None:
        rec["status"] = NO_SQL
        return rec
    if run.sql_error:
        rec["status"] = SQL_EXEC_ERROR
        rec["error"] = (_coerce_result_text(run.result) or "")[:500]
        return rec

    agent_rows = parse_csv_rows(run.result)
    rec["agent_row_count"] = len(agent_rows)
    rec["agent_rows_preview"] = _preview(agent_rows)

    cmp = compare_rows(agent_rows, gold_rows)
    rec["exec_match"] = cmp["relaxed"]
    rec["exec_match_strict"] = cmp["strict"]
    rec["count_match"] = cmp["relaxed"] or count_match(agent_rows, gold_rows)
    rec["status"] = OK if cmp["relaxed"] else RESULT_MISMATCH
    return rec


async def evaluate_one(item: Dict[str, Any], semantic, database) -> Dict[str, Any]:
    """Run one query through the pipeline and compare the agent's SQL + rows to gold."""
    rec = _init_record(item)
    gold_rows = item.get("execution_result") or []

    t0 = time.perf_counter()
    try:
        run = await run_pipeline(rec["input"], semantic, database)
    except Exception as exc:  # noqa: BLE001 - record any pipeline failure
        rec["status"] = PIPELINE_ERROR
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["latency_s"] = round(time.perf_counter() - t0, 3)
        return rec
    rec["latency_s"] = round(time.perf_counter() - t0, 3)
    return _score_run(rec, run, gold_rows)


def _attach_refine_fields(rec: Dict[str, Any], trace: RefineTrace, max_refine: int) -> None:
    """Record the FastSSV refinement loop's convergence signal on `rec`.

    `refine_rounds` = analytic-SQL submissions validated (introspection bypassed, not counted).
    `converged` = the executed query was clean (last validated round had 0 violations).
    `refine_budget_hit` = the loop spent all `max_refine` blocking rounds and executed anyway.
    """
    rounds = trace.rounds
    last = rounds[-1] if rounds else None
    blocking = [r for r in rounds if r["n_violations"] > 0]
    rec["refine_rounds"] = len(rounds)
    rec["refine_final_violations"] = last["n_violations"] if last else 0
    rec["converged"] = bool(rounds) and last["n_violations"] == 0
    rec["refine_budget_hit"] = len(blocking) >= max_refine and bool(last) and last["n_violations"] > 0
    rec["refine_trace"] = [
        {"round": r["round"], "n_violations": r["n_violations"], "rules": r["rules"]}
        for r in rounds
    ]


async def evaluate_one_gated(
    item: Dict[str, Any], semantic, omcp_tools, max_rounds: int = 3,
) -> Dict[str, Any]:
    """Run one query through the PRODUCTION agent behind a warning-feedback gate.

    The treatment arm for "does conformance feedback improve the SQL?". Unlike
    `evaluate_one_refine` the agent keeps every skill it has in production (including
    `omop-best-practices`); the only thing that changes versus the control arm is that the gate
    also feeds back WARNING-severity violations. Holding the agent fixed is what makes the
    contrast attributable to the feedback rather than to a different prompt.

    The gate carries a round budget and a trace, so it is stateful — a fresh agent and gate are
    built per query rather than shared across the concurrent workers.
    """
    rec = _init_record(item)
    gold_rows = item.get("execution_result") or []
    trace = RefineTrace()
    database = build_database_agent(
        omcp_tools, True, feedback_severity="all", max_feedback_rounds=max_rounds, trace=trace,
    )

    t0 = time.perf_counter()
    try:
        run = await run_pipeline(rec["input"], semantic, database)
    except Exception as exc:  # noqa: BLE001 - record any pipeline failure
        rec["status"] = PIPELINE_ERROR
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["latency_s"] = round(time.perf_counter() - t0, 3)
        _attach_refine_fields(rec, trace, max_rounds)
        return rec
    rec["latency_s"] = round(time.perf_counter() - t0, 3)
    _score_run(rec, run, gold_rows)
    _attach_refine_fields(rec, trace, max_rounds)
    return rec


async def evaluate_one_refine(
    item: Dict[str, Any], semantic, omcp_tools, max_refine: int = 5,
    policy: str = "epidemiological",
) -> Dict[str, Any]:
    """Run one query through the naive/refine pipeline (fresh gate + trace per query).

    The refinement gate is stateful (round counter + trace), so — unlike the production path's
    shared database agent — a fresh naive agent, gate, and trace are built per query. Agent
    construction is a pure in-process constructor over the shared omcp toolset (no network).
    """
    rec = _init_record(item)
    gold_rows = item.get("execution_result") or []
    trace = RefineTrace()
    gate = make_refinement_gate(trace, max_refine=max_refine, policy=policy)
    naive_database = build_naive_database_agent(omcp_tools, tool_hooks=[gate])

    t0 = time.perf_counter()
    try:
        run = await run_refinement_pipeline(rec["input"], semantic, naive_database, trace)
    except Exception as exc:  # noqa: BLE001 - record any pipeline failure
        rec["status"] = PIPELINE_ERROR
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["latency_s"] = round(time.perf_counter() - t0, 3)
        _attach_refine_fields(rec, trace, max_refine)
        return rec
    rec["latency_s"] = round(time.perf_counter() - t0, 3)
    _score_run(rec, run, gold_rows)
    _attach_refine_fields(rec, run.trace, max_refine)
    return rec


# --------------------------------------------------------------------------- #
# Dataset selection
# --------------------------------------------------------------------------- #

def load_dataset(path: Path) -> List[Dict[str, Any]]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list of queries in {path}")
    return data


def select_items(
    data: List[Dict[str, Any]],
    *,
    ids: Optional[List[int]] = None,
    limit: Optional[int] = None,
    sample: Optional[int] = None,
    seed: int = 0,
) -> List[Dict[str, Any]]:
    if ids:
        wanted = set(ids)
        items = [d for d in data if d.get("id") in wanted]
    elif sample:
        import random

        rng = random.Random(seed)
        items = rng.sample(data, min(sample, len(data)))
        items.sort(key=lambda d: d.get("id", 0))
    else:
        items = list(data)
    if limit is not None:
        items = items[:limit]
    return items


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

@dataclass
class EvalConfig:
    out_dir: Path
    concurrency: int = 4
    resume: bool = False
    refine: bool = False       # naive/refine mode: template-free agent + FastSSV feedback loop
    max_refine: int = 5
    policy: str = "epidemiological"  # FastSSV feedback policy (refine mode): clinical | epidemiological
    # Production-mode FastSSV gate. None = follow [validation].enabled; False = ungated control
    # arm for a gated-vs-ungated comparison. Ignored in refine mode (the loop IS the gate).
    validation_enabled: Optional[bool] = None
    # What the production gate makes the agent react to: "error" (safety gate only) or "all"
    # (warnings fed back too — the warning-feedback treatment arm, agent otherwise unchanged).
    gate_feedback: str = "error"
    max_gate_rounds: int = 3


def _load_done_ids(jsonl: Path) -> set:
    done = set()
    if jsonl.exists():
        with open(jsonl) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line).get("id"))
                except json.JSONDecodeError:
                    continue
    return done


async def run_eval(items: List[Dict[str, Any]], cfg: EvalConfig) -> List[Dict[str, Any]]:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = cfg.out_dir / "records.jsonl"

    done_ids = _load_done_ids(jsonl_path) if cfg.resume else set()
    pending = [it for it in items if it.get("id") not in done_ids]
    if done_ids:
        print(f"[eval] resume: {len(done_ids)} done, {len(pending)} remaining", file=sys.stderr)

    concept_cfg = get_mcp_config("concept_omcp")
    omcp_cfg = get_mcp_config("omcp")

    records: List[Dict[str, Any]] = []
    write_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    counter = {"done": 0}
    total = len(pending)
    t_start = time.perf_counter()

    # Append-mode so a crash/resume keeps prior lines.
    jsonl_file = open(jsonl_path, "a")

    async with connect_concept_omcp(concept_cfg) as concept_tools, connect_omcp(omcp_cfg) as omcp_tools:
        semantic = build_semantic_agent(concept_tools)
        # An error-only gate is stateless, so production mode shares one database agent. Refine
        # mode and the warning-feedback arm carry a round budget + trace, so those workers build
        # a fresh agent per query from the shared toolset.
        stateful = cfg.refine or cfg.gate_feedback == "all"
        database = None if stateful else build_database_agent(omcp_tools, cfg.validation_enabled)

        async def worker(item: Dict[str, Any]) -> None:
            async with sem:
                if cfg.refine:
                    rec = await evaluate_one_refine(item, semantic, omcp_tools, cfg.max_refine, cfg.policy)
                elif cfg.gate_feedback == "all":
                    rec = await evaluate_one_gated(item, semantic, omcp_tools, cfg.max_gate_rounds)
                else:
                    rec = await evaluate_one(item, semantic, database)
            async with write_lock:
                jsonl_file.write(json.dumps(rec, default=str) + "\n")
                jsonl_file.flush()
                records.append(rec)
                counter["done"] += 1
                n = counter["done"]
                if n % 10 == 0 or n == total:
                    elapsed = time.perf_counter() - t_start
                    rate = n / elapsed if elapsed else 0
                    eta = (total - n) / rate if rate else 0
                    print(
                        f"[eval] {n}/{total}  ({rate:.2f} q/s, eta {eta/60:.1f} min)  "
                        f"last: id={rec['id']} {rec['status']}",
                        file=sys.stderr,
                    )

        try:
            await asyncio.gather(*(worker(it) for it in pending))
        finally:
            jsonl_file.close()

    # On resume, fold in previously-completed records for the report.
    if done_ids:
        records = _read_all_records(jsonl_path)
    records.sort(key=lambda r: (r.get("id") is None, r.get("id")))
    return records


def _read_all_records(jsonl_path: Path) -> List[Dict[str, Any]]:
    out = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _pct(num: int, den: int) -> str:
    return f"{(100.0 * num / den):.1f}%" if den else "n/a"


def _refinement_summary(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convergence stats for a naive/refine run (None if not a refine run)."""
    refs = [r for r in records if "refine_rounds" in r]
    if not refs:
        return None
    rounds = [r["refine_rounds"] for r in refs]
    converged = sum(1 for r in refs if r.get("converged"))
    budget_hit = sum(1 for r in refs if r.get("refine_budget_hit"))
    iterated = sum(1 for r in refs if r.get("refine_rounds", 0) > 1)
    dist: Dict[str, int] = {}
    for k in rounds:
        dist[str(k)] = dist.get(str(k), 0) + 1
    return {
        "queries": len(refs),
        "converged": {"n": converged, "of": len(refs), "rate": _pct(converged, len(refs))},
        "budget_hit": {"n": budget_hit, "of": len(refs), "rate": _pct(budget_hit, len(refs))},
        "iterated_more_than_one_round": iterated,
        "rounds": {
            "mean": round(statistics.mean(rounds), 2) if rounds else None,
            "max": max(rounds) if rounds else None,
            "distribution": dict(sorted(dist.items(), key=lambda kv: int(kv[0]))),
        },
    }


def build_summary(records: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    n = len(records)
    by_status: Dict[str, int] = {}
    for r in records:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    completed = [r for r in records if r["status"] in (OK, RESULT_MISMATCH)]
    ok = by_status.get(OK, 0)
    count_ok = sum(1 for r in records if r.get("count_match"))
    mism_count_ok = sum(1 for r in records if r["status"] == RESULT_MISMATCH and r.get("count_match"))
    sql_exact = sum(1 for r in records if r.get("sql_exact_match"))
    sims = [r["sql_similarity"] for r in completed if r.get("sql_similarity") is not None]
    lats = [r["latency_s"] for r in records if r.get("latency_s") is not None]

    return {
        "meta": meta,
        "counts": {
            "evaluated": n,
            "completed_pipeline": len(completed),
            "by_status": by_status,
        },
        "refinement": _refinement_summary(records),
        "conformance": _conformance_summary(records),
        "execution_accuracy": {
            # overall = ok over ALL queries; completed = ok over queries that produced a result.
            "overall": {"correct": ok, "of": n, "rate": _pct(ok, n)},
            "completed": {"correct": ok, "of": len(completed), "rate": _pct(ok, len(completed))},
        },
        "count_accuracy": {
            # credits results whose aggregate (count) column is right even if labels differ.
            "correct": count_ok, "of": n, "rate": _pct(count_ok, n),
            "extra_credited_vs_exact": mism_count_ok,
        },
        "sql_match": {
            "exact": {"correct": sql_exact, "of": n, "rate": _pct(sql_exact, n)},
            "mean_similarity": round(statistics.mean(sims), 4) if sims else None,
            "median_similarity": round(statistics.median(sims), 4) if sims else None,
        },
        "latency_s": {
            "total_wall_clock": meta.get("wall_clock_s"),
            "mean": round(statistics.mean(lats), 2) if lats else None,
            "median": round(statistics.median(lats), 2) if lats else None,
            "p90": round(sorted(lats)[int(0.9 * (len(lats) - 1))], 2) if lats else None,
        },
    }


def _trunc(s: Any, n: int = 80) -> str:
    s = "" if s is None else str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_markdown(summary: Dict[str, Any], records: List[Dict[str, Any]]) -> str:
    m = summary["meta"]
    ea = summary["execution_accuracy"]
    sq = summary["sql_match"]
    lat = summary["latency_s"]
    c = summary["counts"]

    L: List[str] = []
    L.append("# clean_fastomop — Eval Report")
    L.append("")
    L.append(f"- **Generated:** {m.get('generated')}")
    L.append(f"- **Dataset:** `{m.get('dataset')}` ({m.get('dataset_size')} queries, {c['evaluated']} evaluated)")
    L.append(f"- **Mode:** {m.get('mode', 'production')}")
    L.append(f"- **Semantic model:** {m.get('semantic_model')}")
    L.append(f"- **Database model:** {m.get('database_model')}")
    L.append(f"- **omcp:** `{m.get('omcp_command')}`")
    L.append(f"- **Concurrency:** {m.get('concurrency')}")
    L.append(f"- **Wall clock:** {m.get('wall_clock_s')} s")
    L.append("")

    ref = summary.get("refinement")
    if ref:
        L.append("## FastSSV feedback convergence")
        L.append("")
        if m.get("gate_feedback") == "all":
            L.append(
                "Production agent (skills unchanged) with the gate feeding back WARNINGs as well as "
                f"errors, max {m.get('max_gate_rounds', '?')} warning-driven revisions before "
                "best-effort execution. A *round* is one validated analytic-SQL submission "
                "(introspection bypassed).")
        else:
            L.append(
                "Template-free agent (no `omop-best-practices`) driven by the FastSSV feedback loop "
                f"(policy=**{m.get('policy', '?')}**, max {m.get('max_refine', '?')} blocking rounds). "
                "A *round* is one validated analytic-SQL submission (introspection bypassed).")
        L.append("")
        L.append("| Metric | Value |")
        L.append("|---|---:|")
        L.append(f"| Converged (executed query was FastSSV-clean) | {ref['converged']['n']}/{ref['converged']['of']} = **{ref['converged']['rate']}** |")
        L.append(f"| Hit round budget (executed with violations remaining) | {ref['budget_hit']['n']}/{ref['budget_hit']['of']} = {ref['budget_hit']['rate']} |")
        L.append(f"| Iterated (>1 round) | {ref['iterated_more_than_one_round']} |")
        L.append(f"| Rounds per query | mean {ref['rounds']['mean']} · max {ref['rounds']['max']} |")
        dist = ", ".join(f"{k}→{v}" for k, v in ref["rounds"]["distribution"].items())
        L.append(f"| Round distribution (rounds→queries) | {dist} |")
        L.append("")

    L.append("## Headline — execution accuracy")
    L.append("")
    L.append("Do the agent's returned rows match the gold `execution_result`?")
    L.append("")
    ca = summary.get("count_accuracy", {})
    L.append("| Basis | Correct | Of | Rate |")
    L.append("|---|---:|---:|---:|")
    L.append(f"| **Exact rows** (all queries) | {ea['overall']['correct']} | {ea['overall']['of']} | **{ea['overall']['rate']}** |")
    L.append(f"| **Counts correct** (labels/columns may differ) | {ca.get('correct')} | {ca.get('of')} | **{ca.get('rate')}** |")
    L.append(f"| Exact-rows, of those that produced a result | {ea['completed']['correct']} | {ea['completed']['of']} | {ea['completed']['rate']} |")
    L.append("")
    if ca.get("extra_credited_vs_exact"):
        L.append(
            f"_{ca['extra_credited_vs_exact']} `result_mismatch` rows have the **correct counts** but "
            f"differ only in dimension labels/columns (e.g. concept_id vs name, 'No matching concept' vs "
            f"'Unknown', an extra id column) — credited under **Counts correct** but not **Exact rows**._"
        )
        L.append("")

    conf = summary.get("conformance")
    if conf:
        sc = m.get("scoring", {})
        ve, gf = m.get("validation_enabled"), m.get("gate_feedback")
        if ve is True:
            gate_txt = "warnings + errors fed back" if gf == "all" else "errors only"
        elif ve is False:
            gate_txt = "off (ungated control)"
        else:
            gate_txt = "not recorded by this run"
        L.append("## FastSSV conformance (post-hoc, on the executed SQL)")
        L.append("")
        L.append(
            f"All captured `agent_sql` re-validated offline under one fixed ruler "
            f"(strict=**{sc.get('strict')}**, policy=**{sc.get('policy')}**, dialect=`{sc.get('dialect')}`), "
            f"independent of whether the gate was active during the run "
            f"(gate: **{gate_txt}**)."
        )
        L.append("")
        L.append(
            "**Warnings are the comparable signal.** The gate blocks ERROR-severity SQL only, so a "
            "gated run is error-clean by construction; warnings are never blocked in either arm, so any "
            "difference there is genuine spillover from the gate's feedback."
        )
        L.append("")
        wpq = conf["warnings_per_query"]
        L.append("| Metric | Value |")
        L.append("|---|---:|")
        L.append(f"| **Warning-clean queries** | {conf['warning_clean']['n']}/{conf['warning_clean']['of']} = **{conf['warning_clean']['rate']}** |")
        L.append(f"| Warnings per query | mean **{wpq['mean']}** · median {wpq['median']} · max {wpq['max']} · total {wpq['total']} |")
        L.append(f"| Error-clean queries (mechanical when gated) | {conf['error_clean']['n']}/{conf['error_clean']['of']} = {conf['error_clean']['rate']} |")
        L.append(f"| Fully clean (no errors, no warnings) | {conf['fully_clean']['n']}/{conf['fully_clean']['of']} = {conf['fully_clean']['rate']} |")
        L.append(f"| Not scored (no analytic SQL produced) | {conf['unscored_no_sql']} |")
        L.append("")
        if conf["warning_rules"]:
            L.append("### Warning rules fired (queries affected)")
            L.append("")
            L.append("| Rule | Queries |")
            L.append("|---|---:|")
            for rule, cnt in list(conf["warning_rules"].items())[:15]:
                L.append(f"| `{rule}` | {cnt} |")
            L.append("")
        if conf["error_rules"]:
            L.append("### Error rules fired (queries affected)")
            L.append("")
            L.append("| Rule | Queries |")
            L.append("|---|---:|")
            for rule, cnt in list(conf["error_rules"].items())[:15]:
                L.append(f"| `{rule}` | {cnt} |")
            L.append("")

    L.append("## SQL match (agent SQL vs gold SQL)")
    L.append("")
    L.append(f"- **Exact match** (normalised whitespace/case): {sq['exact']['correct']}/{sq['exact']['of']} = {sq['exact']['rate']}")
    L.append(f"- **Mean token similarity:** {sq['mean_similarity']}  ·  **median:** {sq['median_similarity']}")
    L.append("")
    L.append("_SQL rarely matches verbatim — different valid SQL can yield identical rows, so execution accuracy above is the real signal._")
    L.append("")

    L.append("## Status breakdown")
    L.append("")
    L.append("| Status | Count |")
    L.append("|---|---:|")
    labels = {
        OK: "ok (rows match)",
        RESULT_MISMATCH: "result_mismatch (ran, wrong rows)",
        SQL_EXEC_ERROR: "sql_exec_error (Select_Query failed)",
        NO_SQL: "no_sql (agent never queried)",
        PIPELINE_ERROR: "pipeline_error (exception)",
    }
    for status, count in sorted(c["by_status"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {labels.get(status, status)} | {count} |")
    L.append("")

    total_wall = f"{lat['total_wall_clock']}s" if lat["total_wall_clock"] is not None else "n/a"
    L.append("## Latency")
    L.append("")
    L.append(f"- mean **{lat['mean']}s** · median **{lat['median']}s** · p90 **{lat['p90']}s** · total **{total_wall}**")
    L.append("")

    fails = [r for r in records if r["status"] != OK]
    if fails:
        L.append(f"## Sample failures (showing up to 20 of {len(fails)})")
        L.append("")
        L.append("| id | status | input | agent rows | gold rows | sql sim | note |")
        L.append("|---:|---|---|---:|---:|---:|---|")
        for r in fails[:20]:
            note = _trunc(r.get("error") or "", 50)
            L.append(
                f"| {r['id']} | {r['status']} | {_trunc(r.get('input'), 50)} | "
                f"{r.get('agent_row_count')} | {r.get('gold_row_count')} | {r.get('sql_similarity')} | {note} |"
            )
        L.append("")

    L.append("---")
    L.append("_Full per-query records in `records.jsonl`; machine-readable summary in `report.json`._")
    return "\n".join(L)


def write_reports(out_dir: Path, summary: Dict[str, Any], records: List[Dict[str, Any]]) -> None:
    (out_dir / "report.json").write_text(json.dumps(summary, indent=2, default=str))
    (out_dir / "report.md").write_text(render_markdown(summary, records))


# --------------------------------------------------------------------------- #
# A/B comparison of two runs (gated vs ungated)
# --------------------------------------------------------------------------- #

def _mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar (binomial sign) p-value for paired binary outcomes.

    `b` = queries where only the baseline succeeded, `c` = only the treatment. Under H0 each
    discordant pair is a fair coin, so the p-value is the two-sided binomial tail. Concordant
    pairs carry no information and are (correctly) ignored. Exact rather than chi-square
    because the discordant count is often small.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def load_run_records(path: Path) -> List[Dict[str, Any]]:
    """Load records.jsonl from a run directory or a direct path to the file."""
    p = Path(path)
    if p.is_dir():
        p = p / "records.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"no records.jsonl at {p}")
    return _read_all_records(p)


def _arm_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    scored = [r for r in records if r.get("fastssv_scored")]
    warns = [r.get("fastssv_n_warnings", 0) for r in scored]
    return {
        "n": len(records),
        "scored": len(scored),
        "no_sql": len(records) - len(scored),
        "exec_ok": sum(1 for r in records if r.get("status") == OK),
        "warning_clean": sum(1 for w in warns if w == 0),
        "error_clean": sum(1 for r in scored if not r.get("fastssv_n_errors")),
        "warnings_total": sum(warns),
        "warnings_mean": round(statistics.mean(warns), 3) if warns else None,
        "warning_rules": _rule_query_counts(scored, "fastssv_warning_rules"),
        "error_rules": _rule_query_counts(scored, "fastssv_error_rules"),
    }


def _paired_binary(pairs: List[Tuple[bool, bool]]) -> Dict[str, Any]:
    """McNemar table for paired binary outcomes given (baseline_ok, treatment_ok) pairs."""
    b = sum(1 for a, t in pairs if a and not t)   # baseline only
    c = sum(1 for a, t in pairs if t and not a)   # treatment only
    return {
        "baseline_only": b,
        "treatment_only": c,
        "both": sum(1 for a, t in pairs if a and t),
        "neither": sum(1 for a, t in pairs if not a and not t),
        "p_value": round(_mcnemar_exact(b, c), 5),
    }


def ab_compare(
    baseline: List[Dict[str, Any]],
    treatment: List[Dict[str, Any]],
    label_baseline: str,
    label_treatment: str,
    scoring: Dict[str, Any],
) -> Dict[str, Any]:
    """Paired comparison of two runs. Deltas are treatment - baseline.

    Pairing is by query `id`: only ids present in BOTH runs are compared, so a run that
    covered a different slice of the dataset cannot skew the contrast. Warning metrics are
    additionally restricted to pairs where BOTH arms produced analytic SQL — a query the gated
    arm never answered has no SQL to be clean or dirty, and counting it either way would
    confuse the gate's *cost* (no answer) with its *benefit* (better SQL). That cost is
    reported separately as `no_sql`.
    """
    base_by_id = {r.get("id"): r for r in baseline}
    treat_by_id = {r.get("id"): r for r in treatment}
    common = [i for i in base_by_id if i in treat_by_id]
    common.sort(key=lambda x: (x is None, str(x)))

    pairs = [(base_by_id[i], treat_by_id[i]) for i in common]
    both_scored = [(a, t) for a, t in pairs if a.get("fastssv_scored") and t.get("fastssv_scored")]

    wdiffs = [t.get("fastssv_n_warnings", 0) - a.get("fastssv_n_warnings", 0) for a, t in both_scored]
    improved = sum(1 for d in wdiffs if d < 0)
    worsened = sum(1 for d in wdiffs if d > 0)

    # Per-rule query counts over the same paired, both-scored subset.
    rule_delta: Dict[str, Dict[str, int]] = {}
    for rules_field in ("fastssv_warning_rules",):
        a_counts = _rule_query_counts([a for a, _ in both_scored], rules_field)
        t_counts = _rule_query_counts([t for _, t in both_scored], rules_field)
        for rule in set(a_counts) | set(t_counts):
            ac, tc = a_counts.get(rule, 0), t_counts.get(rule, 0)
            rule_delta[rule] = {"baseline": ac, "treatment": tc, "delta": tc - ac}
    ranked_rules = dict(sorted(rule_delta.items(), key=lambda kv: (kv[1]["delta"], -kv[1]["baseline"])))

    return {
        "labels": {"baseline": label_baseline, "treatment": label_treatment},
        "scoring": scoring,
        "pairing": {
            "baseline_records": len(baseline),
            "treatment_records": len(treatment),
            "paired": len(common),
            "paired_both_scored": len(both_scored),
            "dropped_unpaired": len(baseline) + len(treatment) - 2 * len(common),
        },
        "arms": {
            label_baseline: _arm_stats([a for a, _ in pairs]),
            label_treatment: _arm_stats([t for _, t in pairs]),
        },
        "warning_clean": {
            **_paired_binary([
                (a.get("fastssv_n_warnings", 0) == 0, t.get("fastssv_n_warnings", 0) == 0)
                for a, t in both_scored
            ]),
            "of": len(both_scored),
        },
        "warning_count": {
            "mean_delta": round(statistics.mean(wdiffs), 3) if wdiffs else None,
            "improved": improved,
            "worsened": worsened,
            "unchanged": len(wdiffs) - improved - worsened,
            "p_value": round(_mcnemar_exact(worsened, improved), 5),
        },
        "execution_accuracy": {
            **_paired_binary([(a.get("status") == OK, t.get("status") == OK) for a, t in pairs]),
            "of": len(pairs),
        },
        # Restricted to pairs where BOTH arms actually produced SQL. A run killed mid-flight by
        # a flaky endpoint or a provider content filter lands as `no_sql` in exactly one arm,
        # and the all-pairs figure above silently charges that infrastructure failure to the
        # treatment. This subset is the comparison to trust whenever the arms differ in how
        # many model calls they make (a revision loop is exposed to more failures per query).
        "execution_accuracy_both_scored": {
            **_paired_binary([(a.get("status") == OK, t.get("status") == OK) for a, t in both_scored]),
            "of": len(both_scored),
        },
        "no_sql_cost": {
            "baseline": sum(1 for a, _ in pairs if not a.get("fastssv_scored")),
            "treatment": sum(1 for _, t in pairs if not t.get("fastssv_scored")),
        },
        "warning_rules": ranked_rules,
    }


def render_ab_markdown(ab: Dict[str, Any]) -> str:
    bl, tl = ab["labels"]["baseline"], ab["labels"]["treatment"]
    arms, pr, sc = ab["arms"], ab["pairing"], ab["scoring"]
    wc, wcount, ea = ab["warning_clean"], ab["warning_count"], ab["execution_accuracy"]

    L: List[str] = []
    L.append(f"# FastSSV A/B — `{bl}` (baseline) vs `{tl}` (treatment)")
    L.append("")
    L.append(f"- **Paired queries:** {pr['paired']} (of {pr['baseline_records']} / {pr['treatment_records']}); "
             f"both produced SQL: {pr['paired_both_scored']}")
    L.append(f"- **Scoring ruler (identical for both arms):** strict={sc.get('strict')}, "
             f"policy={sc.get('policy')}, dialect=`{sc.get('dialect')}`")
    L.append("")
    L.append("## Headline — warning conformance")
    L.append("")
    L.append("Warnings are never blocked by the gate in either arm, so a difference here is genuine "
             "spillover from the gate's feedback rather than the gate mechanically filtering its own errors.")
    L.append("")
    L.append(f"| Metric | {bl} | {tl} | Δ |")
    L.append("|---|---:|---:|---:|")
    a, t = arms[bl], arms[tl]
    L.append(f"| Warning-clean queries | {a['warning_clean']}/{a['scored']} | {t['warning_clean']}/{t['scored']} | "
             f"{t['warning_clean'] - a['warning_clean']:+d} |")
    if a["warnings_mean"] is not None and t["warnings_mean"] is not None:
        L.append(f"| Warnings per query (mean) | {a['warnings_mean']} | {t['warnings_mean']} | "
                 f"{t['warnings_mean'] - a['warnings_mean']:+.3f} |")
    L.append(f"| Warnings total | {a['warnings_total']} | {t['warnings_total']} | "
             f"{t['warnings_total'] - a['warnings_total']:+d} |")
    L.append(f"| Error-clean queries | {a['error_clean']}/{a['scored']} | {t['error_clean']}/{t['scored']} | "
             f"{t['error_clean'] - a['error_clean']:+d} |")
    L.append(f"| Produced no SQL (gate cost) | {a['no_sql']} | {t['no_sql']} | {t['no_sql'] - a['no_sql']:+d} |")
    L.append(f"| Execution accuracy (rows match gold) | {a['exec_ok']}/{a['n']} | {t['exec_ok']}/{t['n']} | "
             f"{t['exec_ok'] - a['exec_ok']:+d} |")
    L.append("")
    L.append("## Paired significance (McNemar, exact two-sided)")
    L.append("")
    L.append("| Outcome | Baseline only | Treatment only | Both | Neither | p |")
    L.append("|---|---:|---:|---:|---:|---:|")
    eab = ab["execution_accuracy_both_scored"]
    L.append(f"| Warning-clean | {wc['baseline_only']} | {wc['treatment_only']} | {wc['both']} | {wc['neither']} | {wc['p_value']} |")
    L.append(f"| Execution correct (all pairs) | {ea['baseline_only']} | {ea['treatment_only']} | {ea['both']} | {ea['neither']} | {ea['p_value']} |")
    L.append(f"| Execution correct (both produced SQL, n={eab['of']}) | {eab['baseline_only']} | {eab['treatment_only']} | {eab['both']} | {eab['neither']} | {eab['p_value']} |")
    L.append("")
    L.append("_Prefer the both-produced-SQL row when the arms make different numbers of model calls: a "
             "query killed by a flaky endpoint or a provider content filter appears as `no_sql` in one "
             "arm only, and the all-pairs row charges that infrastructure failure to the treatment._")
    L.append("")
    L.append(f"Warning *count* per query (sign test): **{wcount['improved']} improved**, "
             f"{wcount['worsened']} worsened, {wcount['unchanged']} unchanged · "
             f"mean Δ **{wcount['mean_delta']}** · p = {wcount['p_value']}")
    L.append("")
    if ab["warning_rules"]:
        L.append("## Per-rule warning breakdown (queries affected, paired subset)")
        L.append("")
        L.append("Negative Δ = the treatment arm fired the rule on fewer queries.")
        L.append("")
        L.append(f"| Rule | {bl} | {tl} | Δ |")
        L.append("|---|---:|---:|---:|")
        for rule, d in ab["warning_rules"].items():
            L.append(f"| `{rule}` | {d['baseline']} | {d['treatment']} | {d['delta']:+d} |")
        L.append("")
    L.append("---")
    L.append("_Deltas are treatment − baseline. Both arms re-scored offline under one ruler; "
             "pairing is by query id._")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_ids(s: Optional[str]) -> Optional[List[int]]:
    if not s:
        return None
    return [int(x) for x in re.split(r"[,\s]+", s.strip()) if x]


def main() -> int:
    p = argparse.ArgumentParser(prog="clean-fastomop-eval", description="Evaluate the OMOP NL->SQL pipeline against a query dataset.")
    p.add_argument("dataset", nargs="?", default=str(DEFAULT_DATASET), help="path to the gold dataset JSON")
    p.add_argument("--ids", help="comma-separated query ids to run (overrides --limit/--sample)")
    p.add_argument("--limit", type=int, help="run only the first N (after id/sample selection)")
    p.add_argument("--sample", type=int, help="randomly sample N queries")
    p.add_argument("--seed", type=int, default=0, help="sample seed (default 0)")
    p.add_argument("--concurrency", type=int, default=4, help="concurrent queries (default 4; use 1 if the MCP session is flaky)")
    p.add_argument("--refine", action="store_true", help="experimental naive/refine mode: template-free agent driven by the FastSSV feedback loop")
    p.add_argument("--max-refine", type=int, default=5, help="max blocking FastSSV rounds before best-effort execution (refine mode; default 5)")
    p.add_argument("--policy", choices=["clinical", "epidemiological"], default="epidemiological",
                   help="refine mode FastSSV feedback policy: 'clinical' suppresses epi observation-window rules (default epidemiological)")
    p.add_argument("--no-validation", action="store_true",
                   help="run WITHOUT the FastSSV pre-execution gate (ungated control arm); overrides [validation].enabled")
    p.add_argument("--gate-feedback", choices=["error", "all"], default="error",
                   help="what the production gate feeds back: 'error' (safety gate, default) or "
                        "'all' (warnings too — the warning-feedback arm, agent otherwise unchanged)")
    p.add_argument("--max-gate-rounds", type=int, default=3,
                   help="max warning-driven revisions before executing best-effort (--gate-feedback all; default 3)")
    p.add_argument("--score-policy", choices=["clinical", "epidemiological"], default="epidemiological",
                   help="post-hoc conformance scoring policy (default epidemiological: score every rule)")
    p.add_argument("--score-lenient", action="store_true",
                   help="score conformance with FastSSV strict mode OFF (default: strict, matching the gate's ruler)")
    p.add_argument("--out", help="output dir (default eval_runs/<timestamp>)")
    p.add_argument("--resume", action="store_true", help="resume into --out, skipping ids already in records.jsonl")
    p.add_argument("--report-only", help="skip running; rebuild report.json/report.md from an existing records.jsonl")
    p.add_argument("--ab", nargs=2, metavar=("BASELINE", "TREATMENT"),
                   help="skip running; paired A/B of two runs (dir or records.jsonl each), re-scored under one ruler")
    p.add_argument("--ab-out", help="write the A/B report here (default eval_runs/ab_<baseline>_vs_<treatment>.md)")
    args = p.parse_args()

    scoring = {
        "strict": not args.score_lenient,
        "policy": args.score_policy,
        "dialect": get_validation_config()["dialect"],
    }

    if args.ab:
        base_path, treat_path = Path(args.ab[0]), Path(args.ab[1])
        baseline, treatment = load_run_records(base_path), load_run_records(treat_path)
        # force=True: re-score BOTH arms here, ignoring any fields already on the records, so
        # the comparison cannot inherit two different rulers from two different runs.
        score_conformance(baseline, force=True, **scoring)
        score_conformance(treatment, force=True, **scoring)
        ab = ab_compare(baseline, treatment, base_path.name, treat_path.name, scoring)
        md = render_ab_markdown(ab)
        out = Path(args.ab_out) if args.ab_out else (ROOT_DIR / "eval_runs" / f"ab_{base_path.name}_vs_{treat_path.name}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        out.with_suffix(".json").write_text(json.dumps(ab, indent=2, default=str))
        print(md)
        print(f"\n[eval] A/B report -> {out}", file=sys.stderr)
        return 0

    if args.report_only:
        jsonl = Path(args.report_only)
        records = _read_all_records(jsonl)
        out_dir = jsonl.parent
        n_scored = score_conformance(records, force=True, **scoring)
        summary = build_summary(records, {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                          "dataset": "(report-only)", "dataset_size": len(records),
                                          "note": "rebuilt from records.jsonl",
                                          "scoring": scoring,
                                          "validation_enabled": None})
        write_reports(out_dir, summary, records)
        print(f"[eval] report rebuilt ({n_scored} queries conformance-scored) -> {out_dir/'report.md'}")
        print(render_markdown(summary, records))
        return 0

    dataset_path = Path(args.dataset)
    data = load_dataset(dataset_path)
    items = select_items(data, ids=_parse_ids(args.ids), limit=args.limit, sample=args.sample, seed=args.seed)
    if not items:
        print("[eval] no queries selected", file=sys.stderr)
        return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (ROOT_DIR / "eval_runs" / ts)

    if args.refine and args.no_validation:
        print("[eval] --no-validation is meaningless with --refine (the refinement loop IS the "
              "validator); drop one of them", file=sys.stderr)
        return 1
    if args.refine and args.gate_feedback == "all":
        print("[eval] --gate-feedback all is meaningless with --refine (the refinement loop "
              "already feeds back every violation); drop one of them", file=sys.stderr)
        return 1
    if args.no_validation and args.gate_feedback == "all":
        print("[eval] --gate-feedback all requires the gate; drop --no-validation", file=sys.stderr)
        return 1

    setup_observability()
    sem_cfg, db_cfg = get_agent_config("semantic"), get_agent_config("database")
    gate_on = False if args.no_validation else get_validation_config()["enabled"]
    if args.gate_feedback == "all" and not gate_on:
        print("[eval] --gate-feedback all requires [validation].enabled = true in config", file=sys.stderr)
        return 1
    cfg = EvalConfig(
        out_dir=out_dir,
        concurrency=args.concurrency,
        resume=args.resume,
        refine=args.refine,
        max_refine=args.max_refine,
        policy=args.policy,
        validation_enabled=gate_on,
        gate_feedback=args.gate_feedback,
        max_gate_rounds=args.max_gate_rounds,
    )

    mode = f"refine (naive/FastSSV loop, policy={cfg.policy})" if cfg.refine else "production"
    if cfg.refine:
        gate_desc = ""
    elif not gate_on:
        gate_desc = ", gate=OFF (ungated control)"
    elif cfg.gate_feedback == "all":
        gate_desc = f", gate=warnings+errors (max {cfg.max_gate_rounds} rounds)"
    else:
        gate_desc = ", gate=errors only"
    print(f"[eval] {len(items)} queries -> {out_dir}  (mode={mode}{gate_desc}, concurrency={cfg.concurrency})", file=sys.stderr)
    t0 = time.perf_counter()
    records = asyncio.run(run_eval(items, cfg))
    wall = round(time.perf_counter() - t0, 1)

    # Score the captured SQL and persist it back into records.jsonl, so the run is
    # self-describing and a later --ab has the fields without needing the gold dataset.
    score_conformance(records, force=True, **scoring)
    with open(cfg.out_dir / "records.jsonl", "w") as f:
        for rec in records:
            f.write(json.dumps(rec, default=str) + "\n")

    db_model = f"{db_cfg['MODEL_TYPE']}/{db_cfg['MODEL_ID']}"
    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": str(dataset_path),
        "dataset_size": len(data),
        "mode": (
            "refine (naive, no omop-best-practices)" if cfg.refine
            else f"production (gate: {'off' if not gate_on else cfg.gate_feedback})"
        ),
        "max_refine": cfg.max_refine if cfg.refine else None,
        "policy": cfg.policy if cfg.refine else None,
        # Which arm this run is, and the ruler its SQL was scored with — without both, two runs
        # are indistinguishable after the fact and no honest A/B is possible.
        "validation_enabled": None if cfg.refine else gate_on,
        "gate_feedback": None if cfg.refine else cfg.gate_feedback,
        "max_gate_rounds": cfg.max_gate_rounds if (not cfg.refine and cfg.gate_feedback == "all") else None,
        "scoring": scoring,
        "semantic_model": f"{sem_cfg['MODEL_TYPE']}/{sem_cfg['MODEL_ID']}",
        "database_model": db_model + (" [naive/refine]" if cfg.refine else ""),
        "omcp_command": get_mcp_config("omcp").get("command"),
        "concurrency": cfg.concurrency,
        "wall_clock_s": wall,
    }
    summary = build_summary(records, meta)
    write_reports(out_dir, summary, records)

    ea = summary["execution_accuracy"]["overall"]
    print(f"\n[eval] done in {wall}s — execution accuracy {ea['rate']} ({ea['correct']}/{ea['of']})", file=sys.stderr)
    conf = summary.get("conformance")
    if conf:
        print(f"[eval] FastSSV warning-clean {conf['warning_clean']['rate']} "
              f"({conf['warning_clean']['n']}/{conf['warning_clean']['of']}), "
              f"mean {conf['warnings_per_query']['mean']} warnings/query", file=sys.stderr)
    print(f"[eval] report: {out_dir/'report.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
