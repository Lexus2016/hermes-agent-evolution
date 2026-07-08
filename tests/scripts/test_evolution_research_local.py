"""Tests for the local-state research fallback (#733).

Verifies the fallback mines local telemetry and emits findings in the
live-research schema WITHOUT any network access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_research_local import (  # noqa: E402
    _default_evolution_dir,
    _load_records,
    _priority,
    _reject_rate,
    _trailing_zero_streak,
    main,
    mine_findings,
    render_report,
    run_local_research,
    web_tools_available,
)


def _write_metrics(evo_dir: Path, records: list[dict]) -> None:
    (evo_dir / "metrics.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


@pytest.fixture
def evo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "evolution"
    d.mkdir(parents=True)
    return d


# ── capability gate ──────────────────────────────────────────────────────────


def test_web_tools_available_true_when_any_present():
    assert web_tools_available(["read_file", "web_search"]) is True
    assert web_tools_available({"browser"}) is True


def test_web_tools_available_false_when_none_present():
    assert web_tools_available(["read_file", "patch", "terminal"]) is False
    assert web_tools_available([]) is False
    assert web_tools_available(None) is False


# ── priority formula ─────────────────────────────────────────────────────────


def test_priority_matches_canonical_formula():
    # impact 0.8, effort 0.4 -> 0.8 * 2 * (1 - 0.16) = 1.344 -> 1.34
    assert _priority(0.8, 0.4) == 1.34
    # impact 0.7, effort 0.3 -> 1.4 * 0.88 = 1.232 -> 1.23
    assert _priority(0.7, 0.3) == 1.23
    # effort dampens, never divides: zero effort keeps full doubled impact
    assert _priority(0.5, 0.0) == 1.0


# ── telemetry helpers ────────────────────────────────────────────────────────


def test_load_records_skips_blank_and_malformed(evo_dir: Path):
    (evo_dir / "metrics.jsonl").write_text(
        '{"date":"d1","merged":1}\n\nnot-json\n{"date":"d2","merged":0}\n',
        encoding="utf-8",
    )
    records = _load_records(evo_dir / "metrics.jsonl")
    assert [r["date"] for r in records] == ["d1", "d2"]


def test_load_records_missing_file_returns_empty(evo_dir: Path):
    assert _load_records(evo_dir / "nope.jsonl") == []


def test_trailing_zero_streak_counts_from_end():
    recs = [{"merged": 2}, {"merged": 0}, {"merged": 0}, {"merged": 0}]
    assert _trailing_zero_streak(recs, "merged") == 3
    assert _trailing_zero_streak([{"merged": 1}], "merged") == 0
    # absent field counts as zero
    assert _trailing_zero_streak([{}, {}], "merged") == 2


def test_reject_rate():
    recs = [{"selected": 1, "rejected": 3}, {"selected": 1, "rejected": 1}]
    assert _reject_rate(recs) == pytest.approx(4 / 6)
    assert _reject_rate([]) == 0.0


# ── mining: at least one actionable proposal without network ──────────────────


def test_merged_zero_streak_produces_actionable_finding(evo_dir: Path):
    records = [{"date": "d0", "issues_created": 2, "selected": 2, "merged": 1}]
    records += [
        {"date": f"d{i}", "issues_created": 1, "selected": 1, "merged": 0}
        for i in range(1, 5)
    ]
    _write_metrics(evo_dir, records)

    result = run_local_research(evo_dir)

    assert result["local_research"] is True
    assert result["cycles_analyzed"] == len(records)
    assert len(result["findings"]) >= 1
    top = result["findings"][0]
    assert top["category"] == "IMPROVEMENT"
    assert top["priority_score"] >= 0.7
    assert "Integration is stuck" in top["title"]
    assert "4 cycles" in top["source"]


def test_stagnation_and_reject_signals(evo_dir: Path):
    # 7 cycles, all zero issues_created (stagnation) and high reject rate
    records = [
        {
            "date": f"d{i}",
            "issues_created": 0,
            "selected": 1,
            "rejected": 4,
            "merged": 1,
        }
        for i in range(7)
    ]
    findings = mine_findings(records)
    titles = " ".join(f["title"] for f in findings)
    assert "restore frontier access" in titles
    assert "raise the research evidence bar" in titles


def test_high_reject_rate_funnel_flag_when_counts_thin(evo_dir: Path):
    findings = mine_findings([], funnel_summary="[evolution-funnel] HIGH_REJECT_RATE")
    assert any("evidence bar" in f["title"] for f in findings)


def test_stale_research_report_finding():
    findings = mine_findings([], latest_research_age_days=30)
    assert any("Frontier scan is stale" in f["title"] for f in findings)
    # fresh report -> no stale finding
    assert not any(
        "Frontier scan is stale" in f["title"]
        for f in mine_findings([], latest_research_age_days=1)
    )


# ── never a silent empty report ──────────────────────────────────────────────


def test_healthy_pipeline_yields_explicit_note_not_silence(evo_dir: Path):
    records = [
        {
            "date": f"d{i}",
            "issues_created": 2,
            "selected": 2,
            "rejected": 0,
            "merged": 2,
        }
        for i in range(5)
    ]
    _write_metrics(evo_dir, records)

    result = run_local_research(evo_dir)
    assert result["findings"] == []
    assert "note" in result and result["note"]

    report = render_report(result)
    assert "Local-state fallback" in report
    assert result["note"] in report


def test_empty_install_no_crash(evo_dir: Path):
    result = run_local_research(evo_dir)
    assert result["findings"] == []
    assert "note" in result


# ── output schema ────────────────────────────────────────────────────────────


def test_render_report_follows_live_schema(evo_dir: Path):
    records = [{"date": "d0", "merged": 1}] + [
        {"date": f"d{i}", "merged": 0} for i in range(1, 4)
    ]
    _write_metrics(evo_dir, records)
    report = render_report(run_local_research(evo_dir))

    assert report.startswith("<!-- evolution-research: local-state fallback -->")
    assert "# Research Report -" in report
    assert "## Improvements" in report
    assert "- **Priority Score**:" in report
    assert "- **Frontier standing**:" in report


def test_all_findings_respect_priority_floor(evo_dir: Path):
    records = [{"date": "d0", "merged": 1}] + [
        {
            "date": f"d{i}",
            "issues_created": 0,
            "selected": 1,
            "rejected": 9,
            "merged": 0,
        }
        for i in range(1, 8)
    ]
    _write_metrics(evo_dir, records)
    findings = run_local_research(evo_dir)["findings"]
    assert findings  # signals present
    assert all(f["priority_score"] >= 0.7 for f in findings)


# ── main() writes a file and does not clobber a live report ───────────────────


def test_main_writes_report_file(evo_dir: Path):
    records = [{"date": "d0", "merged": 1}] + [
        {"date": f"d{i}", "merged": 0} for i in range(1, 4)
    ]
    _write_metrics(evo_dir, records)

    rc = main(["--evolution-dir", str(evo_dir)])
    assert rc == 0

    reports = list((evo_dir / "research").glob("*.md"))
    assert len(reports) == 1
    assert "<!-- evolution-research: local-state fallback -->" in reports[0].read_text()


def test_main_does_not_clobber_live_report(evo_dir: Path):
    from datetime import datetime, timezone

    _write_metrics(evo_dir, [{"date": "d0", "merged": 0} for _ in range(4)])
    research_dir = evo_dir / "research"
    research_dir.mkdir()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    live = research_dir / f"{today}.md"
    live.write_text(
        "# Research Report - live\n\nreal frontier scan\n", encoding="utf-8"
    )

    rc = main(["--evolution-dir", str(evo_dir)])
    assert rc == 0
    # live report is preserved (no local marker written over it)
    assert "real frontier scan" in live.read_text()


def test_main_missing_dir_returns_error(tmp_path: Path):
    assert main(["--evolution-dir", str(tmp_path / "absent")]) == 1


# ── profile-dir resolution: never hardcode 'user1' (#733 review fix) ──────────


def test_default_dir_prefers_evolution_profile_dir_env(monkeypatch, tmp_path):
    target = tmp_path / "srv" / "evolution"
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(target))
    assert _default_evolution_dir() == target


def test_default_dir_uses_default_profile_not_user1(monkeypatch, tmp_path):
    monkeypatch.delenv("EVOLUTION_PROFILE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    resolved = _default_evolution_dir()
    assert resolved == tmp_path / "profiles" / "default" / "evolution"
    assert "user1" not in str(resolved)


def test_default_dir_honors_active_profile_marker(monkeypatch, tmp_path):
    monkeypatch.delenv("EVOLUTION_PROFILE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "active_profile").write_text("osoba\n", encoding="utf-8")
    resolved = _default_evolution_dir()
    assert resolved == tmp_path / "profiles" / "osoba" / "evolution"
