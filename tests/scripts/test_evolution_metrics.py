"""Tests for scripts/evolution_metrics.py — longitudinal meta-evolution health."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_metrics import compute_health, format_health, main


def _rec(date, created=0, selected=0, rejected=0, merged=0):
    return {
        "date": date,
        "issues_created": created,
        "selected": selected,
        "rejected": rejected,
        "merged": merged,
    }


class TestComputeHealth:
    def test_idle_and_artifact_cycles_excluded_from_active(self):
        recs = [
            _rec("d1", selected=2, merged=1),
            _rec("d2"),  # idle / all-zero artifact — must NOT count as a failed cycle
            _rec("d3", created=3, selected=0, merged=0),  # active (created>0), no merge
        ]
        h = compute_health(recs)
        assert h["cycles_total"] == 3
        assert h["cycles_active"] == 2  # d2 excluded
        # 1 of 2 active cycles merged
        assert h["cycle_success_rate"] == 0.5

    def test_selection_efficiency_proxy(self):
        recs = [_rec(f"d{i}", selected=10, merged=3) for i in range(5)]
        h = compute_health(recs)
        assert h["selection_efficiency"] == round(15 / 50, 3)  # 0.3

    def test_low_efficiency_flagged_only_with_enough_signal(self):
        # < 3 active cycles → no judgement (insufficient signal)
        few = [_rec("d1", selected=10, merged=0), _rec("d2", selected=10, merged=0)]
        assert compute_health(few)["flags"] == []
        # >= 3 active cycles with poor efficiency → flagged
        many = [_rec(f"d{i}", selected=10, merged=0) for i in range(4)]
        flags = compute_health(many)["flags"]
        assert any("LOW_SUCCESS" in f for f in flags)
        assert any("LOW_SELECTION_EFFICIENCY" in f for f in flags)

    def test_healthy_pipeline_no_flags(self):
        recs = [_rec(f"d{i}", selected=4, merged=3, rejected=1) for i in range(5)]
        h = compute_health(recs)
        assert h["flags"] == []
        assert "healthy" in format_health(h)

    def test_empty_history_no_crash(self):
        h = compute_health([])
        assert h["cycles_active"] == 0
        assert h["cycle_success_rate"] is None
        assert h["selection_efficiency"] is None
        assert "[evolution-metrics]" in format_health(h)
        assert "n/a" in format_health(h)

    def test_merged_trend(self):
        improving = [
            _rec(f"d{i}", selected=1, merged=m) for i, m in enumerate([0, 0, 3, 4])
        ]
        assert compute_health(improving)["merged_trend"] == "improving"
        declining = [
            _rec(f"d{i}", selected=1, merged=m) for i, m in enumerate([4, 3, 0, 0])
        ]
        assert compute_health(declining)["merged_trend"] == "declining"

    def test_window_last_n(self):
        recs = [_rec(f"d{i}", selected=1, merged=1) for i in range(40)]
        assert compute_health(recs, last=10)["cycles_total"] == 10

    def test_effort_budget_throttles_only_when_flagged(self):
        # Healthy window → default budget 3.0 (no throttle).
        healthy = [_rec(f"d{i}", selected=4, merged=3, rejected=1) for i in range(5)]
        assert compute_health(healthy)["effort_budget"] == 3.0
        # LOW_SELECTION_EFFICIENCY flagged → throttled budget 1.5, never a middle.
        starved = [_rec(f"d{i}", selected=10, merged=0) for i in range(4)]
        h = compute_health(starved)
        assert any("LOW_SELECTION_EFFICIENCY" in f for f in h["flags"])
        assert h["effort_budget"] == 1.5

    def test_effort_budget_default_when_insufficient_signal(self):
        # < 3 active cycles → no flags → default budget; never throttle blindly.
        few = [_rec("d1", selected=10, merged=0), _rec("d2", selected=10, merged=0)]
        assert compute_health(few)["effort_budget"] == 3.0

    def test_format_health_carries_budget_without_breaking_watchdog_tail(self):
        # The budget token must sit in the BODY, NOT the tail: evolution_watchdog
        # keys on `.endswith("| healthy")` and treats everything after the last
        # `|` as the flags. A budget in the tail would silence/garble the alert.
        healthy = format_health(
            compute_health([
                _rec(f"d{i}", selected=4, merged=3, rejected=1) for i in range(5)
            ])
        )
        assert "effort_budget=3.0" in healthy
        assert healthy.endswith("| healthy")
        flagged = format_health(
            compute_health([_rec(f"d{i}", selected=10, merged=0) for i in range(4)])
        )
        assert "effort_budget=1.5" in flagged
        assert not flagged.endswith("| healthy")
        assert "LOW_SELECTION_EFFICIENCY" in flagged

    def test_halt_state_visible_in_health(self, tmp_path):
        # When halt-state.txt exists, compute_health() should surface HALTED in
        # the flags and set halted=True so the health sidecar is self-describing.
        (tmp_path / "halt-state.txt").write_text("HALTED\n", encoding="utf-8")
        h = compute_health(
            [_rec(f"d{i}", selected=4, merged=3, rejected=1) for i in range(5)],
            evolution_dir=tmp_path,
        )
        assert h["halted"] is True
        assert any("HALTED" in f for f in h["flags"])
        formatted = format_health(h)
        assert "HALTED" in formatted
        assert "halt-state.txt" in formatted

    def test_halt_state_absent_when_no_file(self, tmp_path):
        h = compute_health(
            [_rec(f"d{i}", selected=4, merged=3, rejected=1) for i in range(5)],
            evolution_dir=tmp_path,
        )
        assert h["halted"] is False
        assert not any("HALTED" in f for f in h["flags"])
        assert "| healthy" in format_health(h)


class TestCohortSelectionEfficiency:
    """selection_efficiency is a COHORT ratio when the funnel emits issue-id
    lists: distinct selected issues that landed >=1 merge / distinct selected
    issues. Bounded 0..1, immune to increment-PR inflation and temporal skew.
    Falls back to the legacy merged/selected count ratio for pre-cohort records.
    """

    def _crec(self, date, selected_ids=None, merged_ids=None, rejected=0):
        r = {
            "date": date,
            "issues_created": 0,
            "selected": len(selected_ids or []),
            "merged": len(merged_ids or []),
            "rejected": rejected,
        }
        if selected_ids is not None:
            r["selected_issue_ids"] = list(selected_ids)
        if merged_ids is not None:
            r["merged_issue_ids"] = list(merged_ids)
        return r

    def test_cohort_ratio_from_issue_ids(self):
        # 4 distinct selected issues; 2 of them landed -> 0.5, NOT merged/selected.
        recs = [
            self._crec("d1", selected_ids=[1, 2], merged_ids=[2]),
            self._crec("d2", selected_ids=[3, 4], merged_ids=[4]),
        ]
        h = compute_health(recs)
        assert h["selection_efficiency"] == 0.5

    def test_increment_merges_do_not_exceed_100pct(self):
        # One selected issue, three increment merges recorded as one issue id.
        recs = [
            self._crec(f"d{i}", selected_ids=[798], merged_ids=[798]) for i in range(3)
        ]
        h = compute_health(recs)
        assert h["selection_efficiency"] == 1.0  # bounded, never >1

    def test_temporal_lag_issue_selected_then_merged_next_cycle(self):
        # Selected on d1 (no merge yet), merged on d2 -> counts as landed.
        recs = [
            self._crec("d1", selected_ids=[798], merged_ids=[]),
            self._crec("d2", selected_ids=[801], merged_ids=[798]),
        ]
        h = compute_health(recs)
        # both 798 and 801 selected; only 798 landed -> 0.5
        assert h["selection_efficiency"] == 0.5

    def test_legacy_records_without_ids_use_count_ratio(self):
        # No issue-id fields -> fall back to merged/selected counts (0.3).
        recs = [_rec(f"d{i}", selected=10, merged=3) for i in range(5)]
        h = compute_health(recs)
        assert h["selection_efficiency"] == round(15 / 50, 3)


class TestStageGateIntegration:
    """The gate rates ride the health line; the watchdog contract must hold.

    evolution_watchdog.check_health reads the WHOLE file and alerts unless it
    ends in "| healthy", so where the rates sit is a correctness property, not
    formatting taste. This had no coverage when the integration landed (#1340).
    """

    @staticmethod
    def _ledger(evolution_dir, *branches, stage="local_triage"):
        import json

        evolution_dir.mkdir(parents=True, exist_ok=True)
        with open(evolution_dir / "stage_gate.jsonl", "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(
                        {
                            "branch": b,
                            "stage": stage,
                            "confidence": 50,
                            "threshold": 70,
                            "reason": "test",
                            "retained_evidence": [],
                        }
                    )
                    + "\n" for b in branches)

    def test_healthy_gate_keeps_the_healthy_tail(self, tmp_path):
        d = tmp_path / "evolution"
        self._ledger(d, "refine", "refine", "accept", "accept")
        line = format_health(compute_health([], 30, d))
        assert line.endswith("| healthy"), line
        assert "gate[local_triage]" in line

    def test_mistuned_gate_flags_and_breaks_the_healthy_tail(self, tmp_path):
        d = tmp_path / "evolution"
        self._ledger(d, "restart", "restart", "restart", "accept")
        line = format_health(compute_health([], 30, d))
        assert not line.endswith("| healthy")
        assert "HIGH_STAGE_RESTART_RATE:local_triage" in line

    def test_no_ledger_leaves_the_line_untouched(self, tmp_path):
        d = tmp_path / "evolution"
        d.mkdir(parents=True, exist_ok=True)
        line = format_health(compute_health([], 30, d))
        assert "gate[" not in line
        assert line.endswith("| healthy")

    def test_non_string_stage_does_not_blind_the_feature(self, tmp_path):
        """A single bad-typed ledger line used to raise TypeError inside
        gate_flags, which compute_health swallowed — dropping the rates AND the
        alert while still reporting healthy."""
        import json

        d = tmp_path / "evolution"
        self._ledger(d, "restart", "restart", "restart", "accept")
        with open(d / "stage_gate.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"branch": "accept", "stage": 42}) + "\n")
        line = format_health(compute_health([], 30, d))
        assert "HIGH_STAGE_RESTART_RATE:local_triage" in line, line


class TestEvolutionDirectoryResolution:
    def test_health_reads_active_home(self, tmp_path, monkeypatch):
        home = tmp_path / "profile"
        evolution = home / "evolution"
        evolution.mkdir(parents=True)
        (evolution / "halt-state.txt").write_text("HALTED\n", encoding="utf-8")
        TestStageGateIntegration._ledger(
            evolution, "restart", "restart", "restart", "accept"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("EVOLUTION_PROFILE_DIR", raising=False)

        health = compute_health([])

        assert health["halted"] is True
        assert "HIGH_STAGE_RESTART_RATE:local_triage" in format_health(health)

    def test_cli_reads_evolution_override_before_active_home(
        self, tmp_path, monkeypatch, capsys
    ):
        import json

        home = tmp_path / "profile"
        evolution = home / "evolution"
        evolution.mkdir(parents=True)
        (evolution / "halt-state.txt").write_text("HALTED\n", encoding="utf-8")
        (evolution / "metrics.jsonl").write_text(
            json.dumps({**_rec("d1", selected=2, merged=1), "skipped": 0}) + "\n", encoding="utf-8"
        )
        override = tmp_path / "override"
        override.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("EVOLUTION_PROFILE_DIR", raising=False)

        assert main(["evolution_metrics.py"]) == 0
        default_line = capsys.readouterr().out
        assert "selected=2 merged=1" in default_line
        assert "HALTED" in default_line

        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(override))
        assert main(["evolution_metrics.py"]) == 0
        override_line = capsys.readouterr().out
        assert "selected=0 merged=0" in override_line
        assert override_line.rstrip().endswith("| healthy")
        assert compute_health([])["halted"] is False
        assert compute_health([], evolution_dir=evolution)["halted"] is True

    def test_installed_metrics_uses_explicit_home_without_checkout(
        self, tmp_path, monkeypatch
    ):
        import json
        import os
        import subprocess

        from scripts.register_evolution_cron import _install_evolution_helpers

        home = tmp_path / "profile"
        monkeypatch.setenv("HERMES_HOME", str(home))
        _install_evolution_helpers(Path(__file__).resolve().parents[2])
        evolution = home / "evolution"
        evolution.mkdir()
        (evolution / "halt-state.txt").write_text("HALTED\n", encoding="utf-8")
        (evolution / "metrics.jsonl").write_text(
            json.dumps({**_rec("d1", selected=2, merged=1), "skipped": 0}) + "\n",
            encoding="utf-8",
        )
        env = {
            "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
            "HERMES_HOME": str(home), "PATH": "", "TZ": "UTC", "LANG": "C.UTF-8",
        }
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

        def run():
            return subprocess.run(
                [sys.executable, "-E", "-S", "-B",
                 str(home / "scripts" / "evolution_metrics.py")],
                cwd=tmp_path, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=30, check=False,
            )

        result = run()
        assert result.returncode == 0, result.stderr
        assert "selected=2 merged=1" in result.stdout
        assert "HALTED" in result.stdout

        env["EVOLUTION_PROFILE_DIR"] = str(tmp_path / "override")
        result = run()
        assert result.returncode == 0, result.stderr
        assert "selected=0 merged=0" in result.stdout
        assert result.stdout.rstrip().endswith("| healthy")

        del env["HERMES_HOME"]
        assert run().returncode == 0  # Explicit evolution directory is sufficient.
        del env["EVOLUTION_PROFILE_DIR"]
        result = run()
        assert result.returncode != 0
        assert "requires HERMES_HOME or EVOLUTION_PROFILE_DIR" in result.stderr

        env["HERMES_HOME"] = " \t "
        result = run()
        assert result.returncode != 0
        assert "requires HERMES_HOME or EVOLUTION_PROFILE_DIR" in result.stderr

    @pytest.mark.parametrize("failure", ["nested_import", "canonical_runtime"])
    def test_canonical_failures_do_not_fall_back_to_environment(
        self, tmp_path, monkeypatch, failure
    ):
        import builtins

        import hermes_constants

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
        monkeypatch.delenv("EVOLUTION_PROFILE_DIR", raising=False)
        if failure == "nested_import":
            original_import = builtins.__import__
            error = ModuleNotFoundError("missing canonical dependency", name="canonical_dependency")

            def import_with_missing_dependency(name, *args, **kwargs):
                if name == "hermes_constants":
                    raise error
                return original_import(name, *args, **kwargs)

            monkeypatch.setattr(builtins, "__import__", import_with_missing_dependency)
        else:
            error = RuntimeError("canonical resolver failed")

            def failed_home():
                raise error

            monkeypatch.setattr(hermes_constants, "get_hermes_home", failed_home)

        with pytest.raises(type(error)) as raised:
            compute_health([])
        assert raised.value is error
