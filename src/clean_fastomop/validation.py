"""FastSSV pre-execution gate for the database agent.

A static, deterministic check (FastSSV) on the SQL the database agent submits to
`Select_Query`, run BEFORE the query reaches the database. It is wired as an agno
`tool_hook`: when the agent calls `Select_Query`, the hook validates the SQL with FastSSV;
if any ERROR-severity violation is reported the query is NOT executed and the violations are
returned to the agent so it can revise and resubmit. Every other tool (e.g.
`Get_Information_Schema`) passes through untouched.

FastSSV is run in **strict mode**, which escalates the analytically-dangerous best-practice
rules (standard-concept enforcement, concept-domain validation, vocabulary_id requirements,
relationship_id requirements) from WARNING to ERROR. Empirically this leaves correct,
hierarchy-expanded, standardized queries at zero errors while blocking unsafe ones (for
example, filtering `concept_name LIKE '%...%'` without standardization). Non-error warnings
do not block execution.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional

from fastssv import Severity, validate_sql
from fastssv.core.validation_context import with_strict_mode


# --- clinical-vs-epidemiological policy ------------------------------------- #
# A subset of FastSSV rules encode OHDSI/epidemiological *convention* rather than OMOP-CDM
# *validity*: they push queries to anchor events inside the patient's observation window
# (denominator validity, immortal-time avoidance). For descriptive / clinical count queries —
# "how many patients have a record of X" — that anchoring is a defensible thing to DECLINE,
# and on MIMIC (whose observation_period is coarsely/synthetically derived) anchoring injects
# artifact rather than rigor. In "clinical" policy these rules are suppressed; in
# "epidemiological" policy every rule is enforced. This only affects the refinement loop
# (which reacts to WARNINGs); the production safety gate blocks ERRORs only, and these rules
# are WARNING-severity, so the gate is unaffected either way.
EPI_CONVENTION_RULES = frozenset(
    {
        "temporal.observation_period_anchoring",
        "temporal.future_information_leakage",
    }
)


def is_introspection_query(sql: str) -> bool:
    """Is this a catalog/metadata query rather than OMOP analytic SQL?

    The database agent discovers schemas with `SELECT ... FROM information_schema.tables`.
    Such metadata queries are outside FastSSV's domain (it validates OMOP CDM analytics) and
    would otherwise be flagged for referencing non-OMOP tables, so they bypass the gate.
    """
    s = sql.lower()
    return (
        "information_schema" in s
        or "pg_catalog" in s
        or "sqlite_master" in s
        or s.lstrip().startswith("pragma")
    )


def fastssv_validations(
    sql: str,
    dialect: str = "duckdb",
    strict: bool = True,
    policy: str = "epidemiological",
) -> List[Any]:
    """Return ALL FastSSV violations for `sql` (errors AND warnings; empty list means clean).

    Introspection queries and empty SQL bypass validation. A parse error is reported by
    FastSSV as a single violation and is included. Used by the refinement loop, which feeds
    every violation back; the safety gate filters this to errors via `fastssv_errors`.

    `policy="clinical"` suppresses the epidemiological-convention rules (EPI_CONVENTION_RULES);
    `policy="epidemiological"` (default) enforces every rule.
    """
    if not sql or not sql.strip() or is_introspection_query(sql):
        return []
    with with_strict_mode(strict):
        result = validate_sql(sql, dialect=dialect)
    violations = list(result["violations"])
    if policy == "clinical":
        violations = [v for v in violations if v.rule_id not in EPI_CONVENTION_RULES]
    return violations


def fastssv_errors(
    sql: str,
    dialect: str = "duckdb",
    strict: bool = True,
    policy: str = "epidemiological",
) -> List[Any]:
    """Return the ERROR-severity FastSSV violations for `sql` (empty list means it passes).

    Introspection queries bypass validation. A parse error is reported by FastSSV as a single
    violation; it is included here so unparseable analytic SQL is also blocked.
    """
    return [
        v for v in fastssv_validations(sql, dialect=dialect, strict=strict, policy=policy)
        if v.severity == Severity.ERROR
    ]


def conformance(
    sql: str,
    dialect: str = "duckdb",
    strict: bool = True,
    policy: str = "epidemiological",
) -> Dict[str, Any]:
    """Severity-split FastSSV conformance for one SQL string (pure; no DB, no agent).

    Used to score *already-captured* SQL after a run, so any `records.jsonl` — including
    historical ones — can be re-scored offline under a single common ruler.

    The severity split is what makes a gated-vs-ungated comparison interpretable:

    * **errors** — in strict mode these are exactly what `make_select_query_gate` blocks, so a
      gated run's executed SQL is error-clean *by construction*. Comparing this across arms
      measures that the gate ran, not that the agent improved.
    * **warnings** — never blocked by the gate in either arm. Any cross-arm difference here is
      spillover: SQL the agent wrote better because of gate feedback it was not forced to obey.
      This is the free-standing signal.

    Returns `None` for `scored` when there is no analytic SQL to judge (missing SQL, or an
    introspection query, which `fastssv_validations` bypasses — counting those as "clean" would
    silently credit runs where the agent never produced a real query).
    """
    if not sql or not sql.strip() or is_introspection_query(sql):
        return {"scored": False, "n_errors": 0, "n_warnings": 0, "error_rules": [], "warning_rules": []}
    violations = fastssv_validations(sql, dialect=dialect, strict=strict, policy=policy)
    errors = [v for v in violations if v.severity == Severity.ERROR]
    warnings = [v for v in violations if v.severity == Severity.WARNING]
    return {
        "scored": True,
        "n_errors": len(errors),
        "n_warnings": len(warnings),
        "error_rules": sorted({getattr(v, "rule_id", "?") for v in errors}),
        "warning_rules": sorted({getattr(v, "rule_id", "?") for v in warnings}),
    }


def format_violations(violations: List[Any]) -> str:
    lines = []
    for v in violations:
        sev = getattr(getattr(v, "severity", None), "value", "") or ""
        prefix = f"[{sev.upper()}] " if sev else ""
        line = f"- {prefix}{v.rule_id}: {v.message}"
        fix = getattr(v, "suggested_fix", None)
        if fix:
            line += f"\n  suggested fix: {fix}"
        lines.append(line)
    return "\n".join(lines)


def make_select_query_gate(
    dialect: str = "duckdb",
    strict: bool = True,
    policy: str = "epidemiological",
    tool_name: str = "Select_Query",
    feedback_severity: str = "error",
    max_feedback_rounds: Optional[int] = None,
    trace: Optional[Any] = None,
) -> Callable:
    """Build an agno tool_hook that checks `tool_name` SQL with FastSSV before execution.

    The returned async hook uses agno's hook signature: it receives the tool `function_name`,
    the next callable `function_call`, and the call `arguments`. For the gated tool it
    validates `arguments['query']`; when it decides to block it short-circuits — the underlying
    tool is NOT called — and returns the violations as the tool result so the agent revises.
    All other tools are forwarded unchanged.

    `feedback_severity` selects what the agent is made to react to:

    * `"error"` (default, production) — only ERROR-severity violations block. Warnings are not
      surfaced at all. This is the safety gate: it stops unsafe SQL and says nothing about style.
    * `"all"` — warnings are fed back and block too, so the agent is pushed toward
      FastSSV-clean SQL rather than merely safe SQL. This is the experimental treatment arm;
      unlike `refinement.make_refinement_gate` it leaves the agent's skills untouched, which is
      what makes a warning-feedback comparison against the production agent unconfounded.

    ERROR-severity violations block **unconditionally and without a round budget** — that
    safety invariant does not bend for an experiment. `max_feedback_rounds` bounds only the
    warning-driven revisions: once spent, warning-only SQL is executed best-effort rather than
    trapping the agent in a loop over a rule it has declined (or cannot satisfy). `None` means
    unbounded, which is safe for `"error"` mode and generally a bad idea for `"all"`.

    `trace`, if given, is any object with `record(sql, violations)` (e.g.
    `refinement.RefineTrace`) and receives every validated analytic submission, so convergence
    can be reported per query. NOTE: a gate carrying a budget or a trace is **stateful** and
    must not be shared across concurrently-evaluated queries — build one per query.
    """
    if feedback_severity not in ("error", "all"):
        raise ValueError(f"feedback_severity must be 'error' or 'all', got {feedback_severity!r}")

    state = {"rounds": 0}

    async def fastssv_gate(function_name: str, function_call: Callable, arguments: Dict[str, Any]) -> Any:
        if function_name == tool_name:
            sql = (arguments or {}).get("query", "") or ""
            # Empty SQL is bypassed alongside introspection: neither is analytic SQL, and
            # recording an empty submission would score as a spurious "clean" round.
            if sql.strip() and not is_introspection_query(sql):
                violations = fastssv_validations(sql, dialect=dialect, strict=strict, policy=policy)
                if trace is not None:
                    trace.record(sql, violations)
                errors = [v for v in violations if v.severity == Severity.ERROR]
                if errors:
                    return (
                        f"FastSSV validation FAILED ({len(errors)} error(s)); the query was NOT "
                        f"executed. Revise the SQL to resolve these and resubmit Select_Query:\n"
                        f"{format_violations(errors)}"
                    )
                warnings = [v for v in violations if v.severity == Severity.WARNING]
                budget_left = max_feedback_rounds is None or state["rounds"] < max_feedback_rounds
                if feedback_severity == "all" and warnings and budget_left:
                    state["rounds"] += 1
                    budget = "unbounded" if max_feedback_rounds is None else str(max_feedback_rounds)
                    return (
                        f"FastSSV reported {len(warnings)} warning(s); the query was NOT executed "
                        f"(revision {state['rounds']}/{budget}). Revise the SQL to resolve these and "
                        f"resubmit Select_Query:\n{format_violations(warnings)}"
                    )
        result = function_call(**(arguments or {}))
        if inspect.isawaitable(result):
            result = await result
        return result

    return fastssv_gate
