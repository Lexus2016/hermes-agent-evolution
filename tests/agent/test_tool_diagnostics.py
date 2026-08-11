"""Tests for agent/tool_diagnostics.py — normalized failure taxonomy (#130/#175)."""

import pytest

from agent.tool_diagnostics import (
    classify,
    diagnostic_suffix,
    inline_diagnostics_enabled,
)
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
        assert classify("error: permission denied")[0] in (
            "permission",
            "missing_command",
            "runtime_error",
        )

    def test_timeout(self):
        assert classify("request timed out after 120s")[0] == "timeout"
        assert classify("ClosedResourceError: server unreachable")[0] == "timeout"

    def test_limit(self):
        assert (
            classify("value exceeds the maximum length of 2200 characters")[0]
            == "limit"
        )

    def test_not_found(self):
        assert classify("grep: no matches found")[0] == "not_found"

    def test_runtime_error_fallback(self):
        assert (
            classify("Traceback (most recent call last):\n  ...")[0] == "runtime_error"
        )
        assert classify("process exited, exit code: 1")[0] == "runtime_error"

    def test_exit_code_137_oom_killed(self):
        """#2306 — exit 137 (SIGKILL/OOM) maps to oom_killed, not runtime_error."""
        cat, hint = classify("process exited, exit code: 137")
        assert cat == "oom_killed"
        assert "OOM" in hint or "memory" in hint.lower()

    def test_exit_code_139_segfault(self):
        """#2306 — exit 139 (SIGSEGV) maps to segfault, not runtime_error."""
        cat, hint = classify("process exited, exit code: 139")
        assert cat == "segfault"
        assert "segfault" in hint.lower() or "crash" in hint.lower()

    def test_exit_code_143_terminated(self):
        """#2306 — exit 143 (SIGTERM) maps to terminated, not runtime_error."""
        cat, hint = classify("process exited, exit code: 143")
        assert cat == "terminated"
        assert "terminat" in hint.lower() or "signal" in hint.lower()

    def test_exit_code_2_command_misuse(self):
        """#2306 — exit 2 (shell misuse/bad arg) maps to command_misuse (deterministic)."""
        cat, hint = classify("process exited, exit code: 2")
        assert cat == "command_misuse"
        assert "deterministic" in hint.lower() or "do not retry" in hint.lower()

    def test_exit_code_25_command_misuse(self):
        """#2306 — exit 25 (2.x family) also maps to command_misuse."""
        cat, _ = classify("process exited, exit code: 25")
        assert cat == "command_misuse"

    def test_exit_code_127_still_runtime_error(self):
        """#2306 — exit 127 (command not found is handled by missing_command;
        raw 'exit code: 127' without 'command not found' falls to runtime_error)."""
        assert classify("process exited, exit code: 127")[0] == "runtime_error"

    def test_retry_spiral_diagnostic_classified_correctly(self):
        """#2302 — the spiral-break diagnostic must classify as ``retry_spiral``,
        NOT ``runtime_error`` (the message contains "failed" which would
        otherwise match the runtime_error catch-all)."""
        msg = (
            "Retry spiral detected: this exact command has failed identically "
            "4 times in a row (threshold 3). It is failing deterministically."
        )
        cat, hint = classify(msg)
        assert cat == "retry_spiral"
        assert "deterministic" in hint.lower()


