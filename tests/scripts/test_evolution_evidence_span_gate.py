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

_F = {
    "title": "T",
    "type": "FEATURE",
    "body": "",
    "source_urls": ["https://x.com"],
    "has_quote": True,
    "quote_text": "q",
    "quote_too_long": False,
}


def test_parse_and_evaluate():
    f = parse_findings("### [FEATURE] A\nhttps://a.b\n\n### [BUG] B\nnope\n")
    assert len(f) == 2 and f[0]["source_urls"] and f[0]["type"] == "FEATURE"
    assert parse_findings('### [FEATURE] Q\n> "verbatim quote"\n')[0]["has_quote"]
    assert not parse_findings("# no\nplain\n")
    assert evaluate_findings([dict(_F)])["findings"][0]["verifiable"]
    assert not evaluate_findings([dict(_F, source_urls=[])])["findings"][0][
        "verifiable"
    ]
    r = evaluate_findings([dict(_F, quote_text="x" * 250, quote_too_long=True)])
    assert r["too_long"] == 1 and not r["findings"][0]["verifiable"]
    assert (
        evaluate_findings([dict(_F), dict(_F, source_urls=[])])["compliance_rate"]
        == 0.5
    )


def test_run_gate(tmp_path):
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "2026-01-01.md").write_text(
        '# R\n\n### [FEATURE] G\n> "Q."\nhttps://x.com\n\n### [FEATURE] B\nnope\n',
        encoding="utf-8",
    )
    assert run_gate(tmp_path)["total"] == 2
    assert json.loads((tmp_path / "evidence_spans.json").read_text())["total"] == 2
    assert "skipped" in run_gate(tmp_path / "empty")
    s = format_summary({"total": 5, "compliance_rate": 0.8, "without_quote": 1})
    assert "5 findings" in s and "80%" in s
    assert "skipped" in format_summary({"skipped": "none"})
