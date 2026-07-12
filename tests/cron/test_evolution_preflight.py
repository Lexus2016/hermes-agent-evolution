"""Tests for cron/evolution_preflight.py."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cron import evolution_preflight as ep


class TestEvolutionJobStage:
    def test_name_introspection(self):
        assert (
            ep.evolution_job_stage({"name": "evolution-introspection"})
            == "introspection"
        )

    def test_name_analysis(self):
        assert ep.evolution_job_stage({"name": "evolution-analysis"}) == "analysis"

    def test_tags_when_name_generic(self):
        assert (
            ep.evolution_job_stage({"name": "evolution", "tags": ["analysis"]})
            == "analysis"
        )

    def test_non_evolution_returns_none(self):
        assert ep.evolution_job_stage({"name": "morning-digest"}) is None

    def test_id_fallback(self):
        assert (
            ep.evolution_job_stage({"id": "evolution-implementation", "name": ""})
            == "implementation"
        )


class TestHaltGate:
    """Halt-state gate (#913): expensive LLM stages must skip when the
    evolution pipeline is structurally halted (scripts/evolution_funnel.py's
    halt-state.txt)."""

    def test_no_halt_file_not_skipped(self, tmp_path):
        (tmp_path / "evolution").mkdir()
        assert ep.should_skip_for_halt("research", tmp_path) is False

    def test_halt_file_present_gated_stage_skipped(self, tmp_path):
        evo_dir = tmp_path / "evolution"
        evo_dir.mkdir()
        (evo_dir / "halt-state.txt").write_text("halted")
        for stage in ("research", "analysis", "implementation", "introspection"):
            assert ep.should_skip_for_halt(stage, tmp_path) is True

    def test_halt_file_present_funnel_not_gated(self, tmp_path):
        evo_dir = tmp_path / "evolution"
        evo_dir.mkdir()
        (evo_dir / "halt-state.txt").write_text("halted")
        assert ep.should_skip_for_halt("funnel", tmp_path) is False

    def test_halt_file_present_integration_not_gated(self, tmp_path):
        evo_dir = tmp_path / "evolution"
        evo_dir.mkdir()
        (evo_dir / "halt-state.txt").write_text("halted")
        assert ep.should_skip_for_halt("integration", tmp_path) is False

    def test_none_stage_never_skipped(self, tmp_path):
        evo_dir = tmp_path / "evolution"
        evo_dir.mkdir()
        (evo_dir / "halt-state.txt").write_text("halted")
        assert ep.should_skip_for_halt(None, tmp_path) is False

    def test_missing_evolution_dir_not_halted(self, tmp_path):
        # No evolution/ dir at all yet — must not raise, must not skip.
        assert ep.should_skip_for_halt("research", tmp_path) is False

    def test_halt_check_fail_safe_on_error(self, tmp_path):
        # Any error resolving/reading the halt file must be treated as
        # NOT halted — a broken check must never wrongly skip a job.
        with patch.object(ep, "_evolution_dir", side_effect=OSError("boom")):
            assert ep._halt_state_active(tmp_path) is False
        with patch.object(ep, "_evolution_dir", side_effect=RuntimeError("boom")):
            assert ep._halt_state_active(tmp_path) is False

    def test_evolution_profile_dir_checked_first(self, tmp_path, monkeypatch):
        # scripts/evolution_funnel.py (the writer) resolves its directory via
        # EVOLUTION_PROFILE_DIR when set, independently of HERMES_HOME. The
        # gate must check that exact location too, or a custom-profile
        # deployment's halt file would never be seen (#913 follow-up).
        profile_dir = tmp_path / "custom-profile"
        profile_dir.mkdir()
        (profile_dir / "halt-state.txt").write_text("halted")
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(profile_dir))

        # hermes_home points somewhere else entirely and has no halt file —
        # the EVOLUTION_PROFILE_DIR match must still win.
        other_home = tmp_path / "other-hermes-home"
        (other_home / "evolution").mkdir(parents=True)
        assert ep.should_skip_for_halt("research", other_home) is True

    def test_evolution_profile_dir_unset_falls_back_to_hermes_home(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("EVOLUTION_PROFILE_DIR", raising=False)
        evo_dir = tmp_path / "evolution"
        evo_dir.mkdir()
        (evo_dir / "halt-state.txt").write_text("halted")
        assert ep.should_skip_for_halt("research", tmp_path) is True


class TestPreflightConfig:
    def test_preflight_timeout_default(self):
        with patch("hermes_cli.config.load_config_readonly", return_value={}):
            assert ep._preflight_timeout_seconds() == 30.0

    def test_preflight_timeout_from_config(self):
        cfg = {"cron": {"preflight_timeout_seconds": 10}}
        assert ep._preflight_timeout_seconds(cfg) == 10.0

    def test_preflight_timeout_invalid_falls_back(self):
        cfg = {"cron": {"preflight_timeout_seconds": "bad"}}
        assert ep._preflight_timeout_seconds(cfg) == 30.0

    def test_preflight_enabled_default(self):
        assert ep._preflight_enabled({}) is True

    def test_preflight_enabled_can_disable(self):
        assert ep._preflight_enabled({"cron": {"preflight_enabled": False}}) is False
        assert ep._preflight_enabled({"cron": {"preflight_enabled": "no"}}) is False
        assert ep._preflight_enabled({"cron": {"preflight_enabled": "0"}}) is False


class TestDigestFallback:
    def test_find_latest_digest(self, tmp_path):
        stage_dir = tmp_path / "evolution" / "introspection"
        stage_dir.mkdir(parents=True)
        old = stage_dir / "2026-06-20.json"
        new = stage_dir / "2026-06-23.json"
        old.write_text("old")
        new.write_text("new")
        old.touch()
        new.touch()
        assert ep.find_latest_digest("introspection", tmp_path) == new

    def test_load_digest_as_fallback(self, tmp_path):
        stage_dir = tmp_path / "evolution" / "analysis"
        stage_dir.mkdir(parents=True)
        digest = stage_dir / "2026-06-23.json"
        digest.write_text(json.dumps({"foo": "bar"}))
        text = ep.load_digest_as_fallback("analysis", tmp_path)
        assert text is not None
        assert "Provider unreachable" in text
        assert "2026-06-23.json" in text
        assert '"foo": "bar"' in text

    def test_load_digest_truncate(self, tmp_path):
        stage_dir = tmp_path / "evolution" / "implementation"
        stage_dir.mkdir(parents=True)
        digest = stage_dir / "2026-06-23.md"
        digest.write_text("x" * 300_000)
        text = ep.load_digest_as_fallback("implementation", tmp_path, max_chars=100)
        assert text is not None
        assert text.endswith("[truncated: stale digest exceeded size limit]")

    def test_missing_digest_returns_none(self, tmp_path):
        assert ep.find_latest_digest("research", tmp_path) is None
        assert ep.load_digest_as_fallback("research", tmp_path) is None


class TestPreflightProvider:
    def test_missing_api_key(self):
        assert (
            ep.preflight_provider({})
            == "no API key or ACP command available for pre-flight ping"
        )

    def test_missing_model(self):
        assert (
            ep.preflight_provider({"api_key": "k"})
            == "no model configured for pre-flight ping"
        )

    def test_resolved_model_does_not_bail_no_model(self):
        # ROOT-FIX guard (#486): once the scheduler syncs the resolved model
        # into runtime["model"], the ping must proceed past the "no model"
        # short-circuit. We patch the OpenAI client so no network call is made;
        # the assertion is that the empty-model branch is NOT taken and the
        # provider client is actually invoked with the resolved model.
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock()
        with patch("openai.OpenAI", return_value=fake_client):
            err = ep.preflight_provider(
                {
                    "api_key": "k",
                    "model": "config-default-model",
                    "provider": "openrouter",
                }
            )
        assert err is None
        # The model carried on the runtime dict must reach the client call.
        _, kwargs = fake_client.chat.completions.create.call_args
        assert kwargs["model"] == "config-default-model"

    def test_acp_treated_as_reachable(self):
        assert (
            ep.preflight_provider(
                {
                    "api_key": "k",
                    "model": "m",
                    "command": ["copilot"],
                }
            )
            is None
        )

    def test_openai_success(self):
        fake_client = MagicMock()
        fake_response = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response
        with patch("openai.OpenAI", return_value=fake_client):
            assert (
                ep.preflight_provider(
                    {
                        "api_key": "k",
                        "model": "m",
                        "provider": "openrouter",
                    }
                )
                is None
            )
        fake_client.chat.completions.create.assert_called_once()

    def test_openai_failure(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = RuntimeError(
            "connection refused"
        )
        with patch("openai.OpenAI", return_value=fake_client):
            err = ep.preflight_provider(
                {
                    "api_key": "k",
                    "model": "m",
                    "provider": "openrouter",
                }
            )
        assert err is not None
        assert "connection refused" in err

    def test_anthropic_success(self):
        pytest.importorskip("anthropic")
        fake_client = MagicMock()
        with patch("anthropic.Anthropic", return_value=fake_client):
            assert (
                ep.preflight_provider(
                    {
                        "api_key": "k",
                        "model": "m",
                        "api_mode": "anthropic_messages",
                    }
                )
                is None
            )
        fake_client.messages.create.assert_called_once()

    def test_anthropic_failure(self):
        pytest.importorskip("anthropic")
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("timeout")
        with patch("anthropic.Anthropic", return_value=fake_client):
            err = ep.preflight_provider(
                {
                    "api_key": "k",
                    "model": "m",
                    "api_mode": "anthropic_messages",
                }
            )
        assert err is not None
        assert "timeout" in err


class TestSchedulerIntegration:
    def _make_job(self, stage="introspection"):
        return {
            "id": f"evolution-{stage}",
            "name": f"evolution-{stage}",
            "prompt": "do work",
        }

    def _patch_runtime(self, tmp_path):
        return patch(
            "cron.scheduler._get_hermes_home",
            return_value=tmp_path,
        ), patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "model": "openrouter/model",
            },
        )

    def test_preflight_success_continues_to_agent(self, tmp_path):
        from cron.scheduler import _run_job_impl

        job = self._make_job("analysis")
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                    "model": "openrouter/model",
                },
            ),
            patch("cron.evolution_preflight.preflight_provider", return_value=None),
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, output, final_response, error = _run_job_impl(job)

        assert success is True
        assert final_response == "ok"
        mock_agent_cls.assert_called_once()

    def test_preflight_failure_with_digest_returns_stale_digest(self, tmp_path):
        from cron.scheduler import _run_job_impl

        stage_dir = tmp_path / "evolution" / "analysis"
        stage_dir.mkdir(parents=True)
        digest = stage_dir / "2026-06-23.json"
        digest.write_text(json.dumps({"selected": ["#123"]}))

        job = self._make_job("analysis")
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                    "model": "openrouter/model",
                },
            ),
            patch(
                "cron.evolution_preflight.preflight_provider",
                return_value="provider down",
            ),
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            success, output, final_response, error = _run_job_impl(job)

        assert success is True
        assert final_response == "[SILENT]"
        assert error is None
        assert "provider unreachable — stale digest fallback" in output
        assert '"selected": ["#123"]' in output
        mock_agent_cls.assert_not_called()

    def test_preflight_failure_without_digest_fails_job(self, tmp_path):
        from cron.scheduler import _run_job_impl

        job = self._make_job("research")
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                    "model": "openrouter/model",
                },
            ),
            patch(
                "cron.evolution_preflight.preflight_provider",
                return_value="provider down",
            ),
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            success, output, final_response, error = _run_job_impl(job)

        assert success is False
        assert error is not None and "No cached digest available" in error
        mock_agent_cls.assert_not_called()

    def test_non_evolution_job_skips_preflight(self, tmp_path):
        from cron.scheduler import _run_job_impl

        job = {"id": "morning-digest", "name": "morning-digest", "prompt": "hi"}
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                    "model": "openrouter/model",
                },
            ),
            patch(
                "cron.evolution_preflight.preflight_provider",
                return_value="provider down",
            ) as mock_preflight,
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _output, final_response, _error = _run_job_impl(job)

        assert success is True
        assert final_response == "ok"
        mock_preflight.assert_not_called()
        mock_agent_cls.assert_called_once()

    def test_root_fix_runtime_model_synced_from_config_default(self, tmp_path):
        """ROOT-FIX (#486): scheduler must sync the resolved model into
        runtime["model"] before the pre-flight ping.

        Reproduces the prod failure: resolve_runtime_provider() returns a
        runtime WITHOUT a ``model`` key (it never sets one — the scheduler
        resolves the model into a separate local variable and passes it to
        AIAgent(model=...) directly). The job pins no model, but config.yaml
        supplies model.default. Before the fix, preflight_provider() saw an
        empty runtime["model"] and always returned "no model configured for
        pre-flight ping". After the fix, runtime["model"] carries the resolved
        config default.

        We capture the runtime dict actually handed to preflight_provider and
        assert it carries the config default model.
        """
        from cron.scheduler import _run_job_impl

        # config.yaml provides the default model; job pins nothing.
        (tmp_path / "config.yaml").write_text("model:\n  default: cfg-default-model\n")

        captured = {}

        def _capture_preflight(runtime, *, cfg=None):
            # Snapshot what the scheduler passed in at call time.
            captured["model"] = runtime.get("model")
            captured["provider"] = runtime.get("provider")
            return None  # report provider reachable -> continue to agent

        job = self._make_job("analysis")
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                # NOTE: deliberately NO "model" key — mirrors prod behavior.
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
            ),
            patch(
                "cron.evolution_preflight.preflight_provider",
                side_effect=_capture_preflight,
            ),
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _output, final_response, _error = _run_job_impl(job)

        # The runtime handed to the ping must carry the config-default model,
        # not the empty value resolve_runtime_provider() left it with.
        assert captured.get("model") == "cfg-default-model"
        assert captured.get("provider") == "openrouter"
        # And with a healthy ping the job proceeds to the agent normally.
        assert success is True
        assert final_response == "ok"
        mock_agent_cls.assert_called_once()
        # The model passed to the agent must match the same resolved default.
        _, agent_kwargs = mock_agent_cls.call_args
        assert agent_kwargs["model"] == "cfg-default-model"

        from cron.scheduler import _run_job_impl

        (tmp_path / "config.yaml").write_text("cron:\n  preflight_enabled: false\n")
        job = self._make_job("analysis")
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                    "model": "openrouter/model",
                },
            ),
            patch(
                "cron.evolution_preflight.preflight_provider",
                return_value="provider down",
            ) as mock_preflight,
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _output, final_response, _error = _run_job_impl(job)

        assert success is True
        assert final_response == "ok"
        mock_preflight.assert_not_called()
        mock_agent_cls.assert_called_once()


class TestSchedulerHaltGate:
    """Scheduler-level wiring for the halt-state gate (#913): a job for a
    gated stage must be skipped BEFORE any provider ping or agent
    construction when scripts/evolution_funnel.py's halt-state.txt is
    present, and jobs for ungated stages (funnel is no_agent and never
    reaches this code path; integration is left ungated) must proceed."""

    def _make_job(self, stage):
        return {
            "id": f"evolution-{stage}",
            "name": f"evolution-{stage}",
            "prompt": "do work",
        }

    def _write_halt_file(self, tmp_path):
        evo_dir = tmp_path / "evolution"
        evo_dir.mkdir(parents=True, exist_ok=True)
        (evo_dir / "halt-state.txt").write_text("halted")

    def test_halted_gated_stage_skips_without_agent_or_preflight(self, tmp_path):
        from cron.scheduler import _run_job_impl

        self._write_halt_file(tmp_path)
        job = self._make_job("analysis")
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                    "model": "openrouter/model",
                },
            ),
            patch("cron.evolution_preflight.preflight_provider") as mock_preflight,
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            success, output, final_response, error = _run_job_impl(job)

        assert success is True
        assert final_response == "[SILENT]"
        assert error is None
        assert "pipeline halted" in output
        mock_preflight.assert_not_called()
        mock_agent_cls.assert_not_called()

    def test_halted_all_four_gated_stages_skip(self, tmp_path):
        from cron.scheduler import _run_job_impl

        self._write_halt_file(tmp_path)
        for stage in ("research", "analysis", "implementation", "introspection"):
            job = self._make_job(stage)
            with (
                patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
                patch("cron.scheduler._resolve_origin", return_value=None),
                patch("dotenv.load_dotenv"),
                patch("hermes_state.SessionDB", return_value=MagicMock()),
                patch(
                    "hermes_cli.runtime_provider.resolve_runtime_provider",
                    return_value={
                        "api_key": "test-key",
                        "base_url": "https://example.invalid/v1",
                        "provider": "openrouter",
                        "api_mode": "chat_completions",
                        "model": "openrouter/model",
                    },
                ),
                patch("run_agent.AIAgent") as mock_agent_cls,
            ):
                success, _output, final_response, _error = _run_job_impl(job)
            assert success is True
            assert final_response == "[SILENT]"
            mock_agent_cls.assert_not_called()

    def test_halted_integration_stage_not_gated(self, tmp_path):
        """Integration is intentionally left ungated: gating it too would
        deadlock the halt (merged=0 triggers halt; integration is what
        performs merges, so it must keep running to let the pipeline
        recover on its own)."""
        from cron.scheduler import _run_job_impl

        self._write_halt_file(tmp_path)
        job = self._make_job("integration")
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                    "model": "openrouter/model",
                },
            ),
            patch("cron.evolution_preflight.preflight_provider", return_value=None),
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _output, final_response, _error = _run_job_impl(job)

        assert success is True
        assert final_response == "ok"
        mock_agent_cls.assert_called_once()

    def test_no_halt_file_gated_stage_proceeds_normally(self, tmp_path):
        from cron.scheduler import _run_job_impl

        (tmp_path / "evolution").mkdir(parents=True, exist_ok=True)
        job = self._make_job("research")
        with (
            patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                    "model": "openrouter/model",
                },
            ),
            patch("cron.evolution_preflight.preflight_provider", return_value=None),
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _output, final_response, _error = _run_job_impl(job)

        assert success is True
        assert final_response == "ok"
        mock_agent_cls.assert_called_once()
