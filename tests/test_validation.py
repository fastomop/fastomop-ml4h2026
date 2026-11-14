"""Tests for the FastSSV pre-execution gate (no agent / DB / network needed)."""

from __future__ import annotations

from clean_fastomop import agents
from clean_fastomop.refinement import RefineTrace
from clean_fastomop.validation import (
    EPI_CONVENTION_RULES,
    fastssv_errors,
    fastssv_validations,
    is_introspection_query,
    make_select_query_gate,
)

# A temporal query (date filter on a clinical fact table, no observation_period) that fires
# exactly the epi convention rule `temporal.observation_period_anchoring` (WARNING).
TEMPORAL = "SELECT person_id FROM drug_exposure WHERE drug_exposure_start_date > DATE '2150-01-01';"

# A query that is analytically unsafe (string match on concept_name, no standardization);
# FastSSV in strict mode reports ERROR-severity violations for it.
BAD = (
    "SELECT * FROM drug_exposure de JOIN concept c ON de.drug_concept_id=c.concept_id "
    "WHERE c.concept_name LIKE '%aspirin%';"
)
GOOD = "SELECT COUNT(DISTINCT person_id) FROM omop_gold.person;"
DISCOVERY = (
    "SELECT DISTINCT table_schema, table_name FROM information_schema.tables "
    "WHERE LOWER(table_name) IN ('person','concept_ancestor') ORDER BY table_name;"
)


# --- gate logic --------------------------------------------------------------

def test_bad_sql_has_errors():
    assert len(fastssv_errors(BAD, strict=True)) >= 1


def test_good_sql_passes():
    assert fastssv_errors(GOOD, strict=True) == []


def test_empty_sql_passes():
    assert fastssv_errors("") == []
    assert fastssv_errors("   ") == []


def test_epidemiological_policy_keeps_obs_period_rule():
    rules = {v.rule_id for v in fastssv_validations(TEMPORAL, policy="epidemiological")}
    assert "temporal.observation_period_anchoring" in rules


def test_clinical_policy_suppresses_epi_convention_rules():
    epi = {v.rule_id for v in fastssv_validations(TEMPORAL, policy="epidemiological")}
    clinical = {v.rule_id for v in fastssv_validations(TEMPORAL, policy="clinical")}
    # the obs-window rule is present under epi, gone under clinical...
    assert "temporal.observation_period_anchoring" in epi
    assert "temporal.observation_period_anchoring" not in clinical
    # ...and clinical only ever drops rules from the epi-convention denylist.
    assert epi - clinical <= EPI_CONVENTION_RULES


def test_introspection_detection_and_bypass():
    assert is_introspection_query(DISCOVERY)
    assert not is_introspection_query(GOOD)
    # introspection queries are not subject to the gate even though they reference non-OMOP tables
    assert fastssv_errors(DISCOVERY, strict=True) == []


# --- hook contract (async; agno asyncio_mode = "auto") -----------------------

async def _run(function_name, query):
    """Invoke the gate hook with a mock underlying tool; return (result, executed?)."""
    calls = []

    async def mock_tool(**kwargs):
        calls.append(kwargs)
        return "EXECUTED"

    gate = make_select_query_gate()
    result = await gate(function_name, mock_tool, {"query": query} if query is not None else {})
    return result, bool(calls)


async def test_hook_blocks_bad_sql():
    result, executed = await _run("Select_Query", BAD)
    assert not executed                       # underlying tool was NOT called
    assert "FastSSV validation FAILED" in result


async def test_hook_executes_good_sql():
    result, executed = await _run("Select_Query", GOOD)
    assert executed
    assert result == "EXECUTED"


async def test_hook_bypasses_introspection():
    result, executed = await _run("Select_Query", DISCOVERY)
    assert executed                           # discovery query runs without validation
    assert result == "EXECUTED"


async def test_hook_passes_other_tools_through():
    result, executed = await _run("Get_Information_Schema", None)
    assert executed
    assert result == "EXECUTED"


# --- warning-feedback gate (--gate-feedback all) -----------------------------
#
# The experimental arm: the gate additionally blocks on WARNINGs so the agent is pushed
# toward FastSSV-clean SQL, while keeping every production skill. Two invariants matter —
# errors still block unconditionally (safety must not bend for an experiment), and the
# warning budget must terminate.

async def _run_gate(gate, function_name, query):
    """Invoke the gate hook with a mock underlying tool; return (result, executed?)."""
    calls = []

    async def mock_tool(**kwargs):
        calls.append(kwargs)
        return "EXECUTED"

    result = await gate(function_name, mock_tool, {"query": query} if query is not None else {})
    return result, bool(calls)


async def test_error_only_gate_ignores_warnings():
    gate = make_select_query_gate(strict=True, policy="epidemiological")
    result, executed = await _run_gate(gate, "Select_Query", TEMPORAL)
    assert executed  # a WARNING-only query is not the safety gate's business
    assert result == "EXECUTED"


