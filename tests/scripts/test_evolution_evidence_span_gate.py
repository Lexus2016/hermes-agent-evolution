"""Tests for the evidence-span quality gate (issue #1945)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_evidence_span_gate import (  # noqa: E402
    evaluate_findings,
    format_summary,
    parse_findings,
    run_gate,
)

_FINDING = {
    "title": "T",
    "type": "FEATURE",
    "body": "",
    "source_urls": ["https://x.com"],
    "has_quote": True,
    "quote_text": "q",
    "quote_too_long": False,
}


def test_parse_extracts_headers_and_urls():
    md = (
        "### [FEATURE] One\n- **Source**: https://arxiv.org/abs/2604\nBody.\n\n"
        "### [IMPROVEMENT] Two\nNo source.\n"
    )
    findings = parse_findings(md)
    assert len(findings) == 2
    assert findings[0]["type"] == "FEATURE"
    assert len(findings[0]["source_urls"]) >= 1
    assert findings[1]["type"] == "IMPROVEMENT"


def test_parse_detects_quotes():
    assert (
        parse_findings('### [FEATURE] Q\n> "verbatim quote"\n')[0]["has_quote"] is True
    )
    assert parse_findings("### [FEATURE] Q\nplain text\n")[0]["has_quote"] is False
    assert parse_findings("# no headers\nplain text\n") == []


def test_evaluate_compliant_and_missing():
    good = [dict(_FINDING)]
    bad = [dict(_FINDING, has_quote=False, quote_text=None)]
    no_url = [dict(_FINDING, source_urls=[])]
    long_q = [dict(_FINDING, quote_text="x" * 250, quote_too_long=True)]
    assert evaluate_findings(good)["findings"][0]["verifiable"] is True
    assert evaluate_findings(bad)["findings"][0]["verifiable"] is False
    assert evaluate_findings(no_url)["findings"][0]["verifiable"] is False
    assert evaluate_findings(long_q)["too_long"] == 1
    assert evaluate_findings(long_q)["findings"][0]["verifiable"] is False


def test_evaluate_compliance_rate():
    result = evaluate_findings([dict(_FINDING), dict(_FINDING, source_urls=[])])
    assert result["total"] == 2
    assert result["compliance_rate"] == 0.5


def test_run_gate_writes_sidecar(tmp_path):
    d = tmp_path / "research"
    d.mkdir()
    (d / "2026-01-01.md").write_text(
        '# R\n\n### [FEATURE] Good\n> "Quote."\nSource: https://x.com\n\n'
        "### [FEATURE] Bad\nNo quote or URL.\n",
        encoding="utf-8",
    )
    result = run_gate(tmp_path)
    assert result["total"] == 2
    assert (tmp_path / "evidence_spans.json").exists()
    assert json.loads((tmp_path / "evidence_spans.json").read_text())["total"] == 2


def test_run_gate_skips_when_empty(tmp_path):
    assert "skipped" in run_gate(tmp_path)
    (tmp_path / "research").mkdir()
    assert "skipped" in run_gate(tmp_path)


def test_format_summary():
    s = format_summary({
        "total": 5,
        "compliance_rate": 0.8,
        "without_quote": 1,
        "without_url": 0,
        "too_long": 0,
    })
    assert "5 findings" in s and "80%" in s
    assert "skipped" in format_summary({"skipped": "none"})
