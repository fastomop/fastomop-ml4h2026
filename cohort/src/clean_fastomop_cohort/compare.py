"""Compare a generated cohort's patient set to the gold's. Pure set algebra.

A gold that executes to 0 patients on this CDM is an EMPTY REFERENCE (the phenotype
isn't represented in MIMIC) — overlap is undefined, so it is NA and excluded from every
accuracy figure, never counted as a 0-Jaccard miss.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Set


def jaccard(a: Set[int], b: Set[int]) -> Optional[float]:
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def compare(gold: Set[int], gen: Set[int]) -> Dict[str, Any]:
    """Per-cohort comparison. jaccard is None when the gold is empty (NA)."""
    gold_n, gen_n = len(gold), len(gen)
    if gold_n == 0:
        return {"gold_size": 0, "gen_size": gen_n, "jaccard": None,
                "exact_match": None, "na_reason": "gold empty on CDM"}
    inter = len(gold & gen)
    jac = inter / len(gold | gen)
    return {
        "gold_size": gold_n, "gen_size": gen_n, "intersection": inter,
        "only_in_gold": gold_n - inter, "only_in_gen": gen_n - inter,
        "jaccard": round(jac, 4),
        "exact_match": bool(gold == gen),
        "recall": round(inter / gold_n, 4),
        "precision": round(inter / gen_n, 4) if gen_n else 0.0,
    }


def classify(rec: Dict[str, Any]) -> str:
    """A coarse failure-mode label per cohort, for the post-run analysis."""
    gen_meta = rec.get("generation", {}) or {}
    ex = rec.get("execution", {}) or {}
    gold, gen = ex.get("gold", {}) or {}, ex.get("generated", {}) or {}
    comp = rec.get("comparison")
    if not gen_meta.get("ok"):
        return "GEN_FAILED"          # architect produced no complete cohort
    if not gold.get("ok"):
        return "GOLD_EXEC_ERROR"     # gold CIRCE failed to compile/run
    if not gen.get("ok"):
        return "GEN_EXEC_ERROR"      # generated CIRCE failed to compile/run
    if comp is None:
        return "NO_COMPARE"
    if comp.get("jaccard") is None:
        return "GOLD_EMPTY_NA"       # gold matched 0 patients on this CDM
    j = comp["jaccard"]
    if j >= 0.95:
        return "MATCH"
    if j >= 0.50:
        return "PARTIAL"
    gs, ps = comp.get("gold_size", 0), comp.get("gen_size", 0)
    if ps <= max(1, gs * 0.5):
        return "GEN_UNDER"           # generated far too small (over-restricted)
    if ps >= gs * 2:
        return "GEN_OVER"            # generated far too large (under-restricted)
    return "MISMATCH"                # similar sizes, different people (wrong concepts)


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-cohort records into research-grade summary metrics."""
    n = len(records)
    gold_ok = [r for r in records if (r.get("execution", {}).get("gold", {}) or {}).get("ok")]
    gen_ok = [r for r in records if (r.get("execution", {}).get("generated", {}) or {}).get("ok")]
    both = [r for r in records if r.get("comparison") is not None]
    evaluable = [r for r in both if r["comparison"].get("jaccard") is not None]  # gold>0
    jac = [r["comparison"]["jaccard"] for r in evaluable]

    summary: Dict[str, Any] = {
        "n_cohorts": n,
        "gold_executed": len(gold_ok),
        "generated_executed": len(gen_ok),
        "both_executed": len(both),
        "gold_empty_na": len(both) - len(evaluable),
        "evaluable": len(evaluable),  # gold>0 — the denominator for the rates below
    }
    if jac:
        summary["patient_jaccard_mean"] = round(statistics.mean(jac), 4)
        summary["patient_jaccard_median"] = round(statistics.median(jac), 4)
        if len(jac) > 1:
            summary["patient_jaccard_sd"] = round(statistics.pstdev(jac), 4)
        summary["patient_exact_match_accuracy"] = round(
            sum(1 for r in evaluable if r["comparison"].get("exact_match")) / len(evaluable), 4)
        summary["jaccard_ge_0.95"] = round(sum(1 for j in jac if j >= 0.95) / len(jac), 4)
        summary["jaccard_ge_0.50"] = round(sum(1 for j in jac if j >= 0.50) / len(jac), 4)

    modes: Dict[str, int] = {}
    for r in records:
        m = classify(r)
        modes[m] = modes.get(m, 0) + 1
    summary["failure_modes"] = dict(sorted(modes.items(), key=lambda kv: -kv[1]))
    return summary
