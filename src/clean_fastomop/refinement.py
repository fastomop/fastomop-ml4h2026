"""Experimental FastSSV-feedback refinement workflow (a research demo, not production).

The production pipeline (`clean_fastomop.workflow.run_pipeline`) hands the database agent the
`omop-best-practices` skill — per-category SQL templates — plus a FastSSV *safety* gate that blocks
only ERROR-severity violations. This module instead strips those templates (see
`build_naive_database_agent`) and drives the template-free agent through an iterative loop: on
every `Select_Query`, ALL FastSSV violations (errors AND warnings) are fed back and execution is
blocked until the SQL converges (no violations) or a round budget is exhausted, at which point it
executes best-effort. The question it explores: can static-validator feedback alone steer a
naive agent to a meaningful query?

The loop is realized as a stateful agno `tool_hook` (`make_refinement_gate`). agno delivers a
hook's return value to the model as the tool result when the hook does NOT call `function_call`,
so returning the formatted violations blocks execution and prompts the agent to revise and
resubmit. Termination is guaranteed by `max_refine`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agno.agent import Agent

from clean_fastomop.validation import (
    fastssv_validations,
    format_violations,
    is_introspection_query,
)
from clean_fastomop.workflow import _extract_select_query


@dataclass
class RefineTrace:
    """Round-by-round record of the refinement loop (for display + tests)."""

    rounds: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, sql: str, violations: List[Any]) -> None:
        self.rounds.append(
            {
                "round": len(self.rounds) + 1,
                "n_violations": len(violations),
                "rules": sorted({getattr(v, "rule_id", "?") for v in violations}),
                "sql": sql,
            }
        )


@dataclass
class RefinementResult:
    query: str
    semantic_context: str
    sql: Optional[str] = None
    result: Optional[str] = None
    sql_error: bool = False
    answer: str = ""
    trace: RefineTrace = field(default_factory=RefineTrace)


def make_refinement_gate(
    trace: RefineTrace,
    max_refine: int = 5,
    dialect: str = "duckdb",
    strict: bool = True,
    policy: str = "epidemiological",
    tool_name: str = "Select_Query",
) -> Callable:
    """Build a stateful agno tool_hook that loops `tool_name` through FastSSV feedback.

    On each gated call (introspection queries bypass and execute): run `fastssv_validations`
    and append a `trace` entry. If any violation exists AND fewer than `max_refine` refinement
    rounds have been spent, return the formatted violations as the tool result — execution is
    blocked and the agent revises. Otherwise (clean, or the round budget is spent) forward to
    the underlying tool and return its rows. `max_refine` guarantees termination.

    `policy` ("clinical" | "epidemiological") is passed to `fastssv_validations`: clinical mode
    suppresses the epi observation-window convention rules so they are neither fed back nor
    recorded in the trace.
    """
    state = {"refines": 0}

    async def refinement_gate(
        function_name: str, function_call: Callable, arguments: Dict[str, Any]
    ) -> Any:
        if function_name == tool_name:
            sql = (arguments or {}).get("query", "") or ""
            if not is_introspection_query(sql):
                violations = fastssv_validations(sql, dialect=dialect, strict=strict, policy=policy)
                trace.record(sql, violations)
                if violations and state["refines"] < max_refine:
                    state["refines"] += 1
                    return (
                        f"FastSSV reported {len(violations)} issue(s); the query was NOT "
                        f"executed (refinement round {state['refines']}/{max_refine}). Revise "
                        f"the SQL to resolve these and resubmit Select_Query:\n"
                        f"{format_violations(violations)}"
                    )
        result = function_call(**(arguments or {}))
        if hasattr(result, "__await__"):
            result = await result
        return result

    return refinement_gate


async def run_refinement_pipeline(
    query: str, semantic: Agent, naive_database: Agent, trace: RefineTrace
) -> RefinementResult:
    """Semantic extraction -> naive SQL construction refined by FastSSV feedback.

    `naive_database` must carry a `make_refinement_gate(trace, ...)` tool_hook: the loop runs
    inside a single `arun` as the agent revises and resubmits Select_Query in response to the
    fed-back violations. The final (converged or budget-capped, executed) query and its rows
    are captured with `_extract_select_query`.
    """
    semantic_out = await semantic.arun(query)
    semantic_context = (semantic_out.content or "").strip()

    database_out = await naive_database.arun(semantic_context)
    answer = (database_out.content or "").strip()
    sql, result, sql_error = _extract_select_query(database_out)

    return RefinementResult(
        query=query,
        semantic_context=semantic_context,
        sql=sql,
        result=result,
        sql_error=sql_error,
        answer=answer,
        trace=trace,
    )
