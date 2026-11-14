"""The experiment: per gold cohort, the architect generates CIRCE from the description,
then BOTH the generated and gold CIRCE are executed exactly (circe-be) on the local CDM,
and patient sets are compared. Results stream to results.jsonl; summary to summary.json.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from .settings import settings
from .resources.cdm import CDM
from .resources.concepts import ConceptResolver
from .resources.goldset import GoldSet
from .execute.compiler import CirceCompiler
from .execute.executor import CirceExecutor
from .compare import aggregate, compare
from .strata import complexity_bucket


def _gold_summary(circe: Dict[str, Any]) -> Dict[str, Any]:
    """Structural fingerprint of a gold CIRCE — to compare against what the architect built."""
    cs = circe.get("ConceptSets") or []
    n_concepts = sum(len((c.get("expression", {}) or {}).get("items", []) or []) for c in cs)
    pc = (circe.get("PrimaryCriteria") or {}).get("CriteriaList") or []
    return {
        "n_concept_sets": len(cs),
        "n_concepts_total": n_concepts,
        "n_inclusion_rules": len(circe.get("InclusionRules") or []),
        "n_primary_criteria": len(pc),
        "primary_types": [list(c.keys())[0] for c in pc if isinstance(c, dict) and c],
    }


def _provenance(n: int, seed: int, gold_only: bool,
                strata: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    meta = {}
    try:
        p = Path(settings.concept_artifact_dir) / "meta.json"
        meta = {k: json.loads(p.read_text()).get(k)
                for k in ("turbovec_version", "build_ts", "n_terms")}
    except Exception:
        pass
    return {
        "n": n, "seed": seed, "gold_only": gold_only, "strata": strata,
        "model": settings.llm_model if not gold_only else None,
        "endpoint": settings.llm_endpoint if not gold_only else None,
        "critic_max_rounds": settings.critic_max_rounds if not gold_only else None,
        "ollama_num_ctx": settings.ollama_num_ctx or None,
        "llm_think": settings.llm_think,
        "cdm_schema": settings.cdm_schema, "vocab_schema": settings.vocab_schema,
        "duckdb": settings.duckdb_path, "concept_mcp": meta,
        "ts": int(time.time()),
    }


def run(n: int, seed: int = 7, out_dir: Optional[str] = None,
        gold_only: bool = False, architect_timeout_iters: int = 32,
        strata: Optional[Dict[str, int]] = None, workers: int = 1,
        resume: bool = False, architect_kind: str = "full") -> Dict[str, Any]:
    out = Path(out_dir or (Path(__file__).resolve().parents[2] / "results"))
    out.mkdir(parents=True, exist_ok=True)
    results_f = out / "results.jsonl"
    summary_f = out / "summary.json"

    cdm = CDM()
    executor = CirceExecutor(cdm, CirceCompiler())
    gs = GoldSet()
    sample = gs.stratified(strata, seed=seed) if strata else gs.sample(n, seed=seed)

    architect = None
    if not gold_only:
        architect = make_architect(architect_kind, cdm, executor, architect_timeout_iters)

    prov = _provenance(len(sample), seed, gold_only, strata)
    prov["workers"] = workers
    prov["architect"] = architect_kind if not gold_only else None
    print(f"== clean-fastomop-cohort: {len(sample)} cohorts | gold_only={gold_only} | "
          f"model={prov['model']} | architect={prov['architect']} | workers={workers} ==")
    records: List[Optional[Dict[str, Any]]] = [None] * len(sample)
    done_ids: set = set()
    if resume and results_f.exists():
        # keep every cohort already on disk (ok or failed); only the MISSING ones are run.
        # Same sample + seed => same ids/order, so slots line up by id.
        prev = {}
        for l in results_f.read_text().splitlines():
            if l.strip():
                r = json.loads(l); prev[r["id"]] = r
        for i, g in enumerate(sample):
            if g.id in prev:
                records[i] = prev[g.id]; done_ids.add(g.id)
        prov["resumed"] = {"kept": len(done_ids), "ts": int(time.time())}
        print(f"== resume: {len(done_ids)} cohorts already on disk, running the other "
              f"{len(sample) - len(done_ids)} ==")
        results_f.write_text("".join(json.dumps(r) + "\n" for r in records if r))
    else:
        results_f.write_text("")  # truncate
    lock = threading.Lock()

    def one(i: int, g) -> Dict[str, Any]:
        rec = _evaluate(i, g, architect, executor, len(sample))
        with lock:
            records[i] = rec
            with results_f.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        return rec

    todo = [(i, g) for i, g in enumerate(sample) if g.id not in done_ids]
    if workers <= 1:
        for i, g in todo:
            one(i, g)
    else:
        # Each worker thread gets its own DuckDB cursor (CDM._con is thread-local); the concept
        # store, compiler and OpenAI client are shared and thread-safe.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda ig: one(*ig), todo))

    summary = {"provenance": prov, "summary": aggregate([r for r in records if r])}
    summary_f.write_text(json.dumps(summary, indent=2))
    print("== summary ==")
    print(json.dumps(summary["summary"], indent=2))
    print(f"== wrote {results_f} and {summary_f} ==")
    return summary


def make_architect(kind: str, cdm, executor, max_iters: int = 32):
    """'full' = hierarchy-aware Builder+Critic (agents.py); 'builder' / 'vocab' / 'model' =
    the ablation ladder in generate/baselines.py."""
    if kind == "full":
        from .generate.architect import CleanArchitect
        return CleanArchitect(resolver=ConceptResolver(cdm=cdm), executor=executor, max_iters=max_iters)
    from .generate.baselines import BaselineArchitect
    return BaselineArchitect(kind, resolver=ConceptResolver(cdm=cdm), executor=executor,
                             max_iters=min(max_iters, 16))


def generate_one(description: str, architect_kind: str = "full", execute: bool = False,
                 max_iters: int = 32) -> Dict[str, Any]:
    """One description in, one CIRCE-JSON out. No reference set is needed.

    Returns {"ok", "circe", "error", "tool_calls", "seconds", "rounds", "transcript"} and, with
    execute=True, "execution": {"ok", "size", "seconds", "error"} for the generated cohort."""
    cdm = CDM()
    executor = CirceExecutor(cdm, CirceCompiler())
    architect = make_architect(architect_kind, cdm, executor, max_iters)
    gen = architect.generate(description)
    out: Dict[str, Any] = {k: gen.get(k) for k in
                           ("ok", "circe", "error", "tool_calls", "seconds", "rounds", "transcript")}
    if execute and out["circe"]:
        ex = executor.execute(out["circe"], cohort_id=1_000_001)
        out["execution"] = {k: ex[k] for k in ("ok", "size", "seconds", "error")}
    return out


def _evaluate(i: int, g, architect, executor, n_total: int, id_base: int = 1_000_000) -> Dict[str, Any]:
    """Generate (unless architect is None), execute gold + generated, compare. One cohort."""
    gold_id, gen_id = id_base + 2 * i, id_base + 2 * i + 1
    rec: Dict[str, Any] = {
        "id": g.id, "name": g.name, "description": g.description,
        "gold_complexity": complexity_bucket(g.circe),
        "gold_circe_summary": _gold_summary(g.circe),
    }
    gen_circe = None
    if architect is not None:
        print(f"[{i+1}/{n_total}] architect: {g.id} {g.name[:42]!r}", flush=True)
        gen = architect.generate(g.description)
        rec["generation"] = {k: gen.get(k)
                             for k in ("ok", "tool_calls", "seconds", "error", "guard", "rounds")}
        gen_circe = gen.get("circe")
        rec["generated_circe"] = gen_circe
        rec["generated_circe_summary"] = _gold_summary(gen_circe) if gen_circe else None
        rec["architect_transcript"] = gen.get("transcript")
    gold_ex = executor.execute(g.circe, cohort_id=gold_id)
    gen_ex = executor.execute(gen_circe, cohort_id=gen_id) if gen_circe else \
        {"ok": False, "size": None, "person_ids": set(), "seconds": 0.0, "error": "no generated cohort"}
    rec["execution"] = {
        "gold": {k: gold_ex[k] for k in ("ok", "size", "seconds", "error")},
        "generated": {k: gen_ex[k] for k in ("ok", "size", "seconds", "error")},
    }
    rec["comparison"] = compare(gold_ex["person_ids"], gen_ex["person_ids"]) \
        if gold_ex["ok"] and gen_ex["ok"] else None
    c = rec.get("comparison") or {}
    jac = c.get("jaccard")
    print(f"    [{i+1}/{n_total}] {g.id} done: gold={gold_ex.get('size')} gen={gen_ex.get('size')} "
          f"jaccard={jac if jac is not None else ('NA' if rec['comparison'] else '-')}"
          f"  ({(rec.get('generation') or {}).get('seconds', 0)}s, "
          f"{(rec.get('generation') or {}).get('tool_calls', 0)} turns)"
          + (f"  ERROR: {str((rec.get('generation') or {}).get('error'))[:140]}"
             if rec.get("generation") and not rec["generation"].get("ok") else ""), flush=True)
    return rec


def retry_failed(out_dir: str, workers: int = 1, architect_timeout_iters: int = 32) -> Dict[str, Any]:
    """Re-generate ONLY the cohorts whose generation failed (gateway timeouts, 5xx, ...) in a
    finished run, merge them back by id, and rewrite results.jsonl + summary.json. Records an
    `attempts` counter and the retry set in provenance so the protocol stays visible."""
    out = Path(out_dir)
    results_f, summary_f = out / "results.jsonl", out / "summary.json"
    rows = [json.loads(l) for l in results_f.read_text().splitlines() if l.strip()]
    summ = json.loads(summary_f.read_text()) if summary_f.exists() else {"provenance": {}}
    failed = [(i, r) for i, r in enumerate(rows) if not (r.get("generation") or {}).get("ok")]
    print(f"== retry-failed {out}: {len(failed)} of {len(rows)} cohorts failed generation ==")
    if not failed:
        return summ
    cdm = CDM()
    executor = CirceExecutor(cdm, CirceCompiler())
    by_id = {g.id: g for g in GoldSet().all()}
    kind = (summ.get("provenance") or {}).get("architect") or "full"
    architect = make_architect(kind, cdm, executor, architect_timeout_iters)
    print(f"   architect={kind} model={settings.llm_model}")
    lock = threading.Lock()

    def one(item):
        i, old = item
        rec = _evaluate(i, by_id[old["id"]], architect, executor, len(rows), id_base=4_000_000)
        rec["attempts"] = int(old.get("attempts") or 1) + 1
        rec["previous_error"] = (old.get("generation") or {}).get("error")
        with lock:
            rows[i] = rec
        return rec

    if workers <= 1:
        for it in failed:
            one(it)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, failed))
    results_f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    prov = summ.get("provenance") or {}
    prov.setdefault("retries", []).append({"ts": int(time.time()), "ids": [r["id"] for _, r in failed],
                                           "still_failed": [r["id"] for r in rows if not (r.get("generation") or {}).get("ok")]})
    summary = {"provenance": prov, "summary": aggregate(rows)}
    summary_f.write_text(json.dumps(summary, indent=2))
    print("== summary (after retry) ==")
    print(json.dumps(summary["summary"], indent=2))
    return summary


def replay(source: str, out_dir: str, workers: int = 1) -> Dict[str, Any]:
    """Re-execute a finished run's cohorts (gold + the STORED generated CIRCE) on the CDM
    currently configured in .env / environment — no architect, no LLM. Use it to test a
    run's definitions on a different OMOP database (e.g. DUCKDB_PATH=... CDM_SCHEMA=main)."""
    src = Path(source)
    src_rows = [json.loads(l) for l in src.read_text().splitlines() if l.strip()]
    src_summary = src.parent / "summary.json"
    src_prov = json.loads(src_summary.read_text()).get("provenance") if src_summary.exists() else None

    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    results_f, summary_f = out / "results.jsonl", out / "summary.json"
    cdm = CDM()
    executor = CirceExecutor(cdm, CirceCompiler())
    gs = GoldSet()
    by_id = {g.id: g for g in gs.all()}
    prov = {
        "replay_of": str(src), "source_provenance": src_prov, "n": len(src_rows),
        "cdm_schema": settings.cdm_schema, "vocab_schema": settings.vocab_schema,
        "duckdb": settings.duckdb_path, "workers": workers, "ts": int(time.time()),
    }
    print(f"== replay {src} ({len(src_rows)} cohorts) on {settings.duckdb_path} "
          f"[{settings.cdm_schema}/{settings.vocab_schema}] | workers={workers} ==")
    records: List[Optional[Dict[str, Any]]] = [None] * len(src_rows)
    results_f.write_text("")
    lock = threading.Lock()

    def one(i: int, r: Dict[str, Any]) -> Dict[str, Any]:
        gold_id, gen_id = 2_000_000 + 2 * i, 2_000_000 + 2 * i + 1
        g = by_id[r["id"]]
        gen_circe = r.get("generated_circe")
        rec: Dict[str, Any] = {k: r.get(k) for k in
                               ("id", "name", "description", "gold_complexity", "gold_circe_summary",
                                "generation", "generated_circe", "generated_circe_summary")}
        rec["source_execution"] = r.get("execution")
        rec["source_comparison"] = r.get("comparison")
        gold_ex = executor.execute(g.circe, cohort_id=gold_id)
        gen_ex = executor.execute(gen_circe, cohort_id=gen_id) if gen_circe else \
            {"ok": False, "size": None, "person_ids": set(), "seconds": 0.0, "error": "no generated cohort"}
        rec["execution"] = {
            "gold": {k: gold_ex[k] for k in ("ok", "size", "seconds", "error")},
            "generated": {k: gen_ex[k] for k in ("ok", "size", "seconds", "error")},
        }
        rec["comparison"] = compare(gold_ex["person_ids"], gen_ex["person_ids"]) \
            if gold_ex["ok"] and gen_ex["ok"] else None
        with lock:
            records[i] = rec
            with results_f.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        c = rec.get("comparison") or {}
        jac = c.get("jaccard")
        print(f"    [{i+1}/{len(src_rows)}] {r['id']} gold={gold_ex.get('size')} gen={gen_ex.get('size')} "
              f"jaccard={jac if jac is not None else ('NA' if rec['comparison'] else '-')}"
              f"  (MIMIC: gold={(r.get('execution') or {}).get('gold', {}).get('size')} "
              f"jaccard={(r.get('comparison') or {}).get('jaccard')})", flush=True)
        return rec

    if workers <= 1:
        for i, r in enumerate(src_rows):
            one(i, r)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda ir: one(*ir), enumerate(src_rows)))
    summary = {"provenance": prov, "summary": aggregate([r for r in records if r])}
    summary_f.write_text(json.dumps(summary, indent=2))
    print("== summary ==")
    print(json.dumps(summary["summary"], indent=2))
    print(f"== wrote {results_f} and {summary_f} ==")
    return summary
