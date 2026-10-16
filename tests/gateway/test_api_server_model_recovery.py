"""Focused regression coverage split from the runtime integration suite."""

from tests.gateway.test_api_server import (
    APIServerAdapter,
    PlatformConfig,
    _patch_create_agent_runtime,
)

pytest_plugins = ["tests.gateway.test_api_server"]

class TestCreateAgentModelRecovery:
    def test_create_agent_defaults_to_provider_catalog_model_when_empty(self, monkeypatch):
        """api_server.py had no equivalent of run.py's provider-catalog
        default when model resolves empty but a provider did resolve (e.g.
        `hermes auth add openai-codex` without `hermes model`) —
        AIAgent(model="") 400s every call."""
        captured = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        _patch_create_agent_runtime(monkeypatch, captured, FakeAgent)
        monkeypatch.setattr(
            "gateway.run._resolve_runtime_agent_kwargs",
            lambda: {"provider": "openai-codex", "base_url": "https://example.test/v1",
                     "api_mode": "codex_responses"},
        )
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "")
        monkeypatch.setattr(
            "hermes_cli.models.get_default_model_for_provider",
            lambda provider: "gpt-5.5-codex" if provider == "openai-codex" else None,
        )

        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)

        agent = adapter._create_agent(session_id="api-session")

        assert isinstance(agent, FakeAgent)
        assert captured["model"] == "gpt-5.5-codex"

    def test_create_agent_recovers_last_known_good_model_when_empty(self, monkeypatch):
        """Last-known-good recovery (#35314): a transient config-cache miss
        producing an empty model would build AIAgent(model="") and fail every
        call until manual retry, instead of reusing the model that just
        worked."""
        captured = []

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.append(dict(kwargs))

        _patch_create_agent_runtime(monkeypatch, {}, FakeAgent)
        monkeypatch.setattr("run_agent.AIAgent", FakeAgent)

        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)

        # Turn 1: model resolves fine — populates the last-known-good cache
        # (keyed on gateway_session_key).
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "minimax/minimax-m3")
        adapter._create_agent(session_id="api-session", gateway_session_key="stable-chan-1")
        assert captured[0]["model"] == "minimax/minimax-m3"
        assert adapter._last_resolved_model["stable-chan-1"] == "minimax/minimax-m3"

        # Turn 2: transient empty resolution, no provider catalog default —
        # must recover the model from turn 1, not build model="".
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "")
        monkeypatch.setattr(
            "gateway.run._resolve_runtime_agent_kwargs",
            lambda: {"provider": None, "base_url": None, "api_mode": None},
        )
        adapter._create_agent(session_id="another-session", gateway_session_key="stable-chan-1")
        assert captured[1]["model"] == "minimax/minimax-m3"

    # ── Recovery-net alias guards (PR for #79101) ──────────────────────

    def test_create_agent_does_not_cache_virtual_alias(self, monkeypatch):
        """Write-side guard: the advertised virtual model (``hermes-agent``)
        must never enter ``_last_resolved_model``, even when a prior turn
        (or the session-row bug) dispatched it."""
        captured = []

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.append(dict(kwargs))

        _patch_create_agent_runtime(monkeypatch, {}, FakeAgent)

        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)

        virtual = adapter._model_name
        # Make _resolve_gateway_model return the virtual alias — the
        # condition the session-row bug can produce after a prior turn.
        monkeypatch.setattr(
            "gateway.run._resolve_gateway_model", lambda: virtual,
        )

        adapter._create_agent(session_id="s1", gateway_session_key="ch")
        assert captured[0]["model"] == virtual
        # Cache must reject the alias.
        assert adapter._last_resolved_model.get("ch") != virtual
        assert adapter._last_resolved_model.get("*") != virtual

    def test_create_agent_rejects_virtual_alias_from_cache(self, monkeypatch):
        """Read-side gate: an empty-model dispatch with the alias in
        ``_last_resolved_model`` must NOT recover it — the recovery net
        must never serve the advertised virtual model."""
        captured = []

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.append(dict(kwargs))

        _patch_create_agent_runtime(monkeypatch, {}, FakeAgent)

        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)

        # Seed the cache with the alias (simulate a prior poisoned turn).
        adapter._last_resolved_model["ch"] = adapter._model_name
        adapter._last_resolved_model["*"] = adapter._model_name

        # Trigger an empty resolution.
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "")
        monkeypatch.setattr(
            "gateway.run._resolve_runtime_agent_kwargs",
            lambda: {"provider": None, "base_url": None, "api_mode": None},
        )
        adapter._create_agent(session_id="s1", gateway_session_key="ch")

        # The alias must not be dispatched.
        assert captured[0]["model"] != adapter._model_name

    def test_create_agent_recovery_still_works_for_legitimate_model(
        self, monkeypatch,
    ):
        """Non-regression: a real dispatched model still enters the cache
        and recovers on a subsequent empty-resolution turn — the alias
        guard must not break legitimate recovery."""
        captured = []

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.append(dict(kwargs))

        _patch_create_agent_runtime(monkeypatch, {}, FakeAgent)

        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)

        # Turn 1: legitimate model — must enter the cache.
        monkeypatch.setattr(
            "gateway.run._resolve_gateway_model",
            lambda: "anthropic/claude-opus-4.6",
        )
        adapter._create_agent(session_id="s1", gateway_session_key="ch")
        assert captured[0]["model"] == "anthropic/claude-opus-4.6"
        assert adapter._last_resolved_model["ch"] == "anthropic/claude-opus-4.6"

        # Turn 2: empty resolution — must recover the legitimate model.
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "")
        monkeypatch.setattr(
            "gateway.run._resolve_runtime_agent_kwargs",
            lambda: {"provider": None, "base_url": None, "api_mode": None},
        )
        adapter._create_agent(session_id="s2", gateway_session_key="ch")
        assert captured[1]["model"] == "anthropic/claude-opus-4.6"
