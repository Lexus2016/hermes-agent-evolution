"""Tests for scripts/evolution_watchdog.py — deterministic pipeline health check."""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_watchdog import (  # noqa: E402
    STAGES,
    check_gh,
    check_jobs,
    check_stage_reports,
    check_upstream_lag,
    expected_report_date,
)


NOW = datetime(2026, 6, 11, 7, 45)  # 07:45 — after yesterday's full chain


class TestExpectedReportDate:
    def test_before_slot_plus_grace_expects_yesterday(self):
        # research slot is 09:00; at 07:45 today's slot hasn't happened.
        d = expected_report_date(NOW, slot_hour=9, grace_hours=2)
        assert d == "2026-06-10"

    def test_after_slot_plus_grace_expects_today(self):
        late = NOW.replace(hour=12)  # 12:00 > 09:00 + 2h grace
        d = expected_report_date(late, slot_hour=9, grace_hours=2)
        assert d == "2026-06-11"

    def test_within_grace_still_expects_yesterday(self):
        within = NOW.replace(hour=10)  # 10:00 < 09:00 + 2h grace
        d = expected_report_date(within, slot_hour=9, grace_hours=2)
        assert d == "2026-06-10"


class TestStageReports:
    def _make_reports(self, tmp_path, date="2026-06-10", skip=(), tiny=()):
        for stage, (slot, ext) in STAGES.items():
            if stage in skip:
                continue
            d = tmp_path / stage
            d.mkdir(exist_ok=True)
            content = "x" * 10 if stage in tiny else "x" * 500
            (d / f"{date}.{ext}").write_text(content)

    def test_all_present_no_alerts(self, tmp_path):
        self._make_reports(tmp_path)
        assert check_stage_reports(tmp_path, NOW) == []

    def test_missing_report_alerts(self, tmp_path):
        self._make_reports(tmp_path, skip=("implementation",))
        alerts = check_stage_reports(tmp_path, NOW)
        assert len(alerts) == 1
        assert "implementation" in alerts[0]
        assert "2026-06-10" in alerts[0]

    def test_trivially_small_report_alerts(self, tmp_path):
        self._make_reports(tmp_path, tiny=("analysis",))
        alerts = check_stage_reports(tmp_path, NOW)
        assert len(alerts) == 1
        assert "analysis" in alerts[0]
        assert "small" in alerts[0]

    def test_missing_stage_dir_alerts(self, tmp_path):
        # No dirs at all — every stage should alert, not crash.
        alerts = check_stage_reports(tmp_path, NOW)
        assert len(alerts) == len(STAGES)

    def _jobs_file(self, tmp_path, name, *, status="ok", last_run="2026-06-10T22:01:00"):
        f = tmp_path / "jobs.json"
        f.write_text(json.dumps({"jobs": [
            {"name": name, "enabled": True, "last_status": status, "last_run_at": last_run}
        ]}))
        return f

    def test_missing_report_quiet_when_job_ran_clean(self, tmp_path):
        # implementation report missing, but its cron job ran ok at/after the
        # 22:00 slot for the expected date (2026-06-10) → idle clean cycle, no alert.
        self._make_reports(tmp_path, skip=("implementation",))
        jf = self._jobs_file(tmp_path, "evolution-implementation")
        assert check_stage_reports(tmp_path, NOW, jf) == []

    def test_missing_report_alerts_when_job_errored(self, tmp_path):
        # missing report + job FAILED → still a real anomaly.
        self._make_reports(tmp_path, skip=("implementation",))
        jf = self._jobs_file(tmp_path, "evolution-implementation", status="error")
        alerts = check_stage_reports(tmp_path, NOW, jf)
        assert len(alerts) == 1 and "implementation" in alerts[0]

    def test_missing_report_alerts_when_job_ran_before_slot(self, tmp_path):
        # job ran ok but BEFORE the slot (stale/previous day) → not this slot's
        # clean run → still alert.
        self._make_reports(tmp_path, skip=("integration",))
        jf = self._jobs_file(tmp_path, "evolution-integration", last_run="2026-06-09T23:00:00")
        alerts = check_stage_reports(tmp_path, NOW, jf)
        assert len(alerts) == 1 and "integration" in alerts[0]


