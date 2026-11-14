"""CLI entrypoint: run the OMOP NL->SQL pipeline on a query.

    uv run clean-fastomop "How many patients take metformin?"
    uv run clean-fastomop --semantic-only "type 2 diabetes mellitus"

`--semantic-only` exercises just concept-omcp (the omcp subprocess is not started) —
useful for verifying the semantic side locally.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from clean_fastomop.agents import (
    build_database_agent,
    build_naive_database_agent,
    build_semantic_agent,
)
from clean_fastomop.config import get_mcp_config, get_validation_config, validate_skills
from clean_fastomop.integrations import connect_concept_omcp, connect_omcp
from clean_fastomop.observability import setup_observability
from clean_fastomop.refinement import (
    RefineTrace,
    make_refinement_gate,
    run_refinement_pipeline,
)
from clean_fastomop.skills import skills_dir
from clean_fastomop.workflow import run_pipeline, run_semantic_only


async def _run_semantic_only(query: str) -> int:
    concept_cfg = get_mcp_config("concept_omcp")
    async with connect_concept_omcp(concept_cfg) as concept_tools:
        semantic = build_semantic_agent(concept_tools)
        result = await run_semantic_only(query, semantic)
    print(result)
    return 0


async def _run_full(query: str) -> int:
    concept_cfg = get_mcp_config("concept_omcp")
    omcp_cfg = get_mcp_config("omcp")
    async with connect_concept_omcp(concept_cfg) as concept_tools, connect_omcp(omcp_cfg) as omcp_tools:
        semantic = build_semantic_agent(concept_tools)
        database = build_database_agent(omcp_tools)
        result = await run_pipeline(query, semantic, database)
    print("=== SemanticContext ===")
    print(result.semantic_context)
    if result.sql is not None:
        print("\n=== SQL (Select_Query) ===")
        print(result.sql)
    if result.result is not None:
        print("\n=== Result ===")
        print(result.result)
    print("\n=== Answer ===")
    print(result.answer)
    return 0


async def _run_refine(query: str) -> int:
    """Experimental research mode: template-free agent + FastSSV-feedback refinement loop."""
    concept_cfg = get_mcp_config("concept_omcp")
    omcp_cfg = get_mcp_config("omcp")
    policy = get_validation_config()["policy"]
    async with connect_concept_omcp(concept_cfg) as concept_tools, connect_omcp(omcp_cfg) as omcp_tools:
        semantic = build_semantic_agent(concept_tools)
        trace = RefineTrace()
        gate = make_refinement_gate(trace, policy=policy)
        naive_database = build_naive_database_agent(omcp_tools, tool_hooks=[gate])
        result = await run_refinement_pipeline(query, semantic, naive_database, trace)
    print("=== SemanticContext ===")
    print(result.semantic_context)
    print(f"\n=== Refinement trace (policy={policy}) ===")
    for r in result.trace.rounds:
        rules = ", ".join(r["rules"]) if r["rules"] else "(clean)"
        print(f"round {r['round']}: {r['n_violations']} violation(s) [{rules}]")
    if result.sql is not None:
        print("\n=== Final SQL (Select_Query) ===")
        print(result.sql)
    if result.result is not None:
        print("\n=== Result ===")
        print(result.result)
    print("\n=== Answer ===")
    print(result.answer)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="clean-fastomop", description="OMOP NL->SQL pipeline")
    parser.add_argument("query", help="natural-language clinical query")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--semantic-only",
        action="store_true",
        help="run only the semantic step (concept-omcp); skip omcp/SQL",
    )
    mode.add_argument(
        "--refine",
        action="store_true",
        help="experimental: template-free agent refined by an iterative FastSSV-feedback loop",
    )
    args = parser.parse_args()

    validate_skills(skills_dir())
    setup_observability()

    if args.semantic_only:
        runner = _run_semantic_only(args.query)
    elif args.refine:
        runner = _run_refine(args.query)
    else:
        runner = _run_full(args.query)
    return asyncio.run(runner)


if __name__ == "__main__":
    sys.exit(main())
