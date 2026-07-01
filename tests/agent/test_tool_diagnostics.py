"""Tests for agent/tool_diagnostics.py — normalized failure taxonomy (#130/#175)."""

from agent.tool_diagnostics import classify, diagnostic_suffix, inline_diagnostics_enabled
from agent.tool_dispatch_helpers import make_tool_result_message


class TestClassify:
    def test_success_is_none(self):
        assert classify("ok, wrote 3 files") is None
        assert classify("") is None
        assert classify(None) is None

    def test_missing_command(self):
        cat, hint = classify("bash: foo: command not found")
        assert cat == "missing_command" and "prerequisites" in hint.lower()

    def test_permission(self):
        assert classify("Refusing to write to sensitive system path")[0] == "permission"
        assert classify("error: permission denied")[0] in ("permission", "missing_command", "runtime_error")

    def test_timeout(self):
        assert classify("request timed out after 120s")[0] == "timeout"
        assert classify("ClosedResourceError: server unreachable")[0] == "timeout"

    def test_limit(self):
        assert classify("value exceeds the maximum length of 2200 characters")[0] == "limit"

    def test_not_found(self):
        assert classify("grep: no matches found")[0] == "not_found"

    def test_runtime_error_fallback(self):
        assert classify("Traceback (most recent call last):\n  ...")[0] == "runtime_error"
        assert classify("process exited, exit code: 1")[0] == "runtime_error"


class TestInlineDiagnosticsEnabled:
    def test_default_off_with_empty_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        assert inline_diagnostics_enabled(config={}) is False

    def test_config_true_enables(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        assert inline_diagnostics_enabled(config={"agent": {"diagnostics": {"inline": True}}}) is True

    def test_env_var_truthy_values_enable(self, monkeypatch):
        for value in ("1", "true", "True", "yes", "on"):
            monkeypatch.setenv("HERMES_DIAGNOSTICS_INLINE", value)
            assert inline_diagnostics_enabled(config={}) is True, value

    def test_env_var_falsy_values_disable(self, monkeypatch):
        for value in ("0", "false", "False", "no", "off"):
            monkeypatch.setenv("HERMES_DIAGNOSTICS_INLINE", value)
            assert inline_diagnostics_enabled(config={"agent": {"diagnostics": {"inline": True}}}) is False, value

    def test_malformed_config_section_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        # "agent" is a string, not a dict — cfg_get() must not raise.
        assert inline_diagnostics_enabled(config={"agent": "oops"}) is False

    def test_config_none_falls_back_to_load_config_readonly(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        import hermes_cli.config as config_module

        monkeypatch.setattr(
            config_module, "load_config_readonly",
            lambda: {"agent": {"diagnostics": {"inline": True}}},
        )
        assert inline_diagnostics_enabled(config=None) is True


class TestDiagnosticSuffix:
    """Inline injection defaults OFF (#606) — classify() is a text heuristic,
    not a real success/failure signal, and false-positives on successful
    results that merely mention words like "timeout" or "error"."""

    def test_empty_for_success(self):
        assert diagnostic_suffix("done, all good") == ""

    def test_disabled_by_default_even_for_a_real_failure(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        assert diagnostic_suffix("permission denied", config={}) == ""

    def test_suffix_for_failure_when_explicitly_enabled_via_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        config = {"agent": {"diagnostics": {"inline": True}}}
        s = diagnostic_suffix("permission denied", config=config)
        assert s.startswith("\n\n[diagnostic] failure-class=") and "permission" in s

    def test_suffix_for_failure_when_explicitly_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_DIAGNOSTICS_INLINE", "1")
        s = diagnostic_suffix("permission denied", config={})
        assert s.startswith("\n\n[diagnostic] failure-class=") and "permission" in s

    def test_env_var_disables_even_if_config_enables(self, monkeypatch):
        monkeypatch.setenv("HERMES_DIAGNOSTICS_INLINE", "0")
        config = {"agent": {"diagnostics": {"inline": True}}}
        assert diagnostic_suffix("permission denied", config=config) == ""


class TestWiredIntoToolResult:
    def test_failure_result_unchanged_by_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        msg = make_tool_result_message("terminal", "bash: x: command not found", "c1")
        assert "[diagnostic]" not in msg["content"]

    def test_success_result_unchanged(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        msg = make_tool_result_message("read_file", "file contents, all fine", "c2")
        assert "[diagnostic]" not in msg["content"]

    def test_failure_result_gets_hint_when_explicitly_enabled(self, monkeypatch):
        # Restores the pre-#606 integration coverage for the opt-in path: the
        # full make_tool_result_message() wiring must still surface the hint
        # when an operator turns inline diagnostics back on for debugging.
        monkeypatch.setenv("HERMES_DIAGNOSTICS_INLINE", "1")
        msg = make_tool_result_message("terminal", "bash: x: command not found", "c1")
        assert "[diagnostic] failure-class=missing_command" in msg["content"]