class TestJobsHealth:
    def _jobs_file(self, tmp_path, jobs):
        p = tmp_path / "jobs.json"
        p.write_text(json.dumps({"jobs": jobs}))
        return p

    def _job(self, name="evolution-analysis", **over):
        base = {
            "id": "abc123",
            "name": name,
            "enabled": True,
            "state": "scheduled",
            "last_status": "ok",
            "last_run_at": (NOW - timedelta(hours=10)).isoformat(),
            "last_error": None,
        }
        base.update(over)
        return base

    def test_healthy_jobs_no_alerts(self, tmp_path):
        p = self._jobs_file(tmp_path, [self._job()])
        assert check_jobs(p, NOW) == []

    def test_error_status_alerts(self, tmp_path):
        p = self._jobs_file(
            tmp_path, [self._job(last_status="error", last_error="boom")]
        )
        alerts = check_jobs(p, NOW)
        assert len(alerts) == 1
        assert "boom" in alerts[0]

    def test_stale_last_run_alerts(self, tmp_path):
        p = self._jobs_file(
            tmp_path,
            [self._job(last_run_at=(NOW - timedelta(hours=30)).isoformat())],
        )
        alerts = check_jobs(p, NOW)
        assert len(alerts) == 1
        assert "26h" in alerts[0] or "stale" in alerts[0]

    def test_never_ran_alerts(self, tmp_path):
        old_created = (NOW - timedelta(days=5)).isoformat()
        p = self._jobs_file(
            tmp_path,
            [self._job(last_run_at=None, last_status=None, created_at=old_created)],
        )
        alerts = check_jobs(p, NOW)
        assert len(alerts) == 1
        assert "never" in alerts[0]

    def test_freshly_registered_never_ran_is_quiet(self, tmp_path):
        # Re-registration wipes run history; a job younger than its cadence
        # window must not alert (its first slot simply hasn't come yet).
        fresh_created = (NOW - timedelta(hours=5)).isoformat()
        p = self._jobs_file(
            tmp_path,
            [self._job(last_run_at=None, last_status=None, created_at=fresh_created)],
        )
        assert check_jobs(p, NOW) == []

    def test_non_evolution_jobs_ignored(self, tmp_path):
        p = self._jobs_file(
            tmp_path,
            [self._job(name="My Personal Job", last_status="error", last_error="x")],
        )
        assert check_jobs(p, NOW) == []

    def test_disabled_jobs_ignored(self, tmp_path):
        p = self._jobs_file(
            tmp_path, [self._job(enabled=False, last_status="error", last_error="x")]
        )
        assert check_jobs(p, NOW) == []

    def test_weekly_job_uses_8day_threshold(self, tmp_path):
        # upstream-sync runs weekly — 30h-old last run must NOT alert.
        p = self._jobs_file(
            tmp_path,
            [
                self._job(
                    name="evolution-upstream-sync",
                    last_run_at=(NOW - timedelta(hours=30)).isoformat(),
                )
            ],
        )
        assert check_jobs(p, NOW) == []

    def test_missing_jobs_file_alerts(self, tmp_path):
        alerts = check_jobs(tmp_path / "nope.json", NOW)
        assert len(alerts) == 1


class TestGhCheck:
    def test_auth_failure_alerts(self):
        def fake_run(cmd):
            return (1, "not logged in")

        alerts = check_gh(runner=fake_run)
        assert any("auth" in a for a in alerts)

    def test_low_rate_alerts(self):
        def fake_run(cmd):
            if "rate_limit" in " ".join(cmd):
                return (0, json.dumps({"resources": {"core": {"remaining": 12}}}))
            return (0, "ok")

        alerts = check_gh(runner=fake_run)
        assert any("rate" in a for a in alerts)

    def test_healthy_no_alerts(self):
        def fake_run(cmd):
            if "rate_limit" in " ".join(cmd):
                return (0, json.dumps({"resources": {"core": {"remaining": 4900}}}))
            return (0, "ok")

        assert check_gh(runner=fake_run) == []

    def test_gh_missing_alerts(self):
        def fake_run(cmd):
            raise FileNotFoundError("gh")

        alerts = check_gh(runner=fake_run)
        assert len(alerts) >= 1


