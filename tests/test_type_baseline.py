"""The full-source gate must preserve, and ratchet down, diagnostic counts."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check-types.py"
    spec = importlib.util.spec_from_file_location("source_type_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_tracks_duplicate_errors_but_ignores_line_moves(gate):
    item = {"file": str(gate.ROOT / "src/example.py"), "severity": "error", "rule": "fixture", "message": "wrong type"}
    before = gate.diagnostic_counts([{**item, "range": {"start": {"line": 1}}}], gate.ROOT)
    after = gate.diagnostic_counts([{**item, "range": {"start": {"line": 99}}}], gate.ROOT)
    assert before == after
    assert sum((gate.diagnostic_counts([item, item], gate.ROOT) - before).values()) == 1


@pytest.mark.parametrize("change, expected", [(0, 0), (1, 1), (-1, 1)])
def test_gate_rejects_new_diagnostics_and_requires_tightening(gate, tmp_path, monkeypatch, change, expected):
    item = {"file": str(gate.ROOT / "src/example.py"), "severity": "error", "rule": "fixture", "message": "wrong type"}
    output = {"version": "fixture", "summary": {"filesAnalyzed": 1, "errorCount": 1, "warningCount": 0}, "generalDiagnostics": [item]}
    monkeypatch.setattr(gate.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=json.dumps(output)))
    baseline = tmp_path / "baseline.json"
    assert gate.check(baseline_path=baseline, update=True) == 0
    output["generalDiagnostics"] = [item] * (1 + change)
    output["summary"]["errorCount"] = 1 + change
    assert gate.check(baseline_path=baseline) == expected


def test_checker_failure_cannot_overwrite_baseline(gate, tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text("keep original")
    monkeypatch.setattr(gate.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=2, stdout="", stderr="checker failed"))
    assert gate.check(baseline_path=baseline, update=True) == 2
    assert baseline.read_text() == "keep original"
