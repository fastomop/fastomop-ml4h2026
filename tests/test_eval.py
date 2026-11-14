"""Unit tests for the eval comparison logic (no MCP / no LLM needed)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from clean_fastomop.eval import (
    ab_compare,
    compare_rows,
    count_match,
    parse_csv_rows,
    render_ab_markdown,
    score_conformance,
    sql_similarity,
    _conformance_summary,
    _mcnemar_exact,
    _norm_sql,
)
from clean_fastomop.integrations import _looks_like_omcp_error, unwrap_mcp_result_text
from clean_fastomop.workflow import _extract_select_query


# --- CSV parsing -------------------------------------------------------------

def test_parse_csv_drops_header_single_count():
    rows = parse_csv_rows("count\n318265\n")
    assert rows == [["318265"]]


def test_parse_csv_multi_column():
    csv = "gender,ethnicity,number_of_patients\nFEMALE,Unknown,172491\nMALE,Unknown,145774\n"
    assert parse_csv_rows(csv) == [
        ["FEMALE", "Unknown", "172491"],
        ["MALE", "Unknown", "145774"],
    ]


def test_parse_csv_empty():
    assert parse_csv_rows("") == []
    assert parse_csv_rows(None) == []


def test_parse_csv_unwraps_json_calltoolresult():
    blob = '{"content": [{"type": "text", "text": "count\\n5\\n"}], "isError": false}'
    assert parse_csv_rows(blob) == [["5"]]


# --- omcp double-wrap unwrapping ---------------------------------------------

def test_unwrap_success_wrapped():
    raw = '{"_meta": null, "content": [{"type": "text", "text": "count\\n5\\n"}], "isError": false}'
    text, err = unwrap_mcp_result_text(raw)
    assert text == "count\n5\n"
    assert err is False


def test_unwrap_error_wrapped_reads_inner_flag():
    raw = '{"content": [{"type": "text", "text": "Failed to execute query: boom"}], "isError": true}'
    text, err = unwrap_mcp_result_text(raw)
    assert text.startswith("Failed to execute query")
    assert err is True


def test_unwrap_plain_csv_passthrough():
    text, err = unwrap_mcp_result_text("a,b\n1,2\n")
    assert text == "a,b\n1,2\n"
    assert err is None


def test_looks_like_omcp_error():
    assert _looks_like_omcp_error("Failed to execute query: x")
    assert _looks_like_omcp_error("Query validation failed: y")
    assert not _looks_like_omcp_error("count\n5")


# --- row comparison ----------------------------------------------------------

def test_str_int_equivalence():
    # agent CSV gives strings; gold dataset gives ints — must compare equal.
    agent = [["318265"]]
    gold = [[318265]]
    assert compare_rows(agent, gold)["relaxed"]
    assert compare_rows(agent, gold)["strict"]


def test_row_order_insensitive():
    agent = [["MALE", "145774"], ["FEMALE", "172491"]]
    gold = [["FEMALE", 172491], ["MALE", 145774]]
    assert compare_rows(agent, gold)["strict"]


def test_column_reorder_relaxed_only():
    agent = [["172491", "FEMALE"]]   # columns swapped vs gold
    gold = [["FEMALE", 172491]]
    cmp = compare_rows(agent, gold)
    assert cmp["relaxed"]
    assert not cmp["strict"]


def test_null_equivalence():
    assert compare_rows([[None, "5"]], [["", 5]])["strict"]
    assert compare_rows([["None", "5"]], [[None, 5]])["strict"]


def test_mismatch_detected():
    assert not compare_rows([["5"]], [[6]])["relaxed"]
    assert not compare_rows([["5"]], [[5], [6]])["relaxed"]  # different row count


# --- count_match (credits correct counts despite differing labels) -----------

def test_count_match_concept_ids_vs_names():
    # agent returned gender as concept_id (8507) not 'MALE'; counts are right.
    agent = [["8507", "0", "172643"], ["8532", "0", "191984"]]
    gold = [["FEMALE", "Unknown", 191984], ["MALE", "Unknown", 172643]]
    assert count_match(agent, gold)
    assert not compare_rows(agent, gold)["relaxed"]  # exact still fails


def test_count_match_extra_column():
    agent = [["8532", "FEMALE", 191984], ["8507", "MALE", 172643]]  # extra concept_id col
    gold = [["MALE", 172643], ["FEMALE", 191984]]
    assert count_match(agent, gold)


def test_count_match_fails_on_different_grouping():
    agent = [["a", 1], ["b", 2], ["c", 3]]   # 3 groups
    gold = [["x", 1], ["y", 5]]               # 2 groups
    assert not count_match(agent, gold)


def test_count_match_single_value():
    assert count_match([["364627"]], [[364627]])
    assert not count_match([["364627"]], [[0]])


# --- SQL comparison ----------------------------------------------------------

def test_norm_sql_whitespace_and_semicolon():
    a = "SELECT  COUNT(*)\nFROM person ;"
    b = "select count(*) from person"
    assert _norm_sql(a) == _norm_sql(b)


def test_sql_similarity_bounds():
    assert sql_similarity("select 1", "select 1") == 1.0
    assert sql_similarity("select 1", None) == 0.0
    assert 0.0 < sql_similarity("select a from t", "select b from t") < 1.0


# --- tool-call extraction ----------------------------------------------------

@dataclass
class _FakeTE:
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    result: Optional[str] = None
    tool_call_error: Optional[bool] = None


@dataclass
class _FakeRun:
    tools: Any


def test_extract_picks_last_successful_select_query():
    run = _FakeRun(tools=[
        _FakeTE("Get_Information_Schema", {}, "schema...", False),
        _FakeTE("Select_Query", {"query": "SELECT 1"}, "bad", True),
        _FakeTE("Select_Query", {"query": "SELECT COUNT(*) FROM person"}, "count\n5\n", False),
    ])
    sql, result, errored = _extract_select_query(run)
    assert sql == "SELECT COUNT(*) FROM person"
    assert result == "count\n5\n"
    assert errored is False


def test_extract_returns_error_when_all_failed():
    run = _FakeRun(tools=[_FakeTE("Select_Query", {"query": "SELECT bad"}, "err", True)])
    sql, result, errored = _extract_select_query(run)
    assert sql == "SELECT bad"
    assert errored is True


def test_extract_none_when_no_select_query():
    run = _FakeRun(tools=[_FakeTE("Get_Information_Schema", {}, "schema", False)])
    assert _extract_select_query(run) == (None, None, False)


def test_extract_detects_wrapped_error_despite_false_flag():
    # The killer case: agno reports tool_call_error=False (outer wrapper), but the inner
    # result is an omcp error. Must be flagged as errored, not parsed as data rows.
    wrapped_err = '{"content": [{"type": "text", "text": "Failed to execute query: boom"}], "isError": true}'
    run = _FakeRun(tools=[_FakeTE("Select_Query", {"query": "SELECT x"}, wrapped_err, False)])
    sql, result, errored = _extract_select_query(run)
    assert sql == "SELECT x"
    assert errored is True


# --- FastSSV conformance scoring + A/B ---------------------------------------
#
# The A/B design rests on two invariants, both tested here:
#   1. records with no analytic SQL are NEVER counted as clean (they are the gate's cost);
#   2. both arms are scored under one ruler and paired by id, so an arm covering a different
#      slice of the dataset cannot skew the contrast.

_DIRTY_TEMPORAL = (
    "SELECT person_id FROM drug_exposure WHERE drug_exposure_start_date > DATE '2150-01-01';"
)
_CLEAN = "SELECT COUNT(DISTINCT person_id) FROM person;"


def _rec(qid, sql, status="ok"):
    return {"id": qid, "agent_sql": sql, "status": status}


def test_score_conformance_marks_missing_sql_unscored():
    records = [_rec("1", None, "no_sql"), _rec("2", "", "no_sql"), _rec("3", _CLEAN)]
    n = score_conformance(records, policy="epidemiological")
    assert n == 1
    assert records[0]["fastssv_scored"] is False
    assert records[1]["fastssv_scored"] is False
    assert records[2]["fastssv_scored"] is True


def test_score_conformance_splits_severities():
    records = [_rec("1", _DIRTY_TEMPORAL)]
    score_conformance(records, policy="epidemiological")
    r = records[0]
    assert r["fastssv_n_warnings"] >= 1
    assert "temporal.observation_period_anchoring" in r["fastssv_warning_rules"]
    # Warning rules and error rules are disjoint sets, never double-counted.
    assert not set(r["fastssv_warning_rules"]) & set(r["fastssv_error_rules"])


def test_score_conformance_policy_suppresses_epi_rules():
    records = [_rec("1", _DIRTY_TEMPORAL)]
    score_conformance(records, policy="clinical")
    assert "temporal.observation_period_anchoring" not in records[0]["fastssv_warning_rules"]


def test_score_conformance_force_rescopes_stale_fields():
    records = [_rec("1", _DIRTY_TEMPORAL)]
    score_conformance(records, policy="epidemiological")
    dirty = records[0]["fastssv_n_warnings"]
    score_conformance(records, policy="clinical", force=True)
    assert records[0]["fastssv_n_warnings"] < dirty


def test_score_conformance_without_force_keeps_existing():
    records = [_rec("1", _DIRTY_TEMPORAL)]
    score_conformance(records, policy="epidemiological")
    before = dict(records[0])
    score_conformance(records, policy="clinical")  # no force -> must not re-score
    assert records[0] == before


def test_conformance_summary_excludes_unscored_from_denominator():
    records = [_rec("1", _CLEAN), _rec("2", None, "no_sql")]
    score_conformance(records)
    s = _conformance_summary(records)
    assert s["scored"] == 1
    assert s["unscored_no_sql"] == 1
    assert s["warning_clean"]["of"] == 1


def test_conformance_summary_none_when_nothing_scored():
    records = [_rec("1", None, "no_sql")]
    score_conformance(records)
    assert _conformance_summary(records) is None


def test_mcnemar_symmetric_and_bounded():
    assert _mcnemar_exact(0, 0) == 1.0
    assert _mcnemar_exact(5, 5) == 1.0
    assert _mcnemar_exact(10, 0) == _mcnemar_exact(0, 10)
    assert _mcnemar_exact(10, 0) < 0.01       # all discordant pairs one way -> significant
    assert 0.0 <= _mcnemar_exact(3, 7) <= 1.0


def test_ab_pairs_on_id_and_ignores_unmatched():
    baseline = [_rec("1", _DIRTY_TEMPORAL), _rec("2", _CLEAN), _rec("99", _CLEAN)]
    treatment = [_rec("1", _CLEAN), _rec("2", _CLEAN)]
    for arm in (baseline, treatment):
        score_conformance(arm)
    ab = ab_compare(baseline, treatment, "base", "treat", {"strict": True, "policy": "epidemiological"})
    assert ab["pairing"]["paired"] == 2          # id 99 is baseline-only -> dropped
    assert ab["warning_clean"]["treatment_only"] == 1
    assert ab["warning_clean"]["baseline_only"] == 0
    assert ab["warning_count"]["improved"] == 1


def test_ab_excludes_no_sql_pairs_from_warning_metrics_but_counts_the_cost():
    # The gate's cost (a query it talked the agent out of answering at all) must not be
    # laundered into a warning-conformance win.
    baseline = [_rec("1", _DIRTY_TEMPORAL), _rec("2", _CLEAN)]
    treatment = [_rec("1", None, "no_sql"), _rec("2", _CLEAN)]
    for arm in (baseline, treatment):
        score_conformance(arm)
    ab = ab_compare(baseline, treatment, "base", "treat", {})
    assert ab["pairing"]["paired"] == 2
    assert ab["pairing"]["paired_both_scored"] == 1     # id 1 dropped from warning metrics
    assert ab["warning_clean"]["treatment_only"] == 0   # not credited as an improvement
    assert ab["no_sql_cost"]["treatment"] == 1
    assert ab["no_sql_cost"]["baseline"] == 0


def test_ab_render_markdown_smoke():
    baseline = [_rec("1", _DIRTY_TEMPORAL)]
    treatment = [_rec("1", _CLEAN)]
    for arm in (baseline, treatment):
        score_conformance(arm)
    md = render_ab_markdown(ab_compare(baseline, treatment, "base", "treat",
                                       {"strict": True, "policy": "epidemiological", "dialect": "duckdb"}))
    assert "FastSSV A/B" in md and "Warning-clean" in md


def test_ab_execution_accuracy_excludes_infrastructure_failures():
    # id 2 died in the treatment arm before producing SQL (flaky endpoint / content filter).
    # The all-pairs figure charges that to the treatment; the both-scored figure must not.
    baseline = [_rec("1", _CLEAN), _rec("2", _CLEAN)]
    treatment = [_rec("1", _CLEAN), _rec("2", None, "no_sql")]
    for arm in (baseline, treatment):
        score_conformance(arm)
    ab = ab_compare(baseline, treatment, "base", "treat", {})
    assert ab["execution_accuracy"]["baseline_only"] == 1        # all pairs: looks like a loss
    assert ab["execution_accuracy_both_scored"]["baseline_only"] == 0   # restricted: no loss
    assert ab["execution_accuracy_both_scored"]["of"] == 1
