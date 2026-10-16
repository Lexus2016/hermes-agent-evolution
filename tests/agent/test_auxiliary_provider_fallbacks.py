"""Focused regression coverage split from the runtime integration suite."""

from tests.agent.test_auxiliary_client import (
    MagicMock,
    _DummyResponse,
    _clean_env,
    call_llm,
    patch,
    pytest,
)

class TestSynchronousFallbackCachePlans:
    @staticmethod
    def _run_configured_fallback(monkeypatch, entry):
        from agent.auxiliary_client import (
            _call_fallback_candidate_sync,
            _try_configured_fallback_chain,
        )

        client = MagicMock()
        client.base_url = entry["base_url"]
        client.chat.completions.create.return_value = _DummyResponse()
        resolved_calls = []

        def resolve(provider, model=None, **kwargs):
            resolved_calls.append((provider, model, kwargs))
            return client, model

        monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", resolve)
        monkeypatch.setattr(
            "agent.auxiliary_client._get_auxiliary_task_config",
            lambda task: {"fallback_chain": [entry]},
        )
        fallback_client, fallback_model, label = _try_configured_fallback_chain(
            task="moa_aggregator",
            failed_provider="primary",
        )
        tools = [{
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        _call_fallback_candidate_sync(
            fallback_client,
            fallback_model,
            label,
            task="moa_aggregator",
            messages=[
                {"role": "system", "content": "stable prefix"},
                {"role": "user", "content": "lookup"},
            ],
            temperature=None,
            max_tokens=None,
            tools=tools,
            effective_timeout=30.0,
            effective_extra_body={},
            reasoning_config=None,
        )
        return client, resolved_calls, tools

    def test_direct_anthropic_fallback_uses_entry_destination_for_tool_marker(self, monkeypatch):
        client, resolved_calls, tools = self._run_configured_fallback(monkeypatch, {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages",
        })

        assert resolved_calls == [(
            "anthropic",
            "claude-sonnet-4-6",
            {
                "explicit_base_url": "https://api.anthropic.com",
                "explicit_api_key": None,
                "api_mode": "anthropic_messages",
            },
        )]
        wire_tools = client.chat.completions.create.call_args.kwargs["tools"]
        assert "cache_control" in wire_tools[-1]
        assert "cache_control" not in tools[-1]

    def test_third_party_anthropic_fallback_keeps_message_markers_without_tool_marker(self, monkeypatch):
        client, resolved_calls, tools = self._run_configured_fallback(monkeypatch, {
            "provider": "custom",
            "model": "claude-sonnet-4-6",
            "base_url": "https://api.minimax.io/anthropic",
            "api_mode": "anthropic_messages",
        })

        assert resolved_calls[0][2]["explicit_base_url"] == "https://api.minimax.io/anthropic"
        assert resolved_calls[0][2]["api_mode"] == "anthropic_messages"
        wire_request = client.chat.completions.create.call_args.kwargs
        assert "cache_control" not in wire_request["tools"][-1]
        assert "cache_control" not in tools[-1]
        assert any(
            isinstance(part, dict) and "cache_control" in part
            for message in wire_request["messages"]
            for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        )

class TestAsynchronousFallbackCachePlans:
    @pytest.mark.asyncio
    async def test_async_fallback_replans_cache_sections_like_sync(self, monkeypatch):
        """Async mirror parity: per-destination cache replan, not verbatim pass-through."""
        from agent.auxiliary_client import (
            _call_fallback_candidate_async,
            _try_configured_fallback_chain,
        )

        entry = {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages",
        }
        client = MagicMock()
        client.base_url = entry["base_url"]

        async def _create(**kwargs):
            return _DummyResponse()

        client.chat.completions.create = MagicMock(side_effect=_create)
        monkeypatch.setattr(
            "agent.auxiliary_client.resolve_provider_client",
            lambda provider, model=None, **kwargs: (client, model),
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._get_auxiliary_task_config",
            lambda task: {"fallback_chain": [entry]},
        )
        fallback_client, fallback_model, label = _try_configured_fallback_chain(
            task="moa_aggregator",
            failed_provider="primary",
        )
        tools = [{
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        await _call_fallback_candidate_async(
            fallback_client,
            fallback_model,
            label,
            task="moa_aggregator",
            messages=[
                {"role": "system", "content": "stable prefix"},
                {"role": "user", "content": "lookup"},
            ],
            temperature=None,
            max_tokens=None,
            tools=tools,
            effective_timeout=30.0,
            effective_extra_body={},
            reasoning_config=None,
        )

        wire_tools = client.chat.completions.create.call_args.kwargs["tools"]
        assert "cache_control" in wire_tools[-1]
        assert "cache_control" not in tools[-1]

class TestAutoRoutedProviderProfileHooks:
    def test_cached_auto_route_projects_selected_provider_on_every_request(self):
        """Auto routing must retain the concrete provider for request hooks."""
        import agent.auxiliary_client as aux
        from providers.base import ProviderProfile

        hook_calls = []

        class DynamicProfile(ProviderProfile):
            def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
                hook_calls.append(context)
                return {}, {
                    "extra_headers": {
                        "Authorization": f"Bearer token-{len(hook_calls)}",
                    },
                }

        profile = DynamicProfile(name="agentgateway")
        client = MagicMock()
        client.base_url = "https://gateway.example.com/v1"
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="ok"))]

        def lookup_profile(name):
            return profile if name == "agentgateway" else None

        aux.shutdown_cached_clients()
        try:
            with (
                patch(
                    "agent.auxiliary_client._resolve_task_provider_model",
                    return_value=("auto", "gateway/model", None, None, None),
                ),
                patch(
                    "agent.auxiliary_client._resolve_auto_route",
                    return_value=(client, "gateway/model", "agentgateway"),
                ) as resolve_auto,
                patch("providers.get_provider_profile", side_effect=lookup_profile),
                patch(
                    "agent.auxiliary_client._relay_sync_completion",
                    return_value=response,
                ) as relay,
            ):
                for _ in range(2):
                    result = call_llm(
                        task="title_generation",
                        messages=[{"role": "user", "content": "title this"}],
                    )
                    assert result is response
        finally:
            aux.shutdown_cached_clients()

        resolve_auto.assert_called_once()
        assert len(hook_calls) == 2
        assert relay.call_args_list[0].args[1]["extra_headers"] == {
            "Authorization": "Bearer token-1",
        }
        assert relay.call_args_list[1].args[1]["extra_headers"] == {
            "Authorization": "Bearer token-2",
        }

class TestFastModelTier:
    """The opt-in titling fast tier: rot-proof and scoped to titling only."""

    def test_auto_client_cache_key_tracks_fast_model_preference(self):
        """Changing the routing preference must not reuse the old auto client."""
        from agent import auxiliary_client as ac

        with patch.object(ac, "_task_prefers_fast_model", return_value=False):
            main_key = ac._client_cache_key(
                "auto", async_mode=False, task="title_generation"
            )
        with patch.object(ac, "_task_prefers_fast_model", return_value=True):
            fast_key = ac._client_cache_key(
                "auto", async_mode=False, task="title_generation"
            )

        assert main_key != fast_key

    def test_catalog_match_prefers_rolling_alias_over_pinned_id(self):
        """A "-latest" alias wins: it is the only id that cannot go stale."""
        from agent import auxiliary_client as ac

        catalog = {
            "z-ai/glm-5.2": {},
            "openai/gpt-5.4-mini": {},
            "~openai/gpt-mini-latest": {},
            "stepfun/step-3.7-flash:free": {},
        }
        with patch("hermes_cli.models_pricing.fetch_models_with_pricing", return_value=catalog):
            assert ac._fast_model_from_catalog("nous") == "~openai/gpt-mini-latest"

    def test_catalog_match_skips_reasoning_batch_and_embedding_lookalikes(self):
        """Substring matching must not pick a thinker, a queue, or an encoder."""
        from agent import auxiliary_client as ac

        catalog = {
            "openai/o3-mini": {},
            "openai/gpt-5.4-mini:batch": {},
            "sentence-transformers/all-minilm-l6-v2": {},
            "google/gemini-3.6-flash": {},
        }
        with patch("hermes_cli.models_pricing.fetch_models_with_pricing", return_value=catalog):
            assert ac._fast_model_from_catalog("nous") == "google/gemini-3.6-flash"

    def test_catalog_match_skips_the_non_chat_siblings_of_a_chat_model(self):
        """A provider names its speech and image endpoints after the chat model
        they're paired with, so they satisfy the family rungs and can't answer."""
        from agent import auxiliary_client as ac

        catalog = {
            "openai/gpt-4o-mini-tts": {},
            "openai/gpt-4o-mini-transcribe": {},
            "openai/gpt-4o-mini-search-preview": {},
            "openai/gpt-4o-mini": {},
        }
        with patch("hermes_cli.models_pricing.fetch_models_with_pricing", return_value=catalog):
            assert ac._fast_model_from_catalog("nous") == "openai/gpt-4o-mini"

    def test_catalog_match_takes_the_newest_of_a_family(self):
        """The bare family rungs must land on the current generation.

        A provider serves every generation of its small tier it hasn't retired,
        and compared as strings the oldest sorts first — so the rung meant to
        keep the titler current was pinning it to the most obsolete member.
        """
        from agent import auxiliary_client as ac

        catalog = {
            "openai/gpt-3.5-mini": {},
            "openai/gpt-9-mini": {},
            "openai/gpt-10-mini": {},
        }
        with patch("hermes_cli.models_pricing.fetch_models_with_pricing", return_value=catalog):
            assert ac._fast_model_from_catalog("nous") == "openai/gpt-10-mini"

    def test_catalog_fetch_is_authenticated(self):
        """Most /v1/models endpoints need a key; anonymously they 401.

        A 401 reads as "this provider serves no small model", so the titler
        would fall back to the curated default and never notice.
        """
        from agent import auxiliary_client as ac

        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "sk-test", "base_url": "https://api.example.com/v1"},
        ), patch(
            "hermes_cli.models_pricing.fetch_models_with_pricing", return_value={}
        ) as fetch:
            ac._fast_model_from_catalog("openai")

        assert fetch.call_args.kwargs["api_key"] == "sk-test"
        assert fetch.call_args.kwargs["base_url"] == "https://api.example.com"

    def test_falls_back_to_curated_default_when_catalog_unavailable(self):
        """An offline catalog degrades to the provider's pinned default."""
        from agent import auxiliary_client as ac

        with patch.object(ac, "_fast_model_from_catalog", return_value=""):
            assert (
                ac._get_aux_model_for_provider("anthropic", prefer_fast=True)
                == ac._get_aux_model_for_provider("anthropic")
            )

    def test_fast_tier_is_opt_in(self):
        """Without prefer_fast the resolver must not touch the live catalog."""
        from agent import auxiliary_client as ac

        with patch.object(ac, "_fast_model_from_catalog") as spy:
            ac._get_aux_model_for_provider("nous")
        spy.assert_not_called()

    def test_only_titling_is_in_the_fast_tier(self):
        """Compression/vision/search keep 'auto means my chat model'."""
        from agent.auxiliary_client import _FAST_MODEL_TASKS

        assert "title_generation" in _FAST_MODEL_TASKS
        overlap = {"compression", "vision", "web_extract"}.intersection(
            _FAST_MODEL_TASKS
        )
        assert not overlap
    def test_builtin_registry_raises_does_not_suppress_named_custom_lookup(self, monkeypatch):
        """#76602 (review feedback) — a partial catalog-load failure in the
        built-in registry must not short-circuit the user-defined
        provider lookup. The two lookups are now parallel and
        independent; an exception in one does not suppress the other.

        Before the refactor, ``get_provider`` raising jumped the outer
        ``except`` to the hardcoded allowlist fallback and never
        consulted ``_get_named_custom_provider`` — so a configured
        named provider was still downgraded to ``"custom"`` whenever the
        built-in catalog failed to load. The user's repro path
        (Hermes desktop on Windows with a partial / early-startup
        catalog state) hits this branch.
        """
        import agent.auxiliary_client as ac

        def _catalog_raises(_name):
            raise RuntimeError("built-in catalog unavailable")

        fake_entry = {
            "name": "AgnesAI",
            "base_url": "https://api.agnes-ai.cn/v1",
            "api_key": "sk-agnes-from-config",
            "model": "agnes-2.5-flash",
        }
        with patch(
            "hermes_cli.runtime_provider._get_named_custom_provider",
            return_value=fake_entry,
        ), patch(
            "hermes_cli.providers.get_provider", side_effect=_catalog_raises,
        ):
            resolved_provider, _model, base_url, _api_key, _api_mode = (
                ac._resolve_task_provider_model(
                    task="vision",
                    provider="agnes-ai.cn",
                    model="agnes-2.5-flash",
                    base_url="https://api.agnes-ai.cn/v1",
                    api_key=None,
                )
            )

        assert resolved_provider == "agnes-ai.cn", (
            "Built-in registry raising must not suppress the named-custom "
            "lookup; the user-defined provider must remain named (review "
            "feedback on #76602 — partial-load failure path)"
        )
        assert base_url == "https://api.agnes-ai.cn/v1"

    def test_resolve_vision_provider_client_preserves_named_provider_via_config(self, tmp_path):
        """#76602 (review feedback) — integration test through the real
        ``resolve_vision_provider_client`` entry point with a real
        ``HERMES_HOME`` config.yaml, mirroring the pattern from
        ``tests/agent/test_auxiliary_named_custom_providers.py``.

        Before the fix the repro in the issue body returned
        ``('custom', 'no-key-required')`` for this exact config shape;
        after the fix the named provider is preserved and the inline
        ``api_key`` from the providers: entry reaches the client.
        """
        import hermes_yaml as yaml

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(yaml.safe_dump({
            "model": {"default": "test-model"},
            "providers": {
                "agnes-ai.cn": {
                    "name": "AgnesAI",
                    "base_url": "https://api.agnes-ai.cn/v1",
                    "api_key": "sk-agnes-test",
                    "model": "agnes-2.5-flash",
                },
            },
        }))
        import os
        old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(hermes_home)
        try:
            from agent.auxiliary_client import resolve_vision_provider_client

            resolved_provider, client, _model = resolve_vision_provider_client(
                provider="agnes-ai.cn",
                model="agnes-2.5-flash",
                base_url="https://api.agnes-ai.cn/v1",
                api_key=None,
            )
        finally:
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home

        assert resolved_provider == "agnes-ai.cn", (
            "resolve_vision_provider_client must preserve a named "
            "user-defined provider + explicit base_url rather than "
            "downgrading to 'custom' (issue #76602 repro)"
        )
        # The inline api_key from the providers: entry must reach the
        # client — this is what the 'custom' downgrade was losing.
        assert client.api_key == "sk-agnes-test"