async def test_warning_feedback_gate_blocks_warnings():
    gate = make_select_query_gate(feedback_severity="all", max_feedback_rounds=3,
                                  strict=True, policy="epidemiological")
    result, executed = await _run_gate(gate, "Select_Query", TEMPORAL)
    assert not executed
    assert "warning(s)" in result
    assert "temporal.observation_period_anchoring" in result


async def test_warning_feedback_gate_executes_clean_sql():
    gate = make_select_query_gate(feedback_severity="all", max_feedback_rounds=3)
    result, executed = await _run_gate(gate, "Select_Query", GOOD)
    assert executed and result == "EXECUTED"


async def test_warning_budget_terminates():
    gate = make_select_query_gate(feedback_severity="all", max_feedback_rounds=2,
                                  strict=True, policy="epidemiological")
    for _ in range(2):
        _, executed = await _run_gate(gate, "Select_Query", TEMPORAL)
        assert not executed
    _, executed = await _run_gate(gate, "Select_Query", TEMPORAL)
    assert executed  # budget spent -> best-effort execution rather than an endless loop


async def test_errors_block_regardless_of_spent_warning_budget():
    # The safety invariant: a spent warning budget must never let an ERROR through.
    gate = make_select_query_gate(feedback_severity="all", max_feedback_rounds=1, strict=True)
    await _run_gate(gate, "Select_Query", TEMPORAL)     # spends the budget
    await _run_gate(gate, "Select_Query", TEMPORAL)
    result, executed = await _run_gate(gate, "Select_Query", BAD)
    assert not executed
    assert "FastSSV validation FAILED" in result


async def test_warning_gate_bypasses_introspection():
    gate = make_select_query_gate(feedback_severity="all", max_feedback_rounds=3)
    result, executed = await _run_gate(gate, "Select_Query", DISCOVERY)
    assert executed and result == "EXECUTED"


async def test_gate_trace_records_each_validated_submission():
    trace = RefineTrace()
    gate = make_select_query_gate(feedback_severity="all", max_feedback_rounds=3,
                                  strict=True, policy="epidemiological", trace=trace)
    await _run_gate(gate, "Select_Query", TEMPORAL)
    await _run_gate(gate, "Select_Query", GOOD)
    await _run_gate(gate, "Select_Query", DISCOVERY)   # bypassed -> not recorded
    assert len(trace.rounds) == 2
    assert trace.rounds[0]["n_violations"] >= 1
    assert trace.rounds[1]["n_violations"] == 0


async def test_gate_trace_ignores_empty_sql():
    trace = RefineTrace()
    gate = make_select_query_gate(feedback_severity="all", max_feedback_rounds=3, trace=trace)
    await _run_gate(gate, "Select_Query", "   ")
    assert trace.rounds == []  # empty SQL is not a clean round


async def test_invalid_feedback_severity_rejected():
    import pytest

    with pytest.raises(ValueError):
        make_select_query_gate(feedback_severity="warnings")


# --- build_database_agent wiring ---------------------------------------------
#
# The seam between the eval flags and the gate: a silent failure here would make an
# experimental arm quietly run as the control arm, which no downstream metric could detect.

def _capture_gate_kwargs(monkeypatch):
    captured = {}

    def fake_gate(**kwargs):
        captured.update(kwargs)
        return "GATE"

    def fake_build(name, tools, tool_hooks=None, **kwargs):
        captured["hooks"] = tool_hooks
        return "AGENT"

    monkeypatch.setattr(agents, "make_select_query_gate", fake_gate)
    monkeypatch.setattr(agents, "_build", fake_build)
    return captured


def test_build_database_agent_defaults_to_error_only_gate(monkeypatch):
    captured = _capture_gate_kwargs(monkeypatch)
    agents.build_database_agent(None, validation_enabled=True)
    assert captured["feedback_severity"] == "error"
    assert captured["max_feedback_rounds"] is None
    assert captured["hooks"] == ["GATE"]


def test_build_database_agent_forwards_warning_feedback(monkeypatch):
    captured = _capture_gate_kwargs(monkeypatch)
    trace = RefineTrace()
    agents.build_database_agent(None, validation_enabled=True, feedback_severity="all",
                                max_feedback_rounds=4, trace=trace)
    assert captured["feedback_severity"] == "all"
    assert captured["max_feedback_rounds"] == 4
    assert captured["trace"] is trace


def test_build_database_agent_no_hooks_when_disabled(monkeypatch):
    captured = _capture_gate_kwargs(monkeypatch)
    agents.build_database_agent(None, validation_enabled=False, feedback_severity="all")
    assert captured["hooks"] is None
    assert "feedback_severity" not in captured  # gate never constructed
