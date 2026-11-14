"""Unit tests for the skill loader/composer (no agno / no network)."""

import pytest

from clean_fastomop.skills import compose_instructions, load_skill


def test_load_skill_strips_frontmatter():
    body = load_skill("semantic-role")
    assert body, "skill body should be non-empty"
    assert not body.lstrip().startswith("---"), "frontmatter should be stripped"
    assert "Semantic Agent" in body


def test_compose_instructions_joins_in_order():
    composed = compose_instructions(["semantic-role", "query-classification"])
    assert "Semantic Agent" in composed
    assert "Query classification" in composed
    # semantic-role comes first
    assert composed.index("Semantic Agent") < composed.index("Query classification")
    assert "\n\n---\n\n" in composed


def test_missing_skill_raises():
    with pytest.raises(FileNotFoundError):
        load_skill("does-not-exist")


def test_empty_skill_list_raises():
    with pytest.raises(ValueError):
        compose_instructions([])
