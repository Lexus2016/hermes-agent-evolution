"""Failure containment at plugin RPC, cleanup, discovery and validation boundaries."""

import contextvars
import io
import json
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("error", [ValueError("bad plugin"), SystemExit("plugin exit")])
def test_rpc_error_envelope_restores_caller_context(error):
    from hermes_cli import plugin_host_wire as wire

    caller = contextvars.Context()
    caller.run(wire._SERVING.set, 71)
    output = io.BytesIO()
    seen = []

    def handler(method, params, origin):
        seen.append((method, origin, wire.serving_request()))
        raise error

    channel = wire.Channel(io.BytesIO(), output, handler, name="failure-test",
                           context_for_origin=lambda origin: caller)
    before = wire.serving_request()
    channel._serve({"id": 5, "method": "plugin", "origin": 71})
    reply = json.loads(output.getvalue())
    assert seen == [("plugin", 71, 5)]
    assert reply == {"id": 5, "error": {"type": type(error).__name__, "message": str(error)}}
    assert caller.run(wire.serving_request) == 71
    assert wire.serving_request() == before
    expected = ValueError if isinstance(error, ValueError) else wire.PluginHostError
    with pytest.raises(expected, match=str(error)):
        channel.result_of(reply)


def test_profile_extraction_failure_is_rejected_by_parent(monkeypatch, tmp_path):
    from hermes_cli import plugin_host_child as child, plugin_host_profiles as profiles
    from hermes_cli.plugin_host_wire import PluginHostUnavailable

    output = io.BytesIO()
    monkeypatch.setattr(child, "_take_protocol_streams", lambda: (io.BytesIO(), output))
    monkeypatch.setattr(child, "capture_profiles", Mock(side_effect=RuntimeError("profile import failed")))
    assert child.extract_profiles_main(str(tmp_path), "broken_profile") == 0
    payload = json.loads(output.getvalue())
    assert payload == {"error": "RuntimeError: profile import failed"}
    monkeypatch.setattr(profiles, "_extract", lambda *args: payload)
    with pytest.raises(PluginHostUnavailable, match="profile import failed"):
        profiles.load_hosted_profiles(tmp_path, "broken_profile")
    # A rejected payload must not become a successful cached provider list.
    from hermes_constants import get_hermes_home
    assert not list((get_hermes_home() / "cache" / "plugin_host").rglob("*.json"))


@pytest.mark.parametrize("needs_kill", [False, True])
def test_failed_shutdown_rpc_still_closes_and_reaps(needs_kill):
    from hermes_cli.plugin_host import PluginHost, _SHUTDOWN_GRACE_SECS

    channel = Mock(closed_reason=None)
    channel.call.side_effect = RuntimeError("shutdown callback failed")
    proc = Mock()
    proc.wait.side_effect = ([subprocess.TimeoutExpired("host", _SHUTDOWN_GRACE_SECS), 0]
                             if needs_kill else [0])
    host = object.__new__(PluginHost)
    host._lock = threading.Lock()
    host._proc, host._channel = proc, channel
    host.shutdown()
    channel.call.assert_called_once_with("shutdown", {}, timeout=_SHUTDOWN_GRACE_SECS)
    channel.close.assert_called_once_with("shutdown")
    assert host._stopping and host._proc is None and host._channel is None
    assert proc.wait.call_count == (2 if needs_kill else 1)
    assert all(call.kwargs == {"timeout": _SHUTDOWN_GRACE_SECS} for call in proc.wait.call_args_list)
    assert proc.kill.call_count == int(needs_kill)


