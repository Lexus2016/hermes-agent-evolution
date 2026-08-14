"""Tests for hermes_cli/fallback_config.py — fallback entry API-key resolution."""

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.fallback_config import (
    get_fallback_chain,
    get_fallback_chain_from_list,
    resolve_entry_api_key,
)


class TestResolveEntryApiKey:
    def test_inline_api_key_wins(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"provider": "custom", "api_key": "inline-key", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "inline-key"

    def test_no_key_fields_returns_none(self):
        assert resolve_entry_api_key({"provider": "openrouter", "model": "glm"}) is None

    def test_whitespace_inline_key_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"api_key": "   ", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "env-key"

    def test_key_env_resolves_from_active_secret_scope_not_raw_env(self, monkeypatch):
        # Multiplexed gateway: os.environ holds another profile's key, but the
        # active per-turn secret scope holds this profile's key. The scoped
        # value must win — a raw os.getenv() would leak the other profile's
        # credential (issue #74311).
        monkeypatch.setenv("FB_KEY", "fake-other-profile-key")
        token = set_secret_scope({"FB_KEY": "fake-active-profile-key"})
        try:
            assert (
                resolve_entry_api_key({"key_env": "FB_KEY"})
                == "fake-active-profile-key"
            )
        finally:
            reset_secret_scope(token)

    def test_key_env_falls_back_to_env_when_no_active_scope(self, monkeypatch):
        # Non-multiplexed / single-profile behavior must be unchanged: with no
        # secret scope installed, resolution still reads os.environ.
        monkeypatch.setenv("FB_KEY", "env-key")
        assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "env-key"


class TestGetFallbackChainFromList:
    """Tests for get_fallback_chain_from_list — cron-specific chain (issue #2377)."""

    def test_empty_list_returns_empty(self):
        assert get_fallback_chain_from_list([]) == []

    def test_none_returns_empty(self):
        assert get_fallback_chain_from_list(None) == []

    def test_missing_provider_or_model_filtered(self):
        raw = [{"provider": "openrouter"}, {"model": "glm"}]
        assert get_fallback_chain_from_list(raw) == []

    def test_valid_entries_preserved(self):
        raw = [
            {"provider": "openrouter", "model": "deepseek-chat"},
            {"provider": "groq", "model": "llama-3.3-70b"},
        ]
        chain = get_fallback_chain_from_list(raw)
        assert len(chain) == 2
        assert chain[0]["provider"] == "openrouter"
        assert chain[1]["model"] == "llama-3.3-70b"

    def test_duplicates_removed(self):
        raw = [
            {"provider": "openrouter", "model": "glm"},
            {"provider": "openrouter", "model": "glm"},
        ]
        assert len(get_fallback_chain_from_list(raw)) == 1

    def test_case_insensitive_dedup(self):
        raw = [
            {"provider": "OpenRouter", "model": "GLM"},
            {"provider": "openrouter", "model": "glm"},
        ]
        assert len(get_fallback_chain_from_list(raw)) == 1

    def test_returns_fresh_copies(self):
        raw = [{"provider": "openrouter", "model": "glm", "api_key": "k"}]
        chain = get_fallback_chain_from_list(raw)
        chain[0]["api_key"] = "mutated"
        # Original input must not be mutated.
        assert raw[0]["api_key"] == "k"

    def test_single_dict_input_accepted(self):
        raw = {"provider": "openrouter", "model": "glm"}
        chain = get_fallback_chain_from_list(raw)
        assert len(chain) == 1


class TestCronFallbackInheritsGlobal:
    """Verify the empty-default cron fallback_providers inherits global chain."""

    def test_empty_cron_chain_falls_through_to_global(self):
        # Simulates the scheduler logic: empty cron.fallback_providers → global.
        global_chain = get_fallback_chain({
            "fallback_providers": [{"provider": "openrouter", "model": "glm"}]
        })
        cron_chain = get_fallback_chain_from_list([])
        # Scheduler picks global when cron chain is empty.
        assert cron_chain == []
        assert len(global_chain) == 1
