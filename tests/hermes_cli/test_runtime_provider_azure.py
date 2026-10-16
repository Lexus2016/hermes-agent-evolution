"""Azure route and credential resolution regression contracts."""
import pytest
from types import SimpleNamespace
from hermes_cli import runtime_provider as rp

class TestAzureFoundryResolution:
    """Verify Azure Foundry resolves correctly for both API modes."""

    def _make_cfg(self, base_url: str, api_mode: str = "chat_completions"):
        return {
            "provider": "azure-foundry",
            "base_url": base_url,
            "api_mode": api_mode,
            # GPT-4 speaks chat completions on Azure, so this test's assertion
            # about chat_completions stays valid across the Apr 2026 fix that
            # upgrades GPT-5.x / codex deployments to codex_responses.
            "default": "gpt-4.1",
        }


    def test_azure_foundry_missing_base_url_raises(self, monkeypatch):
        monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "az-key")
        monkeypatch.delenv("AZURE_FOUNDRY_BASE_URL", raising=False)
        monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "azure-foundry")
        monkeypatch.setattr(rp, "_get_model_config", dict)
        monkeypatch.setattr(rp, "load_pool", lambda provider: None)

        with pytest.raises(rp.AuthError, match="base URL"):
            rp.resolve_runtime_provider(requested="azure-foundry")


    # -- Model-family api_mode inference -------------------------------------
    # Azure rejects /chat/completions on GPT-5.x / codex / o-series with
    # ``400 "The requested operation is unsupported."`` — the resolver must
    # upgrade api_mode to ``codex_responses`` for those models even when the
    # config was persisted as ``chat_completions`` (the default the setup
    # wizard writes when the user didn't pick explicitly).

    def _make_cfg_with_model(self, model: str, api_mode: str = "chat_completions"):
        return {
            "provider": "azure-foundry",
            "base_url": "https://synopsisse.openai.azure.com/openai/v1",
            "api_mode": api_mode,
            "default": model,
        }

    def test_gpt5_codex_upgrades_chat_completions_to_responses(self, monkeypatch):
        """Reproduces Bob's April 2026 bug: gpt-5.3-codex on chat_completions."""
        monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "az-key")
        monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "azure-foundry")
        monkeypatch.setattr(rp, "_get_model_config",
                            lambda: self._make_cfg_with_model("gpt-5.3-codex", "chat_completions"))
        monkeypatch.setattr(rp, "load_pool", lambda provider: None)

        resolved = rp.resolve_runtime_provider(requested="azure-foundry")

        assert resolved["api_mode"] == "codex_responses"
        assert resolved["base_url"] == "https://synopsisse.openai.azure.com/openai/v1"


    def test_o3_mini_upgrades(self, monkeypatch):
        monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "az-key")
        monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "azure-foundry")
        monkeypatch.setattr(rp, "_get_model_config",
                            lambda: self._make_cfg_with_model("o3-mini", "chat_completions"))
        monkeypatch.setattr(rp, "load_pool", lambda provider: None)

        resolved = rp.resolve_runtime_provider(requested="azure-foundry")

        assert resolved["api_mode"] == "codex_responses"


class TestAzureAnthropicEnvVarHint:
    _AZURE_URL = "https://my-resource.services.ai.azure.com/anthropic"

    def _cfg(self, **overrides):
        base = {"provider": "anthropic", "base_url": self._AZURE_URL}
        base.update(overrides)
        return base

    def test_key_env_hint_picks_custom_var(self, monkeypatch):
        """model.key_env names a non-default env var → that var's value is used."""
        monkeypatch.delenv("AZURE_ANTHROPIC_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("MY_CUSTOM_AZURE_KEY", "from-custom-var")
        monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "anthropic")
        monkeypatch.setattr(rp, "_get_model_config",
                            lambda: self._cfg(key_env="MY_CUSTOM_AZURE_KEY"))
        monkeypatch.setattr(rp, "load_pool", lambda provider: None)

        resolved = rp.resolve_runtime_provider(requested="anthropic")

        assert resolved["api_key"] == "from-custom-var"
        assert resolved["base_url"] == self._AZURE_URL


    def test_key_env_points_at_unset_var_falls_through(self, monkeypatch):
        """If key_env names an env var that isn't set, fall through to the
        historical fixed names rather than failing outright."""
        monkeypatch.setenv("AZURE_ANTHROPIC_KEY", "fallback-works")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("UNSET_VAR", raising=False)
        monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "anthropic")
        monkeypatch.setattr(rp, "_get_model_config",
                            lambda: self._cfg(key_env="UNSET_VAR"))
        monkeypatch.setattr(rp, "load_pool", lambda provider: None)

        resolved = rp.resolve_runtime_provider(requested="anthropic")

        assert resolved["api_key"] == "fallback-works"


    def test_non_azure_anthropic_path_ignores_key_env(self, monkeypatch):
        """key_env is only consulted on Azure endpoints — non-Azure Anthropic
        still goes through the regular resolve_anthropic_token chain."""
        monkeypatch.setenv("MY_KEY", "custom-key-value")
        monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "anthropic")
        monkeypatch.setattr(rp, "_get_model_config", lambda: {
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",  # non-Azure
            "key_env": "MY_KEY",
        })
        monkeypatch.setattr(rp, "load_pool", lambda provider: None)
        called = {"resolve_anthropic_token": False}
        def _fake_resolve(**_kwargs):
            called["resolve_anthropic_token"] = True
            return "token-from-resolver"
        monkeypatch.setattr(
            "agent.anthropic_credentials.resolve_anthropic_token",
            _fake_resolve,
        )

        resolved = rp.resolve_runtime_provider(requested="anthropic")

        # The normal chain runs — key_env is not consulted off-Azure.
        assert called["resolve_anthropic_token"] is True
        assert resolved["api_key"] == "token-from-resolver"