def test_unload_callback_failure_keeps_cleanup_and_visible_error(monkeypatch, caplog):
    from hermes_cli.plugin_host import PluginHost
    from hermes_cli.plugin_host_child import HostRuntime

    runtime = object.__new__(HostRuntime)
    called = []

    def fail():
        called.append("failed")
        raise RuntimeError("unload failed")

    runtime.unload_callbacks = {"broken": [lambda: called.append("first"), fail]}
    runtime._lock = threading.Lock()
    runtime.refs = {1: object(), 2: object()}
    runtime.owners = {1: "broken", 2: "other"}
    runtime.modules = {"broken": "_failure_test_plugin"}
    monkeypatch.setitem(sys.modules, "_failure_test_plugin", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "_failure_test_plugin.child", SimpleNamespace())
    reply = runtime.op_unload({"plugin_key": "broken"})
    assert reply == {"errors": ["RuntimeError: unload failed"]}
    assert called == ["failed", "first"]
    assert set(runtime.refs) == {2} and runtime.owners == {2: "other"}
    assert not runtime.modules and not runtime.unload_callbacks
    assert "_failure_test_plugin" not in sys.modules
    assert "_failure_test_plugin.child" not in sys.modules
    parent = object.__new__(PluginHost)
    parent._contexts = {"broken": object()}
    parent._channel = Mock(closed_reason=None)
    parent._channel.call.return_value = reply
    parent._unload("broken")
    assert not parent._contexts
    assert "unload failed" in caplog.text


def test_metadata_discovery_failure_retains_profile_secret_denial(monkeypatch, tmp_path):
    from hermes_cli import config
    from tools.environments import local_env_policy as policy

    home = tmp_path / "first"
    manifest = home / "plugins" / "demo" / "plugin.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("name: demo\nkind: platform\nrequires_env:\n  - FAILURE_PROBE_TOKEN\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(policy, "_HOME_ADAPTER_SECRET_CACHE", {})
    monkeypatch.setattr(config, "OPTIONAL_ENV_VARS", dict(config.OPTIONAL_ENV_VARS))
    assert "FAILURE_PROBE_TOKEN" in policy._home_adapter_secret_env()
    manifest.write_text("name: [broken YAML\n", encoding="utf-8")
    real_scan = config._platform_plugin_manifests
    calls = []

    def scan(*args, **kwargs):
        if not kwargs.get("strict", False):
            calls.append("metadata failed")
            raise RuntimeError("metadata discovery unavailable")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(config, "_platform_plugin_manifests", scan)
    config._inject_platform_plugin_env_vars()
    assert calls
    names, complete = config.platform_manifest_secret_scan(home)
    assert not complete and "FAILURE_PROBE_TOKEN" not in names
    assert "FAILURE_PROBE_TOKEN" in policy._home_adapter_secret_env()
    assert policy._is_provider_env_blocklisted("FAILURE_PROBE_TOKEN", policy._home_adapter_secret_env())
    # Neither the retained denial nor a failed scan crosses the profile boundary.
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(second))
    assert "FAILURE_PROBE_TOKEN" not in policy._home_adapter_secret_env()
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert "FAILURE_PROBE_TOKEN" in policy._home_adapter_secret_env()


def test_relay_configuration_exception_reports_manual_issue(monkeypatch, capsys):
    from agent import relay_runtime
    from hermes_cli.doctor_config import _check_relay_plugins

    monkeypatch.setattr(relay_runtime, "resolve_plugin_sources", Mock(side_effect=RuntimeError("broken relay config")))
    finding = _check_relay_plugins(False)
    assert finding.manual_issues and finding.fixed == 0
    output = capsys.readouterr().out
    assert "could not be read" in output and "broken relay config" in output


def test_locale_parser_exception_fails_validation(monkeypatch, tmp_path):
    from hermes_cli import plugin_validate_locales as locales
    from hermes_cli.plugin_validate import ValidationReport

    monkeypatch.setattr(locales, "_load_document", Mock(side_effect=ValueError("invalid locale syntax")))
    report = ValidationReport()
    locales._check_locale_file(report, "pl", locales.CORE_SURFACE, tmp_path / "pl.yaml")
    assert not report.ok and report.exit_code == 1
    assert len(report.failures) == 1
    assert "invalid locale syntax" in report.failures[0]
