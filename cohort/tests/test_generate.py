"""`generate`: one description in, one CIRCE-JSON out (no reference set, no database, no model)."""
import argparse
import json

from clean_fastomop_cohort import cli, experiment


class _Architect:
    def generate(self, description):
        return {"ok": True, "circe": {"ConceptSets": [], "described": description},
                "tool_calls": 3, "seconds": 1.0, "rounds": 1, "transcript": [{"step": "tool"}]}


class _Executor:
    def __init__(self, *a, **k):
        pass

    def execute(self, circe, cohort_id):
        return {"ok": True, "size": 42, "person_ids": {1}, "seconds": 0.1, "error": None}


def _patch(monkeypatch):
    monkeypatch.setattr(experiment, "CDM", lambda: object())
    monkeypatch.setattr(experiment, "CirceCompiler", lambda: object())
    monkeypatch.setattr(experiment, "CirceExecutor", _Executor)
    monkeypatch.setattr(experiment, "make_architect", lambda *a, **k: _Architect())


def test_generate_one_returns_circe_and_size(monkeypatch):
    _patch(monkeypatch)
    res = experiment.generate_one("adults with type 2 diabetes", execute=True)
    assert res["circe"]["described"] == "adults with type 2 diabetes"
    assert res["execution"] == {"ok": True, "size": 42, "seconds": 0.1, "error": None}
    assert "person_ids" not in res["execution"]


def test_cli_generate_writes_files(monkeypatch, tmp_path):
    _patch(monkeypatch)
    src = tmp_path / "d.txt"
    src.write_text("heart failure\n")
    args = argparse.Namespace(description=None, file=str(src), out=str(tmp_path / "c.json"),
                              trace=str(tmp_path / "t.json"), architect="full", execute=False)
    assert cli.generate(args) == 0
    assert json.loads((tmp_path / "c.json").read_text())["described"] == "heart failure"
    assert json.loads((tmp_path / "t.json").read_text()) == [{"step": "tool"}]


def test_cli_generate_requires_a_description(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    args = argparse.Namespace(description=None, file=None, out=None, trace=None,
                              architect="full", execute=False)
    assert cli.generate(args) == 2