class TestPreserveNamedCustomProviderWithBaseUrl:
    """#76602 — _resolve_task_provider_model must keep a user-defined
    provider named when the call site passes ``provider=<name>`` +
    ``base_url=...`` (the shape async_call_llm takes after resolving the
    auxiliary.vision task config). The bare-custom downgrade to the
    ``"custom"`` string loses the key and produces 401s.
    """

    def test_user_defined_provider_named_in_config_is_preserved(self, monkeypatch):
        """A provider name from ``providers:`` survives explicit base_url.

        Before the fix this returned ``("custom", ...)`` → bare-custom
        branch in ``resolve_provider_client`` → ``no-key-required`` → 401.
        After the fix it returns ``("agnes-ai.cn", ...)`` so the
        named-custom-provider branch picks up
        ``providers.<name>.api_key`` (or the configured ``key_env``).
        """
        import agent.auxiliary_client as ac

        fake_entry = {
            "name": "AgnesAI",
            "base_url": "https://api.agnes-ai.cn/v1",
            "api_key": "sk-agnes-test",
            "model": "agnes-2.5-flash",
        }
        with patch(
            "hermes_cli.runtime_provider._get_named_custom_provider",
            return_value=fake_entry,
        ), patch(
            "hermes_cli.providers.get_provider", return_value=None,
        ):
            resolved_provider, _model, base_url, _api_key, _api_mode = (
                ac._resolve_task_provider_model(
                    task="vision",
                    provider="agnes-ai.cn",
                    model="agnes-2.5-flash",
                    base_url="https://api.agnes-ai.cn/v1",
                    api_key=None,
                )
            )

        assert resolved_provider == "agnes-ai.cn", (
            "User-defined provider from providers: section must be preserved "
            "instead of being downgraded to 'custom' (issue #76602)"
        )
        assert base_url == "https://api.agnes-ai.cn/v1"

    def test_built_in_provider_with_base_url_still_preserved(self, monkeypatch):
        """Built-in registry hit still wins — user-defined fallback only
        fires when the built-in registry missed. This guards against
        regressing the pre-existing built-in provider behavior.
        """
        import agent.auxiliary_client as ac

        with patch(
            "hermes_cli.providers.get_provider",
            return_value={"name": "Anthropic", "api_key": "sk-anthropic"},
        ):
            resolved_provider, _model, base_url, _api_key, _api_mode = (
                ac._resolve_task_provider_model(
                    task="moa_reference",
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    base_url="https://api.anthropic.com/v1",
                    api_key="sk-anthropic",
                )
            )

        assert resolved_provider == "anthropic"
        assert base_url == "https://api.anthropic.com/v1"

    def test_unknown_provider_with_base_url_falls_back_to_custom_downgrade(self, monkeypatch):
        """Provider name not in either registry → keep the existing
        ``"custom"`` downgrade behavior. Nothing changes for truly
        anonymous custom endpoints (no name → no key → bare-custom branch
        handles ``no-key-required`` itself).
        """
        import agent.auxiliary_client as ac

        with patch(
            "hermes_cli.runtime_provider._get_named_custom_provider",
            return_value=None,
        ), patch(
            "hermes_cli.providers.get_provider", return_value=None,
        ):
            resolved_provider, _model, base_url, _api_key, _api_mode = (
                ac._resolve_task_provider_model(
                    task="vision",
                    provider="some-unknown-gateway",
                    model="custom-model",
                    base_url="https://example.com/v1",
                    api_key="some-token",
                )
            )

        # Pre-existing behavior: unknown name + explicit base_url →
        # downgrade to "custom" so the caller routes through the
        # bare-custom branch (which uses the explicit api_key).
        assert resolved_provider == "custom"
        assert base_url == "https://example.com/v1"

    def test_hardcoded_allowlist_still_works_when_both_registries_unavailable(self, monkeypatch):
        """If both the built-in registry and the user-defined config are
        unavailable (e.g. early import path before config is loaded), the
        existing hardcoded allowlist still returns True for known names —
        so xai-oauth / qwen-oauth / etc. aren't regressed by the fix.
        """
        import agent.auxiliary_client as ac

        def _boom(_name):
            raise RuntimeError("catalog unavailable")

        with patch(
            "hermes_cli.runtime_provider._get_named_custom_provider",
            side_effect=_boom,
        ), patch(
            "hermes_cli.providers.get_provider", side_effect=_boom,
        ):
            resolved_provider, _model, _base_url, _api_key, _api_mode = (
                ac._resolve_task_provider_model(
                    task="moa_reference",
                    provider="xai-oauth",
                    model="grok-3",
                    base_url="https://api.x.ai/v1",
                    api_key="xai-token",
                )
            )

        assert resolved_provider == "xai-oauth"
