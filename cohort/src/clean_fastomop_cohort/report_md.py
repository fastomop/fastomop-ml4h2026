"""Structured markdown report for a run directory: headline metrics, per-stratum table,
failure modes, retry provenance, paired comparison against reference runs, and the full
per-cohort table. Written to <run>/REPORT.md.

    clean-fastomop-cohort report-md --out results/my_run \\
        --compare results/rescored/strata100_v6 results/rescored/strata100_v9
"""
from __future__ import annotations

import json
import statistics as st
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .compare import classify


def _load(run: Path) -> Dict[str, Any]:
    rows = [json.loads(l) for l in (run / "results.jsonl").read_text().splitlines() if l.strip()]
    summ = json.loads((run / "summary.json").read_text()) if (run / "summary.json").exists() else {}
    return {"dir": run, "rows": rows, "summary": summ.get("summary", {}), "prov": summ.get("provenance", {})}


def _J(r: Dict[str, Any]) -> Optional[float]:
    return (r.get("comparison") or {}).get("jaccard")


def _paper_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ev = [r for r in rows if _J(r) is not None]
    js = [_J(r) for r in ev]
    gen = [r for r in rows if (r.get("generation") or {}).get("ok")]
    return {
        "n": len(rows), "generated": len(gen), "evaluable": len(ev),
        "median": st.median(js) if js else None, "mean": st.mean(js) if js else None,
        "sd": st.pstdev(js) if len(js) > 1 else None,
        "j1": sum(1 for j in js if j == 1.0), "j09": sum(1 for j in js if j >= 0.9),
        "j095": sum(1 for j in js if j >= 0.95), "lt05": sum(1 for j in js if j < 0.5),
        "j0": sum(1 for j in js if j == 0.0),
    }


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "–"
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def _stratum_table(runs: List[Dict[str, Any]]) -> List[str]:
    out = ["| stratum | " + " | ".join(f"{r['label']} (n, median J, J≥0.9)" for r in runs) + " |",
           "|---|" + "---|" * len(runs)]
    for s in ("SIMPLE", "MODERATE", "COMPLEX"):
        cells = []
        for r in runs:
            e = [x for x in r["rows"] if x.get("gold_complexity") == s and _J(x) is not None]
            js = [_J(x) for x in e]
            cells.append(f"{len(e)}, {_fmt(st.median(js)) if js else '–'}, {sum(1 for j in js if j >= 0.9)}")
        out.append(f"| {s} | " + " | ".join(cells) + " |")
    return out


def _paired(main: Dict[str, Any], ref: Dict[str, Any]) -> Dict[str, Any]:
    a = {r["id"]: _J(r) for r in main["rows"]}
    b = {r["id"]: _J(r) for r in ref["rows"]}
    ids = [i for i in a if i in b and a[i] is not None and b[i] is not None]
    d = [a[i] - b[i] for i in ids]
    return {"n": len(ids), "mean_delta": st.mean(d) if d else None,
            "wins": sum(1 for x in d if x > 0.05), "losses": sum(1 for x in d if x < -0.05),
            "ties": sum(1 for x in d if abs(x) <= 0.05)}


