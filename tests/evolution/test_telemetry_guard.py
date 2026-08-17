"""Tests for evolution/lib/telemetry_guard.py (#2637)."""

import json

from evolution.lib.telemetry_guard import (
    check_telemetry,
    validate_health_text,
    validate_metrics_line,
    validate_metrics_record,
)

HEALTH_OK = "[evolution-metrics] 19/22 active cycles: success=95% selection_efficiency=49% reject_rate=48% merged_trend=improving (created=29 selected=67 merged=167) effort_budget=3.0 | healthy"
RECORD_OK = {
    "date": "2026-08-17",
    "issues_created": 3,
    "selected": 5,
    "rejected": 1,
    "merged": 2,
    "skipped": 0,
}


def test_valid_record_is_safe():
    assert validate_metrics_record(RECORD_OK).safe is True


def test_record_missing_key_is_unsafe():
    bad = dict(RECORD_OK)
    del bad["merged"]
    verdict = validate_metrics_record(bad)
    assert verdict.safe is False and "merged" in verdict.reason


def test_negative_count_is_unsafe():
    assert validate_metrics_record(dict(RECORD_OK, selected=-1)).safe is False


def test_nan_count_is_unsafe():
    assert validate_metrics_record(dict(RECORD_OK, merged=float("nan"))).safe is False


def test_non_object_record_is_unsafe():
    assert validate_metrics_record("nope").safe is False
    assert validate_metrics_record(None).safe is False


def test_metrics_line_json_error_is_unsafe():
    assert validate_metrics_line("{not json").safe is False


def test_metrics_line_blank_is_safe():
    assert validate_metrics_line("   ").safe is True


def test_health_ok_legacy_and_tampered():
    assert validate_health_text(HEALTH_OK).safe is True
    assert validate_health_text("[evolution-metrics] effort_budget=1.5").safe is True
    assert (
        validate_health_text("[evolution-metrics] LOW_SELECTION_EFFICIENCY").safe
        is False
    )
    assert validate_health_text("totally arbitrary effort_budget=1.5").safe is False


def test_check_telemetry_fails_closed(tmp_path):
    (tmp_path / "metrics.jsonl").write_text(
        json.dumps(RECORD_OK) + "\n", encoding="utf-8"
    )
    (tmp_path / "evolution-health.txt").write_text(HEALTH_OK, encoding="utf-8")
    assert check_telemetry(tmp_path)["safe"] is True

    # A single tampered steering file fails the whole aggregate closed.
    (tmp_path / "evolution-health.txt").write_text(
        "[garbage] effort_budget=1.5", encoding="utf-8"
    )
    report = check_telemetry(tmp_path)
    assert report["safe"] is False
    assert report["checks"]["health"]["safe"] is False
