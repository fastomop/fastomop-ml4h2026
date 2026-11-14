"""Agent builders. Each agent = a config-resolved model + skill-composed instructions
+ one MCP toolset. Stateless (no db) — the pipeline is deterministic per query.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from agno.agent import Agent
from agno.tools.mcp import MCPTools

from clean_fastomop.config import get_agent_config, get_validation_config
from clean_fastomop.models import create_model
from clean_fastomop.skills import compose_instructions
from clean_fastomop.validation import make_select_query_gate


def _build(
    agent_key: str,
    tools: MCPTools,
    tool_hooks: Optional[List[Callable]] = None,
    skills: Optional[List[str]] = None,
    name_suffix: str = "",
) -> Agent:
    cfg = get_agent_config(agent_key)
    return Agent(
        name=cfg.get("name", agent_key) + name_suffix,
        model=create_model(cfg),
        instructions=compose_instructions(skills if skills is not None else cfg["skills"]),
        tools=[tools],
        tool_hooks=tool_hooks,
        markdown=cfg.get("markdown", False),
        reasoning=cfg.get("reasoning", False),
        add_history_to_context=False,
        # Resilience to provider rate limits (HTTP 429): without exponential backoff a
        # 429 burst exhausts retries and the run returns empty -> a false `no_sql`.
        retries=cfg.get("retries", 6),
        delay_between_retries=cfg.get("delay_between_retries", 3),
        exponential_backoff=cfg.get("exponential_backoff", True),
    )


def build_semantic_agent(concept_tools: MCPTools) -> Agent:
    """Clinical term -> OMOP concept + query classification, via concept-omcp."""
    return _build("semantic", concept_tools)


def build_database_agent(
    omcp_tools: MCPTools,
    validation_enabled: Optional[bool] = None,
    feedback_severity: str = "error",
    max_feedback_rounds: Optional[int] = None,
    trace: Optional[Any] = None,
) -> Agent:
    """SemanticContext JSON -> OMOP SQL, executed via omcp.

    When [validation].enabled, the database agent's Select_Query is gated by FastSSV: the SQL
    is statically validated before execution and blocked (with the violations fed back to the
    agent) on any ERROR-severity issue. See clean_fastomop.validation.

    `validation_enabled` overrides that config flag for a single build (None = follow config).
    The eval harness uses it to run an ungated control arm without mutating config files, so
    both arms of a gated-vs-ungated comparison come from one command line.

    `feedback_severity="all"` (plus `max_feedback_rounds`, and optionally a `trace`) additionally
    feeds WARNING-severity violations back — the experimental arm that tests whether conformance
    feedback improves the SQL while holding the agent's skills fixed. A gate built with a budget
    or a trace is stateful: build one agent per query rather than sharing it.
    """
    v = get_validation_config()
    enabled = v["enabled"] if validation_enabled is None else validation_enabled
    hooks = None
    if enabled:
        hooks = [
            make_select_query_gate(
                dialect=v["dialect"],
                strict=v["strict"],
                feedback_severity=feedback_severity,
                max_feedback_rounds=max_feedback_rounds,
                trace=trace,
            )
        ]
    return _build("database", omcp_tools, tool_hooks=hooks)


# Database skills minus `omop-best-practices` (the per-category final-query templates): the naive
# agent keeps schema discovery + concept-hierarchy expansion but must construct query structure
# from first principles, so the FastSSV refinement loop is what steers it toward a valid query.
_NAIVE_DATABASE_SKILLS = ["database-role", "omop-hierarchy", "schema-knowledge"]


def build_naive_database_agent(
    omcp_tools: MCPTools, tool_hooks: Optional[List[Callable]] = None
) -> Agent:
    """Template-free database agent for the experimental FastSSV refinement workflow.

    Same model + omcp tools as `build_database_agent` but WITHOUT the `omop-best-practices` skill, so
    the agent has no per-category SQL templates to copy. `tool_hooks` should carry the
    refinement gate (see clean_fastomop.refinement), which feeds every FastSSV violation back
    and blocks execution until the SQL converges. Deliberately separate from the production
    pipeline — a demo, not part of the eval harness.
    """
    return _build(
        "database",
        omcp_tools,
        tool_hooks=tool_hooks,
        skills=_NAIVE_DATABASE_SKILLS,
        name_suffix=" (naive/refine)",
    )