def write_report(out_dir: str, compare: Optional[List[str]] = None, title: Optional[str] = None) -> Path:
    main = _load(Path(out_dir))
    main["label"] = title or main["prov"].get("model") \
        or (main["prov"].get("source_provenance") or {}).get("model") or Path(out_dir).name
    refs = []
    for c in compare or []:
        r = _load(Path(c))
        src = r["prov"].get("source_provenance") or {}
        model = r["prov"].get("model") or src.get("model") or "?"
        r["label"] = f"{model} [{Path(c).name}]"
        refs.append(r)
    prov, summ, rows = main["prov"], main["summary"], main["rows"]
    m = _paper_metrics(rows)
    L: List[str] = []
    L.append(f"# Run report: {main['label']}")
    L.append("")
    L.append(f"Generated {time.strftime('%Y-%m-%d %H:%M')} from `{Path(out_dir)}`.")
    L.append("")
    L.append("## Setup")
    L.append("")
    keys = [("model", "model"), ("architect", "architect"), ("endpoint", "endpoint"), ("llm_think", "thinking"), ("ollama_num_ctx", "num_ctx"),
            ("critic_max_rounds", "critic rounds"), ("workers", "workers"), ("seed", "seed"), ("strata", "strata"),
            ("cdm_schema", "CDM schema"), ("vocab_schema", "vocab schema"), ("duckdb", "database")]
    L.append("| field | value |"); L.append("|---|---|")
    for k, lab in keys:
        if k in prov and prov[k] is not None:
            L.append(f"| {lab} | `{prov[k]}` |")
    if prov.get("concept_mcp"):
        L.append(f"| concept store | `{prov['concept_mcp']}` |")
    if prov.get("replay_of"):
        L.append(f"| replay of | `{prov['replay_of']}` |")
    L.append("")
    L.append("## Headline")
    L.append("")
    L.append("| metric | value |"); L.append("|---|---|")
    L.append(f"| cohorts | {m['n']} |")
    L.append(f"| generated (architect produced a cohort) | {m['generated']} |")
    L.append(f"| evaluable (gold non-empty, both executed) | {m['evaluable']} |")
    L.append(f"| gold empty on this CDM (NA) | {summ.get('gold_empty_na', '–')} |")
    L.append(f"| median patient Jaccard | {_fmt(m['median'])} |")
    L.append(f"| mean patient Jaccard (sd) | {_fmt(m['mean'])} ({_fmt(m['sd'])}) |")
    L.append(f"| exact set match (J = 1) | {m['j1']} |")
    L.append(f"| J ≥ 0.95 | {m['j095']} |")
    L.append(f"| J ≥ 0.9 | {m['j09']} |")
    L.append(f"| J < 0.5 | {m['lt05']} |")
    L.append(f"| J = 0 | {m['j0']} |")
    L.append("")
    fm = summ.get("failure_modes") or {}
    if fm:
        L.append("## Failure modes")
        L.append("")
        L.append("| mode | n | meaning |"); L.append("|---|---|---|")
        meaning = {"MATCH": "J ≥ 0.95", "PARTIAL": "0.5 ≤ J < 0.95", "GEN_UNDER": "generated ≤ half the gold size",
                   "GEN_OVER": "generated ≥ twice the gold size", "MISMATCH": "similar size, different patients",
                   "GEN_FAILED": "no generated cohort", "GOLD_EMPTY_NA": "gold empty on this CDM (excluded)",
                   "GEN_EXEC_ERROR": "generated CIRCE failed to execute", "GOLD_EXEC_ERROR": "gold failed to execute"}
        for k, v in sorted(fm.items(), key=lambda kv: -kv[1]):
            L.append(f"| {k} | {v} | {meaning.get(k, '')} |")
        L.append("")
    L.append("## Per stratum (gold complexity)")
    L.append("")
    L.extend(_stratum_table([main] + refs))
    L.append("")
    if refs:
        L.append("## Comparison with reference runs")
        L.append("")
        hdr = "| metric | " + " | ".join([main["label"]] + [r["label"] for r in refs]) + " |"
        L.append(hdr); L.append("|---|" + "---|" * (1 + len(refs)))
        ms = [m] + [_paper_metrics(r["rows"]) for r in refs]
        for k, lab in (("generated", "generated"), ("evaluable", "evaluable"), ("median", "median J"), ("mean", "mean J"),
                       ("j1", "J = 1"), ("j09", "J ≥ 0.9"), ("lt05", "J < 0.5"), ("j0", "J = 0")):
            L.append(f"| {lab} | " + " | ".join(_fmt(x[k]) for x in ms) + " |")
        L.append("")
        L.append("Paired per-cohort deltas (this run minus reference, cohorts evaluable in both; win/loss = |Δ| > 0.05):")
        L.append("")
        L.append("| reference | n paired | mean Δ J | wins | ties | losses |"); L.append("|---|---|---|---|---|---|")
        for r in refs:
            p = _paired(main, r)
            L.append(f"| {r['label']} | {p['n']} | {_fmt(p['mean_delta'], 3)} | {p['wins']} | {p['ties']} | {p['losses']} |")
        L.append("")
    retries = prov.get("retries") or []
    if retries or prov.get("resumed"):
        L.append("## Provenance: interruptions and retries")
        L.append("")
        if prov.get("resumed"):
            L.append(f"- resumed once, keeping {prov['resumed'].get('kept')} cohorts already on disk")
        for i, p in enumerate(retries, 1):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.get("ts", 0)))
            L.append(f"- retry pass {i} ({ts}): retried {len(p.get('ids', []))}, still failed after: {len(p.get('still_failed', []))}")
        L.append("")
    failed = [r for r in rows if not (r.get("generation") or {}).get("ok")]
    if failed:
        L.append("## Cohorts without a generated definition")
        L.append("")
        L.append("| id | name | attempts | last error |"); L.append("|---|---|---|---|")
        for r in failed:
            err = str((r.get("generation") or {}).get("error") or "")[:110].replace("|", "/")
            L.append(f"| {r['id']} | {str(r.get('name'))[:45]} | {r.get('attempts', 1)} | {err} |")
        L.append("")
    gens = [(r.get("generation") or {}) for r in rows if (r.get("generation") or {}).get("seconds")]
    if gens:
        secs = [g["seconds"] for g in gens]; turns = [g.get("tool_calls") or 0 for g in gens]
        L.append("## Cost")
        L.append("")
        L.append(f"- architect time per cohort: median {st.median(secs):.0f} s, mean {st.mean(secs):.0f} s, max {max(secs):.0f} s, total {sum(secs)/3600:.1f} h of model time")
        L.append(f"- tool calls per cohort: median {st.median(turns):.0f}, max {max(turns)}")
        L.append("")
    L.append("## Per-cohort results")
    L.append("")
    L.append("| id | stratum | mode | gold | gen | J | recall | prec | secs | turns | name |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (-(_J(r) if _J(r) is not None else -1))):
        ex = r.get("execution") or {}; g, p = ex.get("gold") or {}, ex.get("generated") or {}
        c = r.get("comparison") or {}; gn = r.get("generation") or {}
        L.append(f"| {r['id']} | {str(r.get('gold_complexity'))[:3]} | {classify(r)} | {g.get('size', '')} | "
                 f"{p.get('size', '')} | {_fmt(c.get('jaccard'))} | {_fmt(c.get('recall'))} | {_fmt(c.get('precision'))} | "
                 f"{gn.get('seconds', '')} | {gn.get('tool_calls', '')} | {str(r.get('name', ''))[:40].replace('|', '/')} |")
    L.append("")
    dest = Path(out_dir) / "REPORT.md"
    dest.write_text("\n".join(L))
    return dest
