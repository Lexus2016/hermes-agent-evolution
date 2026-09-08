"""Opt-in warn-and-repin for the cron model-drift guard (cron.model_drift_auto_repin).

Field report (2026-08-23): a global provider switch (bedrock→nous) mass-skipped
13 unpinned tc-* cron jobs in a single scheduler pass. The #44585 fail-closed
guard is the right default for interactive installs, but headless fleets want
unpinned jobs to track the new baseline and keep running.

Contract:
- Default (option missing) → guard behaves exactly as before: skip + alert once.
- Literal ``true`` → the drifted job is re-pinned to the new baseline (snapshots
  refreshed on disk, drift alert bit cleared) and RUNS this tick.
- Malformed values (e.g. the string "yes") stay OFF — strict opt-in.
- Repin failure (exception or unresolvable axis) → falls back to the skip path;
  a broken repin can never silently enable spend.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as cron_jobs
import cron.scheduler as sched


def _job(**overrides):
    job = {
        "id": "auto-repin-test",
        "name": "auto repin test",
        "prompt": "hello",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "deliver": "local",
        "model": None,
        "provider": None,
        "provider_snapshot": "openrouter",
        "base_url": None,
    }
    job.update(overrides)
    return job


def _write_config(tmp_path, auto_repin):
    """Write a config.yaml enabling/disabling cron.model_drift_auto_repin."""
    lines = ["model:", "  default: test-model"]
    if auto_repin is not None:
        lines += ["cron:", f"  model_drift_auto_repin: {auto_repin}"]
    (tmp_path / "config.yaml").write_text("\n".join(lines) + "\n")


def _tick(job, tmp_path, current_provider, deliveries):
    """Run one run_one_job tick with the provider resolution pinned."""
    fake_db = MagicMock()

    def fake_deliver(job, content, adapters=None, loop=None, **kwargs):
        deliveries.append(content)
        return None

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={
                   "api_key": "test-key",
                   "base_url": "https://example.invalid/v1",
                   "provider": current_provider,
                   "api_mode": "chat_completions",
               }), \
         patch("cron.jobs._compute_provider_model_snapshots",
               return_value=(current_provider, "test-model")), \
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent
        ok = sched.run_one_job(job)
    return ok, mock_agent_cls.called


def _stored(tmp_path, job_id="auto-repin-test"):
    return [j for j in cron_jobs.load_jobs() if j["id"] == job_id][0]


class TestDriftAutoRepin:
    def test_default_off_preserves_fail_closed_skip(self, tmp_path):
        """No opt-in → the #44585 skip + alert-once behavior is untouched."""
        _write_config(tmp_path, None)
        job = _job()
        deliveries = []
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            fresh = _stored(tmp_path)
            ok, agent_called = _tick(fresh, tmp_path, "nous", deliveries)
            assert agent_called is False, "default must not spend on drift"

            stored = _stored(tmp_path)
            assert stored.get("drift_alerted") is True
        assert len(deliveries) == 1
        assert "drift" in deliveries[0].lower()

    def test_enabled_repins_and_runs(self, tmp_path):
        """Literal true → drifted job re-pins to the new baseline and runs."""
        _write_config(tmp_path, "true")
        job = _job()
        deliveries = []
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            fresh = _stored(tmp_path)
            ok, agent_called = _tick(fresh, tmp_path, "nous", deliveries)
            assert agent_called is True, "opted-in drifted job must run"

            stored = _stored(tmp_path)
            assert stored.get("provider_snapshot") == "nous", (
                "snapshots must be refreshed to the new baseline"
            )
            assert not stored.get("drift_alerted"), "repin clears the alert bit"

    def test_malformed_value_stays_off(self, tmp_path):
        """Only the literal YAML boolean true opts in."""
        _write_config(tmp_path, '"yes"')
        job = _job()
        deliveries = []
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            fresh = _stored(tmp_path)
            ok, agent_called = _tick(fresh, tmp_path, "nous", deliveries)
            assert agent_called is False, "malformed value must stay fail-closed"
            assert _stored(tmp_path).get("drift_alerted") is True

    def test_repin_failure_falls_back_to_skip(self, tmp_path):
        """A repin exception must fall back to the default skip, never run."""
        _write_config(tmp_path, "true")
        job = _job()
        deliveries = []
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            fresh = _stored(tmp_path)
            with patch("cron.jobs.repin_jobs", side_effect=RuntimeError("lock lost")):
                ok, agent_called = _tick(fresh, tmp_path, "nous", deliveries)
            assert agent_called is False, "failed repin must not spend"

            stored = _stored(tmp_path)
            assert stored.get("drift_alerted") is True, (
                "skip path must re-arm the alert"
            )

    def test_flag_helper_semantics(self):
        assert sched._cron_drift_auto_repin_enabled({}) is False
        assert sched._cron_drift_auto_repin_enabled({"cron": {}}) is False
        assert sched._cron_drift_auto_repin_enabled(
            {"cron": {"model_drift_auto_repin": True}}
        ) is True
        assert sched._cron_drift_auto_repin_enabled(
            {"cron": {"model_drift_auto_repin": "true"}}
        ) is False
        assert sched._cron_drift_auto_repin_enabled(
            {"cron": {"model_drift_auto_repin": False}}
        ) is False
        assert sched._cron_drift_auto_repin_enabled({"cron": "garbage"}) is False
        assert sched._cron_drift_auto_repin_enabled(None) is False