class TestInlineDiagnosticsEnabled:
    def test_default_off_with_empty_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        assert inline_diagnostics_enabled(config={}) is False

    def test_config_true_enables(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        assert (
            inline_diagnostics_enabled(
                config={"agent": {"diagnostics": {"inline": True}}}
            )
            is True
        )

    def test_env_var_truthy_values_enable(self, monkeypatch):
        for value in ("1", "true", "True", "yes", "on"):
            monkeypatch.setenv("HERMES_DIAGNOSTICS_INLINE", value)
            assert inline_diagnostics_enabled(config={}) is True, value

    def test_env_var_falsy_values_disable(self, monkeypatch):
        for value in ("0", "false", "False", "no", "off"):
            monkeypatch.setenv("HERMES_DIAGNOSTICS_INLINE", value)
            assert (
                inline_diagnostics_enabled(
                    config={"agent": {"diagnostics": {"inline": True}}}
                )
                is False
            ), value

    def test_malformed_config_section_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        # "agent" is a string, not a dict — cfg_get() must not raise.
        assert inline_diagnostics_enabled(config={"agent": "oops"}) is False

    def test_config_none_falls_back_to_load_config_readonly(self, monkeypatch):
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        import hermes_cli.config as config_module

        monkeypatch.setattr(
            config_module,
            "load_config_readonly",
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


class TestPayloadAnomaly:
    """Tests for payload_anomaly — structural payload check (#1495 USR)."""

    @pytest.mark.parametrize("payload", [None, "", "   \n\t  ", [], {}])
    def test_empty_payload_types(self, payload):
        from agent.tool_diagnostics import payload_anomaly

        anomaly_type, hint = payload_anomaly(payload)
        assert anomaly_type == "empty_payload"
        assert "malfunctioned" in hint.lower() or "malfunction" in hint.lower()

    @pytest.mark.parametrize(
        "token", ["none", "null", "nil", "None", "NULL", "{}", "[]"]
    )
    def test_null_sentinel_strings(self, token):
        from agent.tool_diagnostics import payload_anomaly

        anomaly_type, _ = payload_anomaly(token)
        assert anomaly_type == "empty_payload", f"Failed for token: {token!r}"

    def test_all_null_dict_is_malformed(self):
        from agent.tool_diagnostics import payload_anomaly

        anomaly_type, hint = payload_anomaly({
            "data": None,
            "error": None,
            "results": [],
        })
        assert anomaly_type == "malformed_payload"
        assert "malfunctioned" in hint.lower()

    def test_valid_payloads_return_none(self):
        from agent.tool_diagnostics import payload_anomaly

        assert payload_anomaly("file contents here") is None
        assert payload_anomaly(["item1", "item2"]) is None
        assert payload_anomaly({"status": "ok", "data": [1, 2, 3]}) is None
        assert payload_anomaly({"data": None, "results": [1, 2]}) is None


class TestUSRSignalInjection:
    """Tests that broken payloads get [tool_error] in make_tool_result_message (#1495)."""

    def test_none_payload_gets_tool_error(self):
        msg = make_tool_result_message("web_search", None, "c1")
        assert "[tool_error]" in msg["content"]
        assert "empty_payload" in msg["content"]
        assert "malfunctioned" in msg["content"].lower()

    def test_empty_string_and_null_sentinel_get_tool_error(self):
        for payload in ("", "null", "{}"):
            msg = make_tool_result_message("mcp_tool", payload, "c2")
            assert "[tool_error]" in msg["content"], f"Failed for {payload!r}"

    def test_empty_list_gets_tool_error(self):
        msg = make_tool_result_message("search_files", [], "c3")
        content = msg["content"]
        if isinstance(content, list):
            content = str(content)
        assert "[tool_error]" in content

    def test_all_null_dict_gets_malformed_error(self):
        msg = make_tool_result_message(
            "mcp_tool", {"data": None, "error": None, "results": []}, "c6"
        )
        assert "[tool_error]" in msg["content"]
        assert "malformed_payload" in msg["content"]

    def test_valid_payload_no_tool_error(self):
        msg = make_tool_result_message("read_file", "file contents here", "c4")
        assert "[tool_error]" not in msg["content"]

    def test_anomaly_signal_prevents_safety_fabrication(self, monkeypatch):
        """Always-on (no env/config needed); explicitly tells model NOT to
        fabricate a safety rationale — the core USR defense."""
        monkeypatch.delenv("HERMES_DIAGNOSTICS_INLINE", raising=False)
        msg = make_tool_result_message("web_search", None, "c8")
        assert "[tool_error]" in msg["content"]
        assert "NOT" in msg["content"] or "not a" in msg["content"].lower()
