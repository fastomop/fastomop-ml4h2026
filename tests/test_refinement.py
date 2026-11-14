"""Tests for the FastSSV refinement gate (no agent / DB / network needed).

Exercises the stateful tool_hook that loops Select_Query through FastSSV feedback: a query
with violations is blocked + fed back (not executed) until it is clean or the round budget is
spent; introspection queries bypass; the trace records one entry per validated round.
"""

from __future__ import annotations

from clean_fastomop.refinement import RefineTrace, make_refinement_gate

# Strict mode reports ERROR-severity violations for this (string match on concept_name).
BAD = (
    "SELECT * FROM drug_exposure de JOIN concept c ON de.drug_concept_id=c.concept_id "
    "WHERE c.concept_name LIKE '%aspirin%';"
)
GOOD = "SELECT COUNT(DISTINCT person_id) FROM omop_gold.person;"
DISCOVERY = (
    "SELECT DISTINCT table_schema, table_name FROM information_schema.tables "
    "WHERE LOWER(table_name) IN ('person','concept_ancestor') ORDER BY table_name;"
)


async def _run(gate, function_name, query):
    """Invoke the gate hook with a mock underlying tool; return (result, executed?)."""
    calls = []

    async def mock_tool(**kwargs):
        calls.append(kwargs)
        return "EXECUTED"

    result = await gate(function_name, mock_tool, {"query": query} if query is not None else {})
    return result, bool(calls)


async def test_violations_block_and_feed_back():
    trace = RefineTrace()
    gate = make_refinement_gate(trace, max_refine=5)
    result, executed = await _run(gate, "Select_Query", BAD)
    assert not executed  # blocked: underlying tool NOT called
    assert "FastSSV reported" in result
    assert "refinement round 1/5" in result
    assert len(trace.rounds) == 1
    assert trace.rounds[0]["n_violations"] >= 1


async def test_clean_sql_executes_immediately():
    trace = RefineTrace()
    gate = make_refinement_gate(trace)
    result, executed = await _run(gate, "Select_Query", GOOD)
    assert executed
    assert result == "EXECUTED"
    # a clean query is still recorded (round 1, zero violations)
    assert len(trace.rounds) == 1
    assert trace.rounds[0]["n_violations"] == 0


async def test_introspection_bypasses_and_executes():
    trace = RefineTrace()
    gate = make_refinement_gate(trace)
    result, executed = await _run(gate, "Select_Query", DISCOVERY)
    assert executed
    assert result == "EXECUTED"
    assert trace.rounds == []  # introspection is not validated/recorded


async def test_budget_exhaustion_forces_execution():
    trace = RefineTrace()
    gate = make_refinement_gate(trace, max_refine=2)
    # First two BAD submissions are blocked (rounds 1, 2)...
    for expected_round in (1, 2):
        result, executed = await _run(gate, "Select_Query", BAD)
        assert not executed
        assert f"refinement round {expected_round}/2" in result
    # ...the third executes best-effort despite remaining violations (budget spent).
    result, executed = await _run(gate, "Select_Query", BAD)
    assert executed
    assert result == "EXECUTED"
    assert len(trace.rounds) == 3  # every validated submission is recorded


async def test_other_tools_pass_through():
    trace = RefineTrace()
    gate = make_refinement_gate(trace)
    result, executed = await _run(gate, "Get_Information_Schema", None)
    assert executed
    assert result == "EXECUTED"
    assert trace.rounds == []
