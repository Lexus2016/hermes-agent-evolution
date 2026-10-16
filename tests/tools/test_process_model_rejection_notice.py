"""Model rejection diagnostics for single and batch delegations."""
import pytest

from tools.process_registry_notifications import format_process_notification


@pytest.fixture(autouse=True)
def _reset_systemd_scope_cache():
    """Reset the cached ``systemd-run --user --scope`` availability flag
    before each test so a probe run on a real systemd host (where
    ``INVOCATION_ID`` is set) doesn't leak into tests that mock
    ``subprocess.Popen``. Tests that exercise the probe directly reset the
    cache themselves."""
    import tools.process_registry as _pr

    original = _pr._SYSTEMD_SCOPE_AVAILABLE
    _pr._SYSTEMD_SCOPE_AVAILABLE = False
    yield
    _pr._SYSTEMD_SCOPE_AVAILABLE = original



def _make_delegation_batch_evt(results):
    """A batch async-delegation event carrying a per-task ``results`` list."""
    return {
        "type": "async_delegation",
        "delegation_id": "deleg_97654",
        "is_batch": True,
        "results": results,
        "goals": [r.get("goal") or "" for r in results],
        "session_key": "agent:main:cli:dm:local",
        "status": "completed",
        "model": "upstage/solar-pro-4",
    }


def _patch_delegation_config(
    monkeypatch, model="upstage/solar-pro-4", provider="openrouter", **over
):
    import tools.process_registry_notifications as _prn

    cfg = {"model": model, "provider": provider}
    cfg.update(over)
    monkeypatch.setattr(_prn, "_delegation_config", lambda: cfg)
    return cfg


def _format_async(evt) -> str:

    text = format_process_notification(evt)
    assert text is not None, "format_process_notification returned None"
    return text


def test_model_not_found_notice_single_failure_once(monkeypatch):
    evt = _make_delegation_batch_evt([
        {
            "task_index": 0,
            "status": "failed",
            "exit_reason": "error",
            "goal": "Create bridge module",
            "error": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
            "summary": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
        }
    ])
    _patch_delegation_config(monkeypatch)
    text = _format_async(evt)
    assert text is not None
    assert text.count("SUBAGENT MODEL REJECTED") == 1
    assert "upstage/solar-pro-4" in text
    assert "openrouter" in text
    assert "No fallback chain is configured" in text


def test_model_not_found_notice_mixed_batch_named_model(monkeypatch):
    evt = _make_delegation_batch_evt([
        {
            "task_index": 0,
            "status": "failed",
            "exit_reason": "error",
            "goal": "A",
            "error": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
            "summary": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
        },
        {
            "task_index": 1,
            "status": "completed",
            "goal": "B",
            "summary": "ok",
            "api_calls": 3,
        },
    ])
    _patch_delegation_config(monkeypatch)
    text = _format_async(evt)
    assert text.count("SUBAGENT MODEL REJECTED") == 1
    assert "upstage/solar-pro-4" in text


def test_model_not_found_notice_absent_for_non_model_errors(monkeypatch):
    evt = _make_delegation_batch_evt([
        {
            "task_index": 0,
            "status": "failed",
            "goal": "A",
            "error": "HTTP 429: rate limit exceeded",
        },
        {
            "task_index": 1,
            "status": "failed",
            "goal": "B",
            "error": "Connection timed out",
        },
    ])
    _patch_delegation_config(monkeypatch)
    text = _format_async(evt)
    assert "SUBAGENT MODEL REJECTED" not in text


def test_model_not_found_notice_absent_when_configured_model_not_named(monkeypatch):
    evt = _make_delegation_batch_evt([
        {
            "task_index": 0,
            "status": "failed",
            "goal": "A",
            "error": "HTTP 400: gpt-99 is not a valid model ID",
        }
    ])
    # Configured model is upstage/solar-pro-4; the rejection names gpt-99.
    _patch_delegation_config(monkeypatch)
    text = _format_async(evt)
    assert "SUBAGENT MODEL REJECTED" not in text


def test_model_not_found_notice_single_dispatch(monkeypatch):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_single",
        "session_key": "agent:main:cli:dm:local",
        "goal": "task A",
        "model": "upstage/solar-pro-4",
        "status": "failed",
        "error": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
        "summary": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
    }
    _patch_delegation_config(monkeypatch)
    text = _format_async(evt)
    assert text.count("SUBAGENT MODEL REJECTED") == 1
    assert "upstage/solar-pro-4" in text


def test_model_not_found_notice_absent_when_fallback_chain_configured(monkeypatch):
    evt = _make_delegation_batch_evt([
        {
            "task_index": 0,
            "status": "failed",
            "goal": "A",
            "error": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
        }
    ])
    _patch_delegation_config(
        monkeypatch,
        fallback_providers=[{"provider": "openrouter", "model": "upstage/solar-pro4"}],
    )
    text = _format_async(evt)
    assert text.count("SUBAGENT MODEL REJECTED") == 1
    assert "No fallback chain is configured" not in text
