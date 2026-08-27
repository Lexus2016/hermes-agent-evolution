"""Tests for the delegate_task failure classifier (#3223 slice 1)."""

import json
import unittest

from tools.delegate_failure_classifier import (
    DelegateFailureClass,
    classify_delegate_failure,
    inject_delegate_failure_class,
)


class TestClassifyDelegateFailure(unittest.TestCase):
    def test_capability_blocked(self):
        self.assertEqual(
            classify_delegate_failure(error_text="Tool 'foo' is blocked by policy"),
            DelegateFailureClass.capability_blocked,
        )
        self.assertEqual(
            classify_delegate_failure(error_text="permission denied on resource"),
            DelegateFailureClass.capability_blocked,
        )

    def test_provider_error(self):
        self.assertEqual(
            classify_delegate_failure(error_text="API provider returned 503"),
            DelegateFailureClass.provider_error,
        )
        self.assertEqual(
            classify_delegate_failure(error_text="rate limit hit"),
            DelegateFailureClass.provider_error,
        )

    def test_timeout(self):
        self.assertEqual(
            classify_delegate_failure(error_text="Subagent timed out after 60s"),
            DelegateFailureClass.timeout,
        )

    def test_unrecognised_returns_none(self):
        self.assertIsNone(
            classify_delegate_failure(error_text="Something weird happened")
        )

    def test_parses_json_string(self):
        self.assertEqual(
            classify_delegate_failure('{"error": "not installed"}'),
            DelegateFailureClass.capability_blocked,
        )


class TestInjectDelegateFailureClass(unittest.TestCase):
    def test_top_level_error(self):
        payload = json.loads('{"error": "blocked by hardline policy"}')
        self.assertTrue(inject_delegate_failure_class(payload))
        self.assertEqual(payload["failure_class"], "capability-blocked")

    def test_per_task_results(self):
        payload = {
            "results": [
                {"task_index": 0, "status": "completed", "summary": "done"},
                {"task_index": 1, "status": "error", "error": "provider 503"},
                {"task_index": 2, "status": "error", "error": "timed out"},
            ],
            "total_duration_seconds": 5.0,
        }
        self.assertTrue(inject_delegate_failure_class(payload))
        self.assertNotIn("failure_class", payload["results"][0])
        self.assertEqual(payload["results"][1]["failure_class"], "provider-error")
        self.assertEqual(payload["results"][2]["failure_class"], "timeout")

    def test_noop_when_no_errors(self):
        payload = {"results": [{"status": "completed"}]}
        self.assertFalse(inject_delegate_failure_class(payload))


if __name__ == "__main__":
    unittest.main()
