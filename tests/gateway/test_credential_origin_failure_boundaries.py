"""Credential-origin diagnostics must never weaken duplicate-bot refusal."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def profile_homes(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    incoming = root / "profiles" / "incoming"
    incoming.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for home in (root, incoming):
        (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n", encoding="utf-8")
        (home / ".env").write_text("TELEGRAM_BOT_TOKEN=synthetic-shared-token\n", encoding="utf-8")
    return root, incoming


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["dotenv", "default_home", "named_home"])
async def test_origin_failure_preserves_duplicate_credential_refusal(
    failure, profile_homes, monkeypatch, caplog,
):
    from agent import secret_scope
    import hermes_constants
    from hermes_cli import profiles
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run_adapters import GatewayAdapterLifecycleMixin

    root, incoming = profile_homes
    token = "synthetic-shared-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    platform_config = PlatformConfig(enabled=True, token=token)
    config = GatewayConfig(multiplex_profiles=True, platforms={Platform.TELEGRAM: platform_config})
    adapter = SimpleNamespace(token=token, config=platform_config)
    runner = GatewayAdapterLifecycleMixin()
    runner.config = config
    runner._profile_adapters = {}
    runner._profile_failed_platforms = {}
    runner._load_secondary_profile_config = AsyncMock(return_value=config)
    runner._connect_initial_adapter_with_timeout = AsyncMock()
    runner._safe_adapter_disconnect = AsyncMock()
    runner._configure_profile_adapter = Mock()
    runner._update_platform_runtime_status = Mock()
    creation_scope = []

    def create_adapter(_platform, _config):
        creation_scope.append((hermes_constants.get_hermes_home(), secret_scope.get_secret("TELEGRAM_BOT_TOKEN")))
        return adapter

    runner._create_adapter = create_adapter
    failed_calls = []

    def unavailable(*args, **kwargs):
        failed_calls.append((args, kwargs))
        raise RuntimeError("synthetic diagnostic lookup failure")

    target, name = {
        "dotenv": (secret_scope, "load_env_file"),
        "default_home": (hermes_constants, "get_default_hermes_root"),
        "named_home": (profiles, "get_profile_dir"),
    }[failure]
    real_origins = runner._duplicate_credential_origins

    def failing_origin_lookup(*args):
        # Inject only at the diagnostic boundary; real scope loading and adapter
        # credential claiming above still execute against the isolated profile.
        with monkeypatch.context() as injected:
            injected.setattr(target, name, unavailable)
            return real_origins(*args)

    runner._duplicate_credential_origins = failing_origin_lookup
    owner = "other" if failure == "named_home" else "default"
    claim = runner._adapter_credential_claim(Platform.TELEGRAM, adapter)
    claimed = {claim: owner}

    assert await runner._start_one_profile_adapters("incoming", incoming, claimed) == 0

    assert failed_calls, "the requested diagnostic failure was not exercised"
    assert creation_scope == [(incoming, token)]
    assert hermes_constants.get_hermes_home() == root
    assert claimed == {claim: owner}
    assert runner._profile_adapters == {"incoming": {}}
    runner._connect_initial_adapter_with_timeout.assert_not_awaited()
    runner._safe_adapter_disconnect.assert_not_awaited()
    runner._configure_profile_adapter.assert_not_called()
    runner._update_platform_runtime_status.assert_called_once()
    args, status = runner._update_platform_runtime_status.call_args
    assert args == ("incoming:telegram",)
    assert status["platform_state"] == "fatal"
    assert status["error_code"] == "duplicate_credential"
    assert owner in status["error_message"] and "incoming" in status["error_message"]
    assert "Give each profile its own telegram credential" in status["error_message"]
    assert token not in status["error_message"]
    assert token not in caplog.text
    if failure == "dotenv":
        assert "env TELEGRAM_BOT_TOKEN" in status["error_message"]
