"""clean-fastomop-cohort CLI: build-jar | models | generate | run | retry-failed | replay | report | report-md."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .settings import settings


def _javac() -> str:
    jb = settings.java_bin
    return jb[:-4] + "javac" if jb.endswith("java") else "javac"


def build_jar() -> None:
    """Compile CompileCohort.java against the bundled circe-be/SqlRender jars."""
    tool = Path(settings.circe_tool_dir)
    classes = tool / "build" / "classes"
    classes.mkdir(parents=True, exist_ok=True)
    src = tool / "src" / "main" / "java" / "org" / "ohdsi" / "lab" / "CompileCohort.java"
    cmd = [_javac(), "-cp", str(tool / "lib" / "*"), "-d", str(classes), str(src)]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"compiled -> {classes}")


def report(out_dir: str | None, cohort_id: str | None = None) -> None:
    from .compare import classify
    out = Path(out_dir or (Path(__file__).resolve().parents[2] / "results"))
    res = out / "results.jsonl"
    if not res.exists():
        print("(no results.jsonl)")
        return
    rows = [json.loads(l) for l in res.read_text().splitlines() if l.strip()]

    # deep dive on one cohort (full transcript + both CIRCEs)
    if cohort_id:
        r = next((x for x in rows if str(x.get("id")) == str(cohort_id)), None)
        if not r:
            print(f"id {cohort_id} not in results"); return
        print(f"=== {r['id']} {r.get('name')} [{classify(r)}] ===")
        print("DESCRIPTION:", (r.get("description") or "")[:500])
        print("\nGOLD summary:", json.dumps(r.get("gold_circe_summary")))
        print("GEN  summary:", json.dumps(r.get("generated_circe_summary")))
        print("comparison:", json.dumps(r.get("comparison")))
        print("\nARCHITECT TRANSCRIPT:")
        for s in (r.get("architect_transcript") or []):
            if s.get("step") == "tool":
                print(f"  - {s['name']}({json.dumps(s['arguments'])[:160]}) -> {json.dumps(s['result'])[:200]}")
            else:
                print(f"  · assistant: {str(s.get('content'))[:200]}")
        return

    summ = out / "summary.json"
    if summ.exists():
        print("SUMMARY:", json.dumps(json.loads(summ.read_text()).get("summary", {}), indent=2))
    print(f"\nper-cohort (n={len(rows)}):")
    gens = [(r.get("generation") or {}) for r in rows]
    secs = [g.get("seconds") or 0 for g in gens if g]
    if secs:
        import statistics as st
        print(f"architect time: median {st.median(secs):.0f}s  mean {st.mean(secs):.0f}s  max {max(secs):.0f}s  "
              f"total {sum(secs)/60:.1f} min | turns median {st.median([g.get('tool_calls') or 0 for g in gens]):.0f}")
    print(f"  {'id':<10} {'mode':<14} {'gold':>7} {'gen':>7} {'jac':>5} {'rec':>5} {'prec':>5} {'secs':>6} {'turns':>5}  name")
    for r in rows:
        ex = r.get("execution", {})
        g, p = ex.get("gold", {}), ex.get("generated", {})
        c = r.get("comparison") or {}
        jac = c.get("jaccard")
        js = f"{jac:.2f}" if isinstance(jac, (int, float)) else ("NA" if r.get("comparison") else "-")
        rec = f"{c['recall']:.2f}" if isinstance(c.get("recall"), (int, float)) else ""
        prec = f"{c['precision']:.2f}" if isinstance(c.get("precision"), (int, float)) else ""
        gn = r.get("generation") or {}
        print(f"  {str(r.get('id'))[:10]:<10} {classify(r):<14} {str(g.get('size','')):>7} "
              f"{str(p.get('size','')):>7} {js:>5} {rec:>5} {prec:>5} {str(gn.get('seconds','')):>6} "
              f"{str(gn.get('tool_calls','')):>5}  {str(r.get('name',''))[:30]}")


def list_models() -> None:
    """Ping the configured LLM endpoint and list the models it exposes."""
    client = settings.make_client()
    print(f"endpoint: {settings.llm_endpoint}\nconfigured model: {settings.llm_model}\n")
    ids = client.list_models() if hasattr(client, "list_models") else sorted(m.id for m in client.models.list())
    for mid in ids:
        print(("* " if mid == settings.llm_model else "  ") + mid)
    if settings.llm_model not in ids:
        print(f"\nWARNING: configured model {settings.llm_model!r} not in the endpoint's list.")


def generate(args: argparse.Namespace) -> int:
    """`generate`: description -> CIRCE-JSON on stdout or --out. Progress goes to stderr."""
    if args.file:
        description = Path(args.file).read_text(encoding="utf-8")
    elif args.description:
        description = args.description
    elif not sys.stdin.isatty():
        description = sys.stdin.read()
    else:
        description = ""
    description = description.strip()
    if not description:
        print("no description given (pass it as an argument, with --file, or on stdin)", file=sys.stderr)
        return 2
    from . import experiment
    res = experiment.generate_one(description, architect_kind=args.architect, execute=args.execute)
    if args.trace:
        Path(args.trace).write_text(json.dumps(res.get("transcript"), indent=2), encoding="utf-8")
    print(f"tool calls: {res.get('tool_calls')}  revision rounds: {res.get('rounds')}  "
          f"seconds: {res.get('seconds')}", file=sys.stderr)
    if not res.get("circe"):
        print(f"no definition produced: {res.get('error')}", file=sys.stderr)
        return 1
    text = json.dumps(res["circe"], indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    ex = res.get("execution")
    if ex:
        print(f"executed: ok={ex['ok']} patients={ex['size']}"
              + (f" error={ex['error']}" if ex.get("error") else ""), file=sys.stderr)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="clean-fastomop-cohort")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-jar", help="compile the circe-be CLI")
    sub.add_parser("models", help="list models on the configured LLM endpoint")
    pg = sub.add_parser("generate", help="one cohort description in, one CIRCE-JSON definition out")
    pg.add_argument("description", nargs="?", help="the cohort description (or use --file, or pipe it on stdin)")
    pg.add_argument("--file", help="read the description from this text file")
    pg.add_argument("--out", help="write the CIRCE-JSON here (default: stdout)")
    pg.add_argument("--trace", help="write the tool-call trace (JSON) here")
    pg.add_argument("--architect", choices=["full", "builder", "vocab", "model"], default="full",
                    help="how much of the workflow to use (default: full)")
    pg.add_argument("--execute", action="store_true",
                    help="also compile and execute the definition and report the cohort size")
    pr = sub.add_parser("run", help="run against a reference set (GOLD_ROOT) and compare")
    pr.add_argument("--n", type=int, default=20)
    pr.add_argument("--seed", type=int, default=7)
    pr.add_argument("--gold-only", action="store_true",
                    help="skip the architect; just execute gold cohorts (pipeline check)")
    pr.add_argument("--strata", metavar="S,M,C",
                    help="stratified selection by gold complexity: counts for "
                         "SIMPLE,MODERATE,COMPLEX (e.g. 50,30,20). Overrides --n.")
    pr.add_argument("--out")
    pr.add_argument("--workers", type=int, default=1,
                    help="cohorts generated+executed concurrently (Ollama must allow parallel "
                         "requests; see preflight). 1 = sequential (default)")
    pr.add_argument("--architect", choices=["full", "builder", "vocab", "model"], default="full",
                    help="ablation ladder: model = one call, no tools, model writes CIRCE; vocab = + lookup_concept "
                         "tool; builder = + the 7 CIRCE builder tools (no hierarchy/Critic); full = Builder+Critic "
                         "with hierarchy tools (default)")
    pr.add_argument("--resume", action="store_true",
                    help="keep cohorts already in --out/results.jsonl and run only the missing ones")
    rr = sub.add_parser("replay", help="re-execute a finished run's gold + stored generated CIRCE "
                                        "on the currently configured CDM (no LLM)")
    rr.add_argument("--from", dest="source", required=True, help="results.jsonl of the run to replay")
    rr.add_argument("--out", required=True)
    rr.add_argument("--workers", type=int, default=1)
    rf = sub.add_parser("retry-failed", help="re-generate only the GEN_FAILED cohorts of a finished "
                                              "run (gateway timeouts etc.) and merge them back")
    rf.add_argument("--out", required=True)
    rf.add_argument("--workers", type=int, default=1)
    rm = sub.add_parser("report-md", help="write a structured REPORT.md into a run directory")
    rm.add_argument("--out", required=True, help="run directory (results.jsonl + summary.json)")
    rm.add_argument("--compare", nargs="*", default=[], help="reference run directories for paired comparison")
    rm.add_argument("--title")
    rp = sub.add_parser("report", help="print the per-cohort breakdown")
    rp.add_argument("--out")
    rp.add_argument("--id", help="deep-dive one cohort (full transcript + CIRCE summaries)")
    args = ap.parse_args()

    if args.cmd == "build-jar":
        build_jar()
    elif args.cmd == "models":
        list_models()
    elif args.cmd == "generate":
        sys.exit(generate(args))
    elif args.cmd == "run":
        from . import experiment
        strata = None
        if args.strata:
            s, m, c = (int(x) for x in args.strata.split(","))
            strata = {"SIMPLE": s, "MODERATE": m, "COMPLEX": c}
        experiment.run(args.n, args.seed, args.out, args.gold_only, strata=strata,
                       workers=args.workers, resume=args.resume, architect_kind=args.architect)
    elif args.cmd == "retry-failed":
        from . import experiment
        experiment.retry_failed(args.out, workers=args.workers)
    elif args.cmd == "replay":
        from . import experiment
        experiment.replay(args.source, args.out, workers=args.workers)
    elif args.cmd == "report-md":
        from .report_md import write_report
        print("wrote", write_report(args.out, args.compare, args.title))
    elif args.cmd == "report":
        report(args.out, args.id)


if __name__ == "__main__":
    main()
