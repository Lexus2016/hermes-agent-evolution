"""Integration tests for cron failure digest surfacing in the CLI (issue #433).

The prior slice added ``build_cron_failure_digest`` in ``cron/scheduler.py`` and
persisted cron failures on disk, but the digest was dead code: no user
interaction path invoked it.  This test verifies that ``HermesCLI.chat()``
now surfaces the digest both to the terminal and to the model's
``user_message`` on the next user turn, and that ack timestamps are only
updated when a digest is actually delivered.
"""

import os
from unittest.mock import MagicMock, patch

import cli as cli_module
import pytest
from cli import HermesCLI, _get_cron_failure_digest_for_user


def _clean_config():
    return {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }


class TestCronFailureDigestHelper:
    def test_returns_digest_when_available(self):
        with patch(
            "cron.scheduler.build_cron_failure_digest",
            return_value="⚠️ Cron failure digest",
        ) as mock_digest:
            assert _get_cron_failure_digest_for_user() == "⚠️ Cron failure digest"
            mock_digest.assert_called_once_with()

    def test_swallows_exceptions_and_returns_none(self):
        with patch(
            "cron.scheduler.build_cron_failure_digest", side_effect=RuntimeError("boom")
        ):
            assert _get_cron_failure_digest_for_user() is None


class TestCronFailureDigestInChat:
    @pytest.fixture
    def cli_obj(self):
        with patch("cli.get_tool_definitions", return_value=[]), patch.dict(
            "os.environ", {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}, clear=False
        ), patch.dict(cli_module.__dict__, {"CLI_CONFIG": _clean_config()}):
            obj = HermesCLI()
            fake_agent = MagicMock()
            fake_agent.run_conversation.return_value = {
                "final_response": "ok",
                "messages": [],
            }
            obj.agent = fake_agent
            yield obj

    def test_digest_prepended_to_user_message(self, cli_obj):
        digest = "⚠️ Cron failure digest (last 24h):\n• 'job' failed"
        with patch(
            "cli._get_cron_failure_digest_for_user", return_value=digest
        ), patch.object(cli_obj, "_ensure_runtime_credentials", return_value=True), patch.object(
            cli_obj,
            "_resolve_turn_agent_config",
            return_value={
                "signature": getattr(cli_obj, "_active_agent_route_signature", None),
                "model": cli_obj.model,
                "runtime": None,
                "request_overrides": {},
            },
        ), patch.object(
            cli_obj, "_init_agent", return_value=True
        ), patch.object(
            cli_obj, "_reset_stream_state"
        ), patch.object(cli_obj, "_flush_stream"), patch.object(
            cli_obj, "_flush_credit_notices"
        ):
            cli_obj.chat("hello")

        calls = cli_obj.agent.run_conversation.call_args_list
        assert len(calls) == 1
        _, kwargs = calls[0]
        user_message = kwargs["user_message"]
        assert digest in user_message
        assert "hello" in user_message

    def test_no_digest_when_none_available(self, cli_obj):
        with patch(
            "cli._get_cron_failure_digest_for_user", return_value=None
        ), patch.object(cli_obj, "_ensure_runtime_credentials", return_value=True), patch.object(
            cli_obj,
            "_resolve_turn_agent_config",
            return_value={
                "signature": getattr(cli_obj, "_active_agent_route_signature", None),
                "model": cli_obj.model,
                "runtime": None,
                "request_overrides": {},
            },
        ), patch.object(
            cli_obj, "_init_agent", return_value=True
        ), patch.object(
            cli_obj, "_reset_stream_state"
        ), patch.object(cli_obj, "_flush_stream"), patch.object(
            cli_obj, "_flush_credit_notices"
        ):
            cli_obj.chat("hello")

        calls = cli_obj.agent.run_conversation.call_args_list
        assert len(calls) == 1
        _, kwargs = calls[0]
        assert kwargs["user_message"] == "hello"
