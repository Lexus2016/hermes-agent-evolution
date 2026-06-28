"""Tests for scripts/cron_health_check.py."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import cron_health_check as chc  # noqa: E402


def _make_record(
    job_id: str,
    success: bool,
    category: str | None = None,
    ts: datetime | None = None,
) -> dict:
    return {
        "job_id": job_id,
        "job_name": "test job",
        "timestamp": (ts or datetime.now(timezone.utc)).isoformat(),
        "success": success,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "failure_category": category,
        "error": None if success else "ReadError: broken pipe",
        "last_output": "",
        "traceback": None,
    }


def _write_records(home: Path, job_id: str, records: list) -> None:
    failure_dir = home / "cron" / "failures" / job_id
    failure_dir.mkdir(parents=True)
    for i, rec in enumerate(records):
        ts = datetime.fromisoformat(rec["timestamp"])
        path = failure_dir / f"{ts.strftime('%Y-%m-%d_%H-%M-%S')}_{i:06d}.json"
        path.write_text(json.dumps(rec), encoding="utf-8")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import get_hermes_home

    assert str(get_hermes_home()) == str(home)
    return home


def test_build_report_flags_unhealthy_high_failure_rate(isolated_home):
    from cron.jobs import create_job

    now = datetime.now(timezone.utc)
    job = create_job(prompt="test", schedule="0 0 * * *", name="failing job")
    records = [
        _make_record(job["id"], False, "timeout", now - timedelta(hours=i))
        for i in range(4)
    ] + [_make_record(job["id"], True, None, now - timedelta(hours=4))]
    _write_records(isolated_home, job["id"], records)

    with patch.object(chc, "_load_jobs", return_value=[job]):
        report = chc.build_report(days=7, alert_threshold=0.75, consecutive_threshold=5)

    assert not report["healthy"]
    assert len(report["unhealthy_jobs"]) == 1
    h = report["unhealthy_jobs"][0]
    assert h["job_id"] == job["id"]
    assert h["failure_rate"] == 0.8
    assert h["categories"]["timeout"] == 4


def test_build_report_flags_unhealthy_consecutive_failures(isolated_home):
    from cron.jobs import create_job

    now = datetime.now(timezone.utc)
    job = create_job(prompt="test", schedule="0 0 * * *", name="consecutive")
    records = [
        _make_record(job["id"], False, "timeout", now - timedelta(minutes=10 * i))
        for i in range(3)
    ] + [_make_record(job["id"], True, None, now - timedelta(minutes=40))]
    _write_records(isolated_home, job["id"], records)

    with patch.object(chc, "_load_jobs", return_value=[job]):
        report = chc.build_report(days=7, alert_threshold=1.0, consecutive_threshold=3)

    assert not report["healthy"]
    assert report["unhealthy_jobs"][0]["consecutive_failures"] == 3


def test_build_report_healthy_when_successes_dominate(isolated_home):
    from cron.jobs import create_job

    now = datetime.now(timezone.utc)
    job = create_job(prompt="test", schedule="0 0 * * *", name="mostly-ok")
    records = [
        _make_record(job["id"], True, None, now - timedelta(hours=i)) for i in range(5)
    ] + [_make_record(job["id"], False, "timeout", now - timedelta(hours=5))]
    _write_records(isolated_home, job["id"], records)

    with patch.object(chc, "_load_jobs", return_value=[job]):
        report = chc.build_report(days=7)

    assert report["healthy"]
    assert report["unhealthy_jobs"] == []
    assert report["total_jobs_with_data"] == 1


def test_build_report_no_data(isolated_home):
    with patch.object(chc, "_load_jobs", return_value=[]):
        report = chc.build_report(days=7)

    assert report["healthy"]
    assert report["total_jobs_with_data"] == 0


def test_aggregate_provider_model_summary(isolated_home):
    from cron.jobs import create_job

    now = datetime.now(timezone.utc)
    job = create_job(
        prompt="test",
        schedule="0 0 * * *",
        name="agg",
        model="deepseek/deepseek-v4-flash",
        provider="deepseek",
    )
    records = [
        _make_record(job["id"], False, "timeout", now - timedelta(hours=i))
        for i in range(2)
    ]
    _write_records(isolated_home, job["id"], records)

    with patch.object(chc, "_load_jobs", return_value=[job]):
        report = chc.build_report(days=7, alert_threshold=0.5, consecutive_threshold=5)

    assert report["provider_summary"]["deepseek"]["failure_rate"] == 1.0
    assert report["model_summary"]["deepseek/deepseek-v4-flash"]["failure_rate"] == 1.0


def test_format_alert_body_contains_job_details():
    report = {
        "generated_at": "2026-06-28T12:00:00+00:00",
        "lookback_days": 7,
        "alert_threshold": 0.75,
        "consecutive_threshold": 3,
        "unhealthy_jobs": [
            {
                "job_name": "evolution-introspection",
                "job_id": "abc123",
                "failure_count": 8,
                "total_runs": 8,
                "failure_rate": 1.0,
                "consecutive_failures": 8,
                "model": "deepseek-v4-flash",
                "provider": "deepseek",
                "categories": {"timeout": 8},
            }
        ],
    }
    body = chc._format_alert_body(report)
    assert "evolution-introspection" in body
    assert "abc123" in body
    assert "timeout" in body
    assert "deepseek-v4-flash" in body


def test_main_dry_run_reports_unhealthy(isolated_home, capsys):
    from cron.jobs import create_job

    now = datetime.now(timezone.utc)
    job = create_job(prompt="test", schedule="0 0 * * *", name="dry-fail")
    records = [
        _make_record(job["id"], False, "timeout", now - timedelta(hours=i))
        for i in range(5)
    ]
    _write_records(isolated_home, job["id"], records)

    with patch.object(chc, "_load_jobs", return_value=[job]):
        rc = chc.main([
            "--dry-run",
            "--alert-threshold",
            "0.5",
            "--consecutive-threshold",
            "5",
        ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "dry-fail" in captured.out


def test_save_report_writes_latest_json(isolated_home):
    from cron.jobs import create_job

    now = datetime.now(timezone.utc)
    job = create_job(prompt="test", schedule="0 0 * * *", name="persist")
    records = [
        _make_record(job["id"], False, "timeout", now - timedelta(hours=i))
        for i in range(5)
    ]
    _write_records(isolated_home, job["id"], records)

    with patch.object(chc, "_load_jobs", return_value=[job]):
        report = chc.build_report(days=7, alert_threshold=0.5, consecutive_threshold=5)
        out = chc._save_report(report)

    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["healthy"] is False
    assert loaded["unhealthy_jobs"][0]["job_id"] == job["id"]
