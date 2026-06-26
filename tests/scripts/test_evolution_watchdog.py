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
    def _make_reports(self, tmp_path, date=None, skip=(), tiny=()):
        # Each stage's report is dated at ITS OWN expected slot date (slot-aware),
        # so the helper stays correct regardless of per-stage schedules.
        for stage, (slot, ext) in STAGES.items():
            if stage in skip:
                continue
            d = tmp_path / stage
            d.mkdir(exist_ok=True)
            dt = date or expected_report_date(NOW, slot)
            content = "x" * 10 if stage in tiny else "x" * 500
            (d / f"{dt}.{ext}").write_text(content)

    def test_all_present_no_alerts(self, tmp_path):
        self._make_reports(tmp_path)
        assert check_stage_reports(tmp_path, NOW) == []

    def test_missing_report_alerts(self, tmp_path):
        self._make_reports(tmp_path, skip=("implementation",))
        alerts = check_stage_reports(tmp_path, NOW)
        assert len(alerts) == 1
        assert "implementation" in alerts[0]
        exp = expected_report_date(NOW, STAGES["implementation"][0])
        assert exp in alerts[0]

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
        # slot for the expected date → idle clean cycle, no alert. Slot-aware.
        slot = STAGES["implementation"][0]
        exp = expected_report_date(NOW, slot)
        self._make_reports(tmp_path, skip=("implementation",))
        jf = self._jobs_file(
            tmp_path, "evolution-implementation",
            last_run=f"{exp}T{slot:02d}:01:00",
        )
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

    def test_shallow_clone_silent_no_phantom_count(self):
        # The installer's `git clone --depth 1` default: a shallow repo. The
        # behind-count would balloon to ~all of upstream history (the 2026-06
        # phantom "~13000 commits behind" alarm on every onboarded client).
        # The shallow probe must short-circuit BEFORE rev-list is consulted, and
        # the result must be SILENT (no alert) — shallow is the intended default.
        def fake_run(cmd):
            if "rev-parse" in cmd and "--is-shallow-repository" in cmd:
                return (0, "true\n")
            if "rev-list" in cmd:
                raise AssertionError(
                    "rev-list must NOT run on a shallow clone — its count is phantom"
                )
            return (0, "")

        assert check_upstream_lag(runner=fake_run, repo_dir=self.REPO) == []

    def test_unresolved_merge_base_silent(self):
        # Non-shallow, but HEAD and upstream/main share no common ancestor
        # (grafted/no-shared-history): `merge-base` exits non-zero with EMPTY
        # stdout. The count is just as meaningless, so skip silently too. A
        # missing-remote case (non-zero exit WITH text) is deliberately NOT
        # treated as unmeasurable here — that falls through to rev-list.
        def fake_run(cmd):
            if "rev-parse" in cmd and "--is-shallow-repository" in cmd:
                return (0, "false\n")
            if "merge-base" in cmd:
                return (1, "")  # no common ancestor: non-zero, empty stdout
            if "rev-list" in cmd:
                raise AssertionError(
                    "rev-list must NOT run when HEAD has no shared history with upstream"
                )
            return (0, "")

        assert check_upstream_lag(runner=fake_run, repo_dir=self.REPO) == []

    def test_full_clone_behind_over_threshold_still_alerts(self):
        # Regression guard: a normal FULL clone (not shallow, shared ancestry)
        # that is genuinely behind must still alert — the evolution server is a
        # full clone and the real upstream-lag monitoring must survive this fix.
        def fake_run(cmd):
            if "rev-parse" in cmd and "--is-shallow-repository" in cmd:
                return (0, "false\n")
            if "merge-base" in cmd:
                return (0, "abc123def456\n")  # shared ancestor exists
            if "rev-list" in cmd:
                return (0, "391\n")
            return (0, "")

        alerts = check_upstream_lag(runner=fake_run, repo_dir=self.REPO)
        assert any("behind upstream/main" in a for a in alerts)
        assert any("391" in a for a in alerts)


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
            # Hour field may be a single hour ("21") or a multi-slot list
            # ("1,5,9,13,17,21"); STAGES mirrors the FIRST slot.
            m = re.search(r'^schedule:\s*"(\d+)\s+([\d,]+)\s', spec, re.M)
            assert m, f"{stage}.yaml has no parsable schedule"
            first_hour = int(m.group(2).split(",")[0])
            assert first_hour == slot, (
                f"watchdog STAGES says '{stage}' first slot is {slot:02d}:00, "
                f"but {stage}.yaml's first scheduled hour is {first_hour}"
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


class TestEdgeTrigger:
    """Edge-triggering for the steady-state HEALTH alerts.

    Suppresses the *verbatim repeat* of an already-reported, non-worsening
    health condition (alert fatigue), while ALWAYS emitting a new fault, a
    worsening of an existing one, a recovery, and a long-cooldown nudge.
    State persists in a small JSON beside the sidecars; all reads/writes are
    fail-open (missing/corrupt → behave like today and emit).
    """

    # A representative steady health condition (the 11% selection-efficiency
    # case that re-screamed daily). Body counts drift run to run; only the
    # flag tail after the final '|' is the actual condition.
    COND_A = [
        "pipeline health degraded: [evolution-metrics] 4/4 active cycles: "
        "success=22% selection_efficiency=11% reject_rate=0% merged_trend=flat "
        "(created=2 selected=9 merged=1) effort_budget=1.5 | "
        "LOW_SELECTION_EFFICIENCY: picks more than it can land "
        "(poor self-capability calibration)"
    ]
    # Same condition, NEXT run: body counts moved but the flag tail is identical.
    COND_A_DRIFTED = [
        "pipeline health degraded: [evolution-metrics] 5/5 active cycles: "
        "success=20% selection_efficiency=12% reject_rate=0% merged_trend=flat "
        "(created=3 selected=8 merged=1) effort_budget=1.5 | "
        "LOW_SELECTION_EFFICIENCY: picks more than it can land "
        "(poor self-capability calibration)"
    ]
    # A genuinely WORSE state: a second, harsher flag now also present.
    COND_A_WORSE = COND_A + [
        "pipeline health degraded: [evolution-metrics] 4/4 active cycles: "
        "success=10% selection_efficiency=11% reject_rate=0% merged_trend=declining "
        "(created=2 selected=9 merged=0) effort_budget=1.5 | "
        "LOW_SUCCESS: <1/3 of active cycles land a merge"
    ]
    # A NEW, distinct condition from a different sidecar.
    COND_B = [
        "realized-impact degraded: [evolution-realized] | "
        "REALIZED_IMPACT_LOW: last 3 merged changes delivered no real value"
    ]

    def _state(self, tmp_path):
        return tmp_path / "watchdog-alert-state.json"

    def test_steady_identical_condition_emits_then_suppresses(self, tmp_path):
        from evolution_watchdog import apply_edge_trigger

        sp = self._state(tmp_path)
        t0 = datetime(2026, 6, 20, 7, 47)
        # Run 1: first time we see the condition → emit.
        out1 = apply_edge_trigger(self.COND_A, sp, t0)
        assert out1 == self.COND_A

        # Run 2 next day, identical condition (and the noisy body drifted) →
        # SUPPRESSED (within cooldown): no new information.
        t1 = t0 + timedelta(days=1)
        out2 = apply_edge_trigger(self.COND_A_DRIFTED, sp, t1)
        assert out2 == []

    def test_new_flag_appearing_is_never_masked(self, tmp_path):
        from evolution_watchdog import apply_edge_trigger

        sp = self._state(tmp_path)
        t0 = datetime(2026, 6, 20, 7, 47)
        apply_edge_trigger(self.COND_A, sp, t0)  # establish baseline
        # Run 2: a brand-new distinct flag appears → MUST emit (no mask).
        t1 = t0 + timedelta(days=1)
        out = apply_edge_trigger(self.COND_B, sp, t1)
        assert out == self.COND_B

    def test_worsening_condition_is_never_masked(self, tmp_path):
        from evolution_watchdog import apply_edge_trigger

        sp = self._state(tmp_path)
        t0 = datetime(2026, 6, 20, 7, 47)
        apply_edge_trigger(self.COND_A, sp, t0)
        # Run 2: original flag PLUS a new harsher flag (escalation) → emit.
        t1 = t0 + timedelta(days=1)
        out = apply_edge_trigger(self.COND_A_WORSE, sp, t1)
        assert out == self.COND_A_WORSE

    def test_merged_zero_streak_growth_is_worsening(self, tmp_path):
        # A counter embedded in the flag tail growing (x3 -> x5) is a worsening
        # of the SAME condition and must still alert — the tail changes.
        from evolution_watchdog import apply_edge_trigger

        sp = self._state(tmp_path)
        t0 = datetime(2026, 6, 20, 7, 47)
        a3 = ["pipeline health degraded: ... | MERGED_ZERO x3: integration stuck"]
        a5 = ["pipeline health degraded: ... | MERGED_ZERO x5: integration stuck"]
        assert apply_edge_trigger(a3, sp, t0) == a3
        out = apply_edge_trigger(a5, sp, t0 + timedelta(days=1))
        assert out == a5

    def test_condition_clears_emits_recovery(self, tmp_path):
        from evolution_watchdog import apply_edge_trigger

        sp = self._state(tmp_path)
        t0 = datetime(2026, 6, 20, 7, 47)
        apply_edge_trigger(self.COND_A, sp, t0)
        # Run 2: no health alerts at all → recovery, worth a single notice.
        t1 = t0 + timedelta(days=1)
        out = apply_edge_trigger([], sp, t1)
        assert len(out) == 1
        assert "recover" in out[0].lower() or "clear" in out[0].lower()

        # Run 3: still healthy → silent (recovery already announced once).
        out3 = apply_edge_trigger([], sp, t1 + timedelta(days=1))
        assert out3 == []

    def test_recovery_then_recurrence_is_never_masked(self, tmp_path):
        # No-mask regression: after a condition CLEARS (recovery persisted as
        # the healthy baseline), the SAME fault reappearing soon after is a NEW
        # transition and must alert again — it must not be suppressed as if the
        # old (pre-recovery) state were still current.
        from evolution_watchdog import apply_edge_trigger

        sp = self._state(tmp_path)
        t0 = datetime(2026, 6, 20, 7, 47)
        assert apply_edge_trigger(self.COND_A, sp, t0) == self.COND_A          # fault
        assert len(apply_edge_trigger([], sp, t0 + timedelta(days=1))) == 1     # recovery
        # Recurrence the very next day (well within the 7d cooldown):
        out = apply_edge_trigger(self.COND_A, sp, t0 + timedelta(days=2))
        assert out == self.COND_A, "a fault recurring after recovery must re-alert"

    def test_persisting_past_cooldown_emits_reminder(self, tmp_path):
        from evolution_watchdog import EDGE_COOLDOWN_DAYS, apply_edge_trigger

        sp = self._state(tmp_path)
        t0 = datetime(2026, 6, 20, 7, 47)
        assert apply_edge_trigger(self.COND_A, sp, t0) == self.COND_A
        # Within cooldown → suppressed.
        assert apply_edge_trigger(self.COND_A, sp, t0 + timedelta(days=1)) == []
        # Past the cooldown, unchanged → a single "still unresolved" nudge.
        later = t0 + timedelta(days=EDGE_COOLDOWN_DAYS + 1)
        out = apply_edge_trigger(self.COND_A, sp, later)
        assert out, "a long-persisting condition must re-remind, never go silent forever"
        assert any("LOW_SELECTION_EFFICIENCY" in a for a in out)
        # Cooldown clock resets after the reminder → next day suppressed again.
        assert apply_edge_trigger(self.COND_A, sp, later + timedelta(days=1)) == []

    def test_missing_state_file_fails_open_emits(self, tmp_path):
        from evolution_watchdog import apply_edge_trigger

        sp = self._state(tmp_path)  # does not exist yet
        assert not sp.exists()
        out = apply_edge_trigger(self.COND_A, sp, datetime(2026, 6, 20, 7, 47))
        assert out == self.COND_A  # behaves like today: emit

    def test_corrupt_state_file_fails_open_emits(self, tmp_path):
        from evolution_watchdog import apply_edge_trigger

        sp = self._state(tmp_path)
        sp.write_text("{not valid json", encoding="utf-8")
        out = apply_edge_trigger(self.COND_A, sp, datetime(2026, 6, 20, 7, 47))
        assert out == self.COND_A  # corrupt == unknown previous state → emit

    def test_unwritable_state_dir_fails_open_emits(self, tmp_path):
        # Persistence failure must NEVER crash or swallow the alert.
        from evolution_watchdog import apply_edge_trigger

        sp = tmp_path / "no-such-dir" / "watchdog-alert-state.json"
        out = apply_edge_trigger(self.COND_A, sp, datetime(2026, 6, 20, 7, 47))
        assert out == self.COND_A

    def test_signature_ignores_drifting_body_counts(self, tmp_path):
        # The condition signature keys on the flag tail, not the noisy metrics
        # body — otherwise every run looks "new" and nothing is ever suppressed.
        from evolution_watchdog import health_signature

        assert health_signature(self.COND_A) == health_signature(self.COND_A_DRIFTED)
        assert health_signature(self.COND_A) != health_signature(self.COND_A_WORSE)
        assert health_signature([]) == ""

    def test_signature_is_order_independent(self, tmp_path):
        from evolution_watchdog import health_signature

        ab = self.COND_A + self.COND_B
        ba = self.COND_B + self.COND_A
        assert health_signature(ab) == health_signature(ba)


class TestMainEdgeTriggerWiring:
    """main() must route ONLY health alerts through the edge-trigger and leave
    operational alerts (upstream-lag, stage reports, jobs, gh) untouched."""

    def test_upstream_lag_and_infra_alerts_bypass_edge_trigger(self, tmp_path, monkeypatch, capsys):
        import evolution_watchdog as w

        # Infra/operational alerts present every run; health alerts steady.
        monkeypatch.setattr(w, "check_stage_reports", lambda *a, **k: [])
        monkeypatch.setattr(w, "check_jobs", lambda *a, **k: [])
        monkeypatch.setattr(w, "check_gh", lambda *a, **k: ["gh auth status FAILED"])
        monkeypatch.setattr(
            w, "check_upstream_lag", lambda *a, **k: ["upstream sync stuck: fork is 301 behind"]
        )
        monkeypatch.setattr(
            w, "check_health", lambda *a, **k: [
                "pipeline health degraded: x | LOW_SELECTION_EFFICIENCY: y"
            ]
        )
        monkeypatch.setattr(w, "check_realized_impact", lambda *a, **k: [])
        monkeypatch.setattr(w, "check_analysis_integrity", lambda *a, **k: [])
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))

        # Run 1: everything emits.
        w.main()
        out1 = capsys.readouterr().out
        assert "upstream sync stuck" in out1
        assert "gh auth status FAILED" in out1
        assert "LOW_SELECTION_EFFICIENCY" in out1

        # Run 2: health is suppressed (steady), but upstream-lag + gh STILL fire.
        w.main()
        out2 = capsys.readouterr().out
        assert "upstream sync stuck" in out2, "operational upstream-lag must never be edge-suppressed"
        assert "gh auth status FAILED" in out2, "operational gh failure must never be edge-suppressed"
        assert "LOW_SELECTION_EFFICIENCY" not in out2, "steady health condition should be suppressed"
