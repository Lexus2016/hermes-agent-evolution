"""Tests for duplicate API request dump suppression (issue #367)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import dump_api_request_debug
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _make_agent(tmp_path: Path) -> AIAgent:
    with (
        patch(
            "model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")
        ),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("run_agent.AIAgent._create_openai_client"),
    ):
        agent = AIAgent(
            api_key="test-key-12345678",
            base_url="https://my-llm.example.com/v1",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent.client.api_key = "sk-test"
        agent.logs_dir = tmp_path
        return agent


def test_duplicate_dump_suppressed_within_window(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    err = ValueError("boom")
    err.status_code = 401  # type: ignore[attr-defined]

    first = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert first is not None
    assert first.exists()

    second = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert second is None


def test_duplicate_dump_allowed_after_window(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    err = ValueError("boom")
    err.status_code = 401  # type: ignore[attr-defined]

    first = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert first is not None

    # Simulate cache entry aging out by poking the cache directly.
    cache = agent._request_dump_cache
    assert cache is not None
    key = next(iter(cache.keys()))
    cache[key] = time.time() - 61

    second = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert second is not None
    assert second.exists()


def test_different_failure_category_not_suppressed(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    err1 = ValueError("boom")
    err1.status_code = 401  # type: ignore[attr-defined]
    err2 = ValueError("boom2")
    err2.status_code = 429  # type: ignore[attr-defined]

    first = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err1)
    assert first is not None

    second = dump_api_request_debug(agent, api_kwargs, reason="rate_limit", error=err2)
    assert second is not None


def test_preflight_dump_not_suppressed_against_error_dump(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    pre = dump_api_request_debug(agent, api_kwargs, reason="preflight")
    assert pre is not None

    err = ValueError("boom")
    err.status_code = 401  # type: ignore[attr-defined]
    post = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert post is not None


def test_dump_payload_includes_failure_category(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    err = ValueError("paid subscription required")
    err.status_code = 403  # type: ignore[attr-defined]

    dump_file = dump_api_request_debug(agent, api_kwargs, reason="billing", error=err)
    assert dump_file is not None
    data = json.loads(dump_file.read_text())
    assert data["error"]["failure_category"] == "billing"
    assert data["error"]["retryable"] is False


# ── Issue #73: preflight dumps are deduped + capped; the cap counts writes ──


def _config_with_request_dump(**kwargs: object) -> dict:
    """A minimal config dict carrying a ``request_dump`` section."""
    return {"request_dump": kwargs}


def _dump_count(agent: AIAgent) -> int:
    return int(getattr(agent, "_request_dump_count", 0))


def test_preflight_duplicates_suppressed_within_window(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    first = dump_api_request_debug(agent, api_kwargs, reason="preflight")
    assert first is not None
    assert first.exists()

    second = dump_api_request_debug(agent, api_kwargs, reason="preflight")
    assert second is None


def test_suppressed_duplicates_do_not_burn_cap_budget(tmp_path: Path):
    """#73 — a suppressed duplicate must not consume the per-session cap.

    A burst of identical preflight attempts writes ONE dump; the suppressed
    duplicates leave the budget intact, so a genuinely new failure payload is
    still dumped instead of being silenced for the rest of the session.
    """
    agent = _make_agent(tmp_path)
    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value=_config_with_request_dump(
            dedup_window_seconds=60, max_dumps_per_session=2
        ),
    ):
        api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

        # Three identical attempts: only the first is a write.
        for _ in range(3):
            dump_api_request_debug(agent, api_kwargs, reason="preflight")
        assert len(list(agent.logs_dir.glob("request_dump_*.json"))) == 1
        assert _dump_count(agent) == 1

        # A genuinely new payload still gets dumped: the two suppressed
        # duplicates did NOT burn the budget.
        new_kwargs = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "different"}],
        }
        second = dump_api_request_debug(agent, new_kwargs, reason="preflight")
        assert second is not None
        assert _dump_count(agent) == 2

        # Cap (2) now reached: a third distinct payload is suppressed.
        third = dump_api_request_debug(
            agent,
            {"model": "gpt-4", "messages": [{"role": "user", "content": "another"}]},
            reason="preflight",
        )
        assert third is None
        assert _dump_count(agent) == 2


def test_config_driven_dedup_window(tmp_path: Path):
    """Changing request_dump.dedup_window_seconds changes suppression."""
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value=_config_with_request_dump(dedup_window_seconds=0),
    ):
        # A window of 0 disables dedup: identical preflight bodies all write.
        first = dump_api_request_debug(agent, api_kwargs, reason="preflight")
        second = dump_api_request_debug(agent, api_kwargs, reason="preflight")
        assert first is not None
        assert second is not None


def test_config_driven_cap(tmp_path: Path):
    """Changing request_dump.max_dumps_per_session changes the cap."""
    agent = _make_agent(tmp_path)
    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value=_config_with_request_dump(
            dedup_window_seconds=0, max_dumps_per_session=0
        ),
    ):
        # A cap of 0 disables the cap entirely.
        for content in ("a", "b", "c", "d", "e"):
            result = dump_api_request_debug(
                agent,
                {"model": "gpt-4", "messages": [{"role": "user", "content": content}]},
                reason="preflight",
            )
            assert result is not None
        assert len(list(agent.logs_dir.glob("request_dump_*.json"))) == 5


def test_non_int_config_values_fall_back_to_defaults(tmp_path: Path):
    """Non-int request_dump values must not crash the dump path."""
    agent = _make_agent(tmp_path)
    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value=_config_with_request_dump(
            dedup_window_seconds="soon", max_dumps_per_session="lots"
        ),
    ):
        api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

        first = dump_api_request_debug(agent, api_kwargs, reason="preflight")
        assert first is not None

        # Default window (60s) still suppresses the identical follow-up.
        second = dump_api_request_debug(agent, api_kwargs, reason="preflight")
        assert second is None

        # The error path still dumps despite the junk config values.
        err = ValueError("boom")
        err.status_code = 500  # type: ignore[attr-defined]
        error_dump = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
        assert error_dump is not None
        assert error_dump.exists()


def test_error_path_not_capped(tmp_path: Path):
    """#73 — max_dumps_per_session must not silence error dumps.

    Once the preflight cap is exhausted, the error path still writes its
    dump: post-mortem debugging must always get its snapshot.
    """
    agent = _make_agent(tmp_path)
    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value=_config_with_request_dump(
            dedup_window_seconds=60, max_dumps_per_session=1
        ),
    ):
        api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

        first = dump_api_request_debug(agent, api_kwargs, reason="preflight")
        assert first is not None

        # Cap of 1 reached: a second, distinct preflight dump is suppressed.
        suppressed = dump_api_request_debug(
            agent,
            {"model": "gpt-4", "messages": [{"role": "user", "content": "new"}]},
            reason="preflight",
        )
        assert suppressed is None

        # ...but the error path still dumps after the cap is exhausted.
        err = ValueError("boom")
        err.status_code = 500  # type: ignore[attr-defined]
        error_dump = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
        assert error_dump is not None
        assert error_dump.exists()
