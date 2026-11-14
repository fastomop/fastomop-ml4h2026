"""The linear NL->SQL pipeline: semantic extraction -> SQL generation/execution.

Deliberately hand-rolled rather than using agno's `Workflow`/`Step` (which pull in
fastapi/uvicorn) — a deterministic two-step chain reads more clearly as plain code.
Step 1's output (the SemanticContext JSON) is the input to step 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agno.agent import Agent

from clean_fastomop.config import get_governance_config
from clean_fastomop.governance import GovernanceDecision, screen
from clean_fastomop.integrations import result_is_error


@dataclass
class PipelineResult:
    query: str
    semantic_context: str  # JSON emitted by the semantic agent
    answer: str  # final database-agent response (e.g. "Found N patients.")
    sql: str | None = None  # the SQL the database agent passed to Select_Query
    result: str | None = None  # the raw CSV Select_Query returned
    sql_error: bool = False  # the (last) Select_Query call returned an error
    #: Pre-generation governance screen. `allowed=False` means the pipeline stopped
    #: before the database agent ran: no SQL was generated and nothing reached the DB.
    governance: GovernanceDecision | None = None


def _extract_select_query(run_output: Any) -> tuple[str | None, str | None, bool]:
    """Pull the SQL argument + raw result of the database agent's Select_Query call.

    agno records each tool invocation as a `ToolExecution` on `RunOutput.tools`
    (fields: tool_name, tool_args, result, tool_call_error). We return the *last*
    Select_Query call — preferring a successful one — so retries collapse to the
    query that actually produced the answer.

    Returns (sql, raw_result, errored). All-None if the agent never ran Select_Query.
    """
    tools = getattr(run_output, "tools", None) or []
    chosen = None
    chosen_errored = True
    for te in tools:
        name = (getattr(te, "tool_name", "") or "").lower()
        if "select_query" not in name:
            continue
        result = getattr(te, "result", None)
        # agno's tool_call_error reflects the always-false OUTER wrapper, so also inspect
        # the inner result for an omcp error — otherwise an error gets read as data rows.
        errored = bool(getattr(te, "tool_call_error", False)) or result_is_error(result)
        # Prefer the most recent *successful* call; if none succeed, keep the most recent.
        if chosen is None or not errored or chosen_errored:
            chosen, chosen_errored = te, errored
    if chosen is None:
        return None, None, False
    args = getattr(chosen, "tool_args", None) or {}
    sql = args.get("query") if isinstance(args, dict) else None
    result = getattr(chosen, "result", None)
    return sql, result, chosen_errored


async def run_pipeline(query: str, semantic: Agent, database: Agent) -> PipelineResult:
    """Run semantic agent, feed its JSON to the database agent, return both outputs.

    The database agent's Select_Query call is captured so callers (CLI, eval) can see
    the exact SQL executed and the raw rows it returned.
    """
    semantic_out = await semantic.arun(query)
    semantic_context = (semantic_out.content or "").strip()

    # Governance screen: deterministic read of the semantic agent's `request_class`.
    # A screened request stops here — the database agent is never invoked, so no SQL is
    # generated and the validator and database are never reached.
    decision = screen(semantic_context)
    if get_governance_config()["enabled"] and not decision.allowed:
        return PipelineResult(
            query=query,
            semantic_context=semantic_context,
            answer=decision.message or "",
            governance=decision,
        )

    database_out = await database.arun(semantic_context)
    answer = (database_out.content or "").strip()
    sql, result, sql_error = _extract_select_query(database_out)

    return PipelineResult(
        query=query,
        semantic_context=semantic_context,
        answer=answer,
        sql=sql,
        result=result,
        sql_error=sql_error,
        governance=decision,
    )


async def run_semantic_only(query: str, semantic: Agent) -> str:
    """Run just the semantic step (for verifying concept-omcp without omcp)."""
    out = await semantic.arun(query)
    return (out.content or "").strip()
