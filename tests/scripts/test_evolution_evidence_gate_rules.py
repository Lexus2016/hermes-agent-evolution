"""Tests for the pinned evidence-gate thresholds (#3567)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from evolution_evidence_gate_rules import load_rules, seed_path

_ENV = "EVOLUTION_PINNED_EVIDENCE_RULES"


def _pinned(tmp_path, **span) -> Path:
    target = tmp_path / "pinned" / seed_path().name
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(seed_path().read_bytes())
    data["span_gate"].update(span)
    target.write_text(json.dumps(data), encoding="utf-8")
    return target


def test_missing_surface_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, str(tmp_path / "absent.json"))
    with pytest.raises(ValueError, match="missing at"):
        load_rules()


def test_shadowed_surface_is_refused(tmp_path, monkeypatch):
    target = _pinned(tmp_path)
    (target.parent / "evolution_evidence_gate_rules.override.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(_ENV, str(target))
    with pytest.raises(ValueError, match="shadowed"):
        load_rules()


def test_rewritten_in_tree_copy_cannot_override_the_pinned_surface(tmp_path, monkeypatch):
    """A self-edit that loosens the in-tree seed leaves the pinned judgement intact."""
    monkeypatch.setenv(_ENV, str(_pinned(tmp_path)))
    loosened = json.loads(seed_path().read_bytes())
    loosened["span_gate"]["max_quote_chars"] = 10_000
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / seed_path().name).write_text(json.dumps(loosened), encoding="utf-8")
    assert load_rules()["span_gate"]["max_quote_chars"] == 200


def test_gate_consumes_the_pinned_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, str(_pinned(tmp_path, max_quote_chars=12)))
    from evolution_evidence_span_gate import parse_findings

    findings = parse_findings("### [FEATURE] t\n> 1234567890123\nhttps://x.com\n")
    assert findings and findings[0]["quote_too_long"] is True
