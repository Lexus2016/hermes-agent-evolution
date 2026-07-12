"""Browser console/click/type failure paths surface the shared taxonomy (#745).

Step 2 of #745: browser_click / browser_type / browser_console failure results
must carry a ``failure_class`` drawn from the shared ``agent/tool_diagnostics``
categories (via ``browser_navigate_fallback.classify_browser_error``), the same
way ``browser_navigate`` already does — no parallel browser-only taxonomy.
"""

import json
from unittest.mock import patch

import agent.tool_diagnostics as td
import tools.browser_tool as bt

_SHARED_CATEGORIES = {cat for _pat, cat, _hint in td._RULES}


class TestFailureTaggingHelpers:
    def test_annotate_adds_shared_failure_class(self):
        resp = bt._annotate_browser_failure(
            {"success": False, "error": "Could not connect to Chrome backend"}
        )
        assert resp["failure_class"] == "provider_dead"
        assert resp["failure_class"] in _SHARED_CATEGORIES

    def test_annotate_is_noop_on_success(self):
        resp = bt._annotate_browser_failure({"success": True, "result": "ok"})
        assert "failure_class" not in resp

    def test_annotate_preserves_existing_failure_class(self):
        resp = bt._annotate_browser_failure(
            {"success": False, "error": "x", "failure_class": "timeout"}
        )
        assert resp["failure_class"] == "timeout"

    def test_tag_string_adds_failure_class(self):
        tagged = bt._tag_browser_failure_class(
            json.dumps({"success": False, "error": "navigation timed out"})
        )
        assert json.loads(tagged)["failure_class"] == "timeout"

    def test_tag_string_noop_on_success_or_garbage(self):
        ok = json.dumps({"success": True, "result": 1})
        assert bt._tag_browser_failure_class(ok) == ok
        assert bt._tag_browser_failure_class("not json") == "not json"


class TestClickTypeConsoleFailurePaths:
    def test_browser_click_failure_has_failure_class(self):
        failed = {"success": False, "error": "Could not connect to Chrome backend"}
        with patch("tools.browser_tool._is_camofox_mode", return_value=False), \
             patch("tools.browser_tool._last_session_key", return_value="t"), \
             patch("tools.browser_tool._blocked_private_page_action", return_value=None), \
             patch("tools.browser_tool._run_browser_command", return_value=failed):
            out = json.loads(bt.browser_click("@e5"))
        assert out["success"] is False
        assert out["failure_class"] == "provider_dead"

    def test_browser_type_failure_has_failure_class(self):
        failed = {"success": False, "error": "navigation timed out"}
        with patch("tools.browser_tool._is_camofox_mode", return_value=False), \
             patch("tools.browser_tool._last_session_key", return_value="t"), \
             patch("tools.browser_tool._blocked_private_page_action", return_value=None), \
             patch("tools.browser_tool._run_browser_command", return_value=failed):
            out = json.loads(bt.browser_type("@e3", "hello"))
        assert out["success"] is False
        assert out["failure_class"] == "timeout"

    def test_browser_console_eval_failure_has_failure_class(self):
        failed = {"success": False, "error": "Could not connect to Chrome backend"}
        with patch("tools.browser_tool._enforce_browser_eval_policy", return_value=None), \
             patch("tools.browser_tool._is_camofox_mode", return_value=False), \
             patch("tools.browser_tool._last_session_key", return_value="t"), \
             patch("tools.browser_tool._eval_ssrf_guard_active", return_value=False), \
             patch("tools.browser_tool._run_browser_command", return_value=failed):
            out = json.loads(bt.browser_console(expression="document.title"))
        assert out["success"] is False
        assert out["failure_class"] == "provider_dead"

    def test_browser_click_success_has_no_failure_class(self):
        ok = {"success": True}
        with patch("tools.browser_tool._is_camofox_mode", return_value=False), \
             patch("tools.browser_tool._last_session_key", return_value="t"), \
             patch("tools.browser_tool._blocked_private_page_action", return_value=None), \
             patch("tools.browser_tool._run_browser_command", return_value=ok):
            out = json.loads(bt.browser_click("@e5"))
        assert out["success"] is True
        assert "failure_class" not in out
