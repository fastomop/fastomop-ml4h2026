"""Regression: circe-be scripts contain apostrophes inside `--` comments; the splitter must
not treat them as string literals (that glued the tail of every script into one chunk and
made empty cohorts read as a phantom subject 0 — fixed 2026-09-08)."""
from clean_fastomop_cohort.resources.cdm import _split_statements


def test_apostrophe_in_comment_does_not_open_literal():
    script = (
        "CREATE TEMP TABLE a AS SELECT 1;\n"
        "-- don't let this apostrophe swallow what follows\n"
        "INSERT INTO a VALUES (2);\n"
        "SELECT * FROM a;"
    )
    stmts = _split_statements(script)
    assert len(stmts) == 3
    assert stmts[-1].strip().upper().startswith("SELECT")


def test_semicolon_inside_literal_is_kept():
    stmts = _split_statements("SELECT 'a;b' AS x; SELECT 2")
    assert stmts == ["SELECT 'a;b' AS x", "SELECT 2"]


def test_comment_then_statement_on_same_chunk():
    stmts = _split_statements("-- header; not a split\nSELECT 1;\n")
    assert len(stmts) == 1 and "SELECT 1" in stmts[0]
