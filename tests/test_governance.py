"""The pre-generation governance screen (clean_fastomop.governance).

Pure: no model, no database, no network.
"""
from __future__ import annotations

import json

import pytest

from clean_fastomop.governance import (
    ADVERSARIAL,
    ANSWERABLE,
    OUT_OF_SCOPE,
    screen,
)


def _ctx(**kw) -> str:
    base = {"user_query": "q", "request_class": ANSWERABLE, "query_category": "SINGLE_DRUG"}
    base.update(kw)
    return json.dumps(base)


def test_answerable_passes():
    d = screen(_ctx())
    assert d.allowed and d.request_class == ANSWERABLE and not d.defaulted


@pytest.mark.parametrize("cls", [OUT_OF_SCOPE, ADVERSARIAL])
def test_flagged_classes_are_blocked(cls):
    d = screen(_ctx(request_class=cls, request_class_reason="because"))
    assert not d.allowed
    assert d.request_class == cls
    assert d.reason == "because"
    assert d.message  # a caller-facing refusal is always supplied


def test_refusal_message_does_not_echo_the_reason():
    """The refusal must not coach an attacker toward a working rephrasing."""
    d = screen(_ctx(request_class=ADVERSARIAL,
                    request_class_reason="asks to DROP TABLE person"))
    assert "DROP" not in (d.message or "")


# --- fail-closed behaviour -------------------------------------------------

def test_missing_field_defaults_to_blocked():
    d = screen(json.dumps({"user_query": "q"}))
    assert not d.allowed and d.defaulted and d.request_class == OUT_OF_SCOPE


def test_unrecognised_value_defaults_to_blocked():
    d = screen(_ctx(request_class="probably_fine"))
    assert not d.allowed and d.defaulted


@pytest.mark.parametrize("bad", ["", "   ", "not json at all", "{unclosed", None, 42, []])
def test_unparseable_context_defaults_to_blocked(bad):
    d = screen(bad)
    assert not d.allowed and d.defaulted and d.request_class == OUT_OF_SCOPE


def test_case_and_whitespace_are_tolerated():
    assert screen(_ctx(request_class="  ANSWERABLE ")).allowed
    assert not screen(_ctx(request_class=" Adversarial")).allowed


def test_fenced_json_is_parsed():
    d = screen("```json\n" + _ctx() + "\n```")
    assert d.allowed


def test_non_string_class_is_blocked():
    d = screen(_ctx(request_class={"nested": "object"}))
    assert not d.allowed and d.defaulted