class TestUpstreamLag:
    REPO = Path("/repo")  # bypass _resolve_repo_dir via explicit repo_dir

    def test_behind_over_threshold_alerts(self):
        def fake_run(cmd):
            assert "rev-list" in cmd
            return (0, "301\n")

        alerts = check_upstream_lag(runner=fake_run, repo_dir=self.REPO)
        assert any("behind upstream" in a for a in alerts)
        assert any("301" in a for a in alerts)

    def test_within_threshold_silent(self):
        def fake_run(cmd):
            return (0, "9\n")

        assert check_upstream_lag(runner=fake_run, repo_dir=self.REPO) == []

    def test_at_threshold_silent(self):
        def fake_run(cmd):
            return (0, "80\n")  # exactly the threshold is not "over"

        assert check_upstream_lag(runner=fake_run, repo_dir=self.REPO) == []

    def test_git_failure_silent(self):
        def fake_run(cmd):
            return (1, "fatal: bad revision 'upstream/main'")

        assert check_upstream_lag(runner=fake_run, repo_dir=self.REPO) == []

    def test_garbage_output_silent(self):
        def fake_run(cmd):
            return (0, "not-a-number")

        assert check_upstream_lag(runner=fake_run, repo_dir=self.REPO) == []

    def test_spawn_error_silent(self):
        def fake_run(cmd):
            raise FileNotFoundError("git")

        assert check_upstream_lag(runner=fake_run, repo_dir=self.REPO) == []

    def test_no_repo_silent(self, monkeypatch):
        import evolution_watchdog as w

        monkeypatch.setattr(w, "_resolve_repo_dir", lambda: None)

        def fake_run(cmd):
            raise AssertionError("runner must not run when repo is unresolved")

        assert check_upstream_lag(runner=fake_run) == []


class TestStagesMirrorCronSpecs:
    """STAGES duplicates cron/evolution/*.yaml; lock the two together.

    Regression for the 2026-06-12 false alarm: integration writes
    {date}.json (integration.yaml output.file) but STAGES said "md",
    so the watchdog reported a healthy run as a dead job.
    """

    CRON_DIR = Path(__file__).resolve().parents[2] / "cron" / "evolution"

    def test_extension_matches_output_file(self):
        for stage, (_slot, ext) in STAGES.items():
            spec = (self.CRON_DIR / f"{stage}.yaml").read_text()
            m = re.search(r"^\s*file:.*\{current_date\}\.(\w+)\s*$", spec, re.M)
            assert m, f"{stage}.yaml has no output.file with {{current_date}}"
            assert m.group(1) == ext, (
                f"watchdog STAGES says '{stage}' reports are .{ext}, "
                f"but {stage}.yaml writes .{m.group(1)}"
            )

    def test_slot_hour_matches_schedule(self):
        for stage, (slot, _ext) in STAGES.items():
            spec = (self.CRON_DIR / f"{stage}.yaml").read_text()
            m = re.search(r'^schedule:\s*"(\d+)\s+(\d+)\s', spec, re.M)
            assert m, f"{stage}.yaml has no parsable daily schedule"
            assert int(m.group(2)) == slot, (
                f"watchdog STAGES says '{stage}' runs at {slot:02d}:00, "
                f"but {stage}.yaml schedules hour {m.group(2)}"
            )


class TestCheckHealth:
    from evolution_watchdog import check_health

    def test_healthy_sidecar_is_silent(self, tmp_path):
        from evolution_watchdog import check_health
        (tmp_path / "evolution-health.txt").write_text(
            "[evolution-metrics] 5/5 active cycles: success=80% ... | healthy\n", encoding="utf-8"
        )
        assert check_health(tmp_path) == []

    def test_flagged_sidecar_alerts(self, tmp_path):
        from evolution_watchdog import check_health
        (tmp_path / "evolution-health.txt").write_text(
            "[evolution-metrics] 4/4 active cycles: success=10% ... | "
            "LOW_SUCCESS: <1/3 of active cycles land a merge\n",
            encoding="utf-8",
        )
        alerts = check_health(tmp_path)
        assert len(alerts) == 1 and "health degraded" in alerts[0] and "LOW_SUCCESS" in alerts[0]

    def test_missing_sidecar_is_silent(self, tmp_path):
        from evolution_watchdog import check_health
        assert check_health(tmp_path) == []
