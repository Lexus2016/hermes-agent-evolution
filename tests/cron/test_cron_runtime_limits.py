"""Cron runtime limits: ``cron.inactivity_timeout_seconds`` / ``cron.delegation_drain_seconds``.

Both keys shipped in ``config_defaults`` with comments describing #1200 but nothing read them:
the watchdog only parsed ``HERMES_CRON_TIMEOUT`` (falling back to a hardcoded 600) and
``wait_for_session_delegations`` had no caller. These tests pin the wiring: env over config
resolution, the 900/1200 defaults, 0 semantics, and the drain barrier in the watchdog's teardown.
"""

from __future__ import annotations

import textwrap

import pytest

import cron.env_settings as env_settings
import cron.jobs as jobs
import cron.scheduler as scheduler


def _write_config(home, body: str) -> None:
    (home / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture()
def cron_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no ``.env`` and a config.yaml the test owns."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "_hermes_home", None, raising=False)
    for name in ("HERMES_CRON_TIMEOUT", "HERMES_CRON_DELEGATION_DRAIN_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_inactivity_timeout_comes_from_config(cron_home):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 42\n")
    assert scheduler._cron_inactivity_seconds() == 42.0


def test_env_overrides_inactivity_timeout(cron_home, monkeypatch):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 42\n")
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "7")
    assert scheduler._cron_inactivity_seconds() == 7.0


def test_invalid_env_falls_back_to_config(cron_home, monkeypatch):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 42\n")
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "not-a-number")
    assert scheduler._cron_inactivity_seconds() == 42.0


def test_invalid_config_uses_default(cron_home):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: not-a-number\n")
    assert scheduler._cron_inactivity_seconds() == 900.0


def test_missing_config_uses_default(cron_home):
    assert scheduler._cron_inactivity_seconds() == 900.0


def test_zero_inactivity_is_unlimited(cron_home):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 0\n")
    assert scheduler._cron_inactivity_seconds() == 0.0


def test_delegation_drain_comes_from_config(cron_home):
    _write_config(cron_home, "cron:\n  delegation_drain_seconds: 33\n")
    assert scheduler._cron_delegation_drain_seconds() == 33.0


def test_delegation_drain_env_overrides_config(cron_home, monkeypatch):
    _write_config(cron_home, "cron:\n  delegation_drain_seconds: 33\n")
    monkeypatch.setenv("HERMES_CRON_DELEGATION_DRAIN_SECONDS", "9")
    assert scheduler._cron_delegation_drain_seconds() == 9.0


def test_oneshot_claim_ttl_follows_config_inactivity(cron_home):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 1200\n")
    # max(1200 * 3, 1800) — the claim must not expire while the run is inside its budget.
    assert jobs._oneshot_run_claim_ttl_seconds() == 3600.0


def test_oneshot_claim_ttl_keeps_floor_for_unlimited(cron_home):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 0\n")
    assert jobs._oneshot_run_claim_ttl_seconds() == 1800.0


class _FakeAgent:
    def run_conversation(self, prompt, task_id=None):
        return {"final_response": "ok", "messages": []}

    def get_activity_summary(self):
        return {"seconds_since_activity": 0.0, "last_activity_desc": "idle",
                "api_call_count": 0, "max_iterations": 1, "current_tool": None}

    def interrupt(self, reason):
        return None


def _run_watchdog(job_name="job one"):
    job = {"id": "j1", "name": job_name, "prompt": "hello"}
    return scheduler._run_agent_with_watchdog(
        _FakeAgent(), "hello", job, "j1", job_name, "task-1", None,
        cron_session_id="cron_j1_20260101_000000")


def test_watchdog_drains_background_delegations(cron_home, monkeypatch):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 0\n"
                             "  delegation_drain_seconds: 5\n")
    calls = []

    def _fake_wait(session_id, deadline_seconds=1200.0):
        calls.append((session_id, deadline_seconds))
        return {"waited": 0, "completed": 0, "interrupted": 0, "timed_out": False}

    monkeypatch.setattr("tools.async_delegation.wait_for_session_delegations", _fake_wait)
    assert _run_watchdog()["final_response"] == "ok"
    assert calls == [("cron_j1_20260101_000000", 5.0)]


def test_watchdog_skips_drain_when_disabled(cron_home, monkeypatch):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 0\n"
                             "  delegation_drain_seconds: 0\n")
    monkeypatch.setattr(
        "tools.async_delegation.wait_for_session_delegations",
        lambda *a, **k: pytest.fail("drain must not run when disabled"))
    assert _run_watchdog()["final_response"] == "ok"


def test_watchdog_survives_drain_failure(cron_home, monkeypatch):
    _write_config(cron_home, "cron:\n  inactivity_timeout_seconds: 0\n"
                             "  delegation_drain_seconds: 5\n")

    def _boom(*args, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr("tools.async_delegation.wait_for_session_delegations", _boom)
    assert _run_watchdog()["final_response"] == "ok"


def test_env_settings_helper_passes_through_unlimited_values(cron_home):
    """<=0 and non-finite values stay parseable: callers read them as unlimited, and the stale
    recovery fails closed on a non-finite bound instead of inventing a default."""
    helper = env_settings.cron_seconds_setting
    assert helper("HERMES_CRON_TIMEOUT", "inactivity_timeout_seconds", 900.0) == 900.0
    (cron_home / "config.yaml").write_text("cron:\n  inactivity_timeout_seconds: -5\n", encoding="utf-8")
    assert helper("HERMES_CRON_TIMEOUT", "inactivity_timeout_seconds", 900.0) == -5.0
