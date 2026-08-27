"""Tests for delegate_task consecutive-failure loop guard (#3224 slice 2)."""

import json
from unittest.mock import MagicMock

import pytest

from tools.delegate_loop_guard import (
    DEFAULT_DELEGATE_MAX_LOOP_FAILURES,
    DELEGATE_LOOP_GUARD,
    _compute_goal_signature,
)
from tools.delegate_tool import delegate_task


@pytest.fixture(autouse=True)
def clean_guard():
    DELEGATE_LOOP_GUARD.reset("test-session")
    DELEGATE_LOOP_GUARD.reset("test-session-2")
    yield
    DELEGATE_LOOP_GUARD.reset("test-session")
    DELEGATE_LOOP_GUARD.reset("test-session-2")


def test_goal_signature_normalization():
    sig1 = _compute_goal_signature([{"task": "Run tests on branch"}])
    sig2 = _compute_goal_signature([{"task": "  run   tests  on  branch  "}])
    sig3 = _compute_goal_signature([{"task": "Run tests on main"}])

    assert sig1 == sig2
    assert sig1 != sig3


def test_first_failure_does_not_trip():
    tasks = [{"task": "fix bug"}]
    results = [{"status": "failed", "error": "something exploded"}]

    tripped, count, diag = DELEGATE_LOOP_GUARD.record_and_evaluate(
        "test-session", tasks, results, budget=3
    )
    assert not tripped
    assert count == 1
    assert diag is None
    assert DELEGATE_LOOP_GUARD.get_consecutive_failures("test-session") == 1


def test_consecutive_identical_failures_trip_at_budget():
    tasks = [{"task": "fix bug"}]
    results = [{"status": "failed", "error": "something exploded"}]

    # 1st failure
    tripped, count, diag = DELEGATE_LOOP_GUARD.record_and_evaluate(
        "test-session", tasks, results, budget=3
    )
    assert not tripped
    assert count == 1

    # 2nd failure
    tripped, count, diag = DELEGATE_LOOP_GUARD.record_and_evaluate(
        "test-session", tasks, results, budget=3
    )
    assert not tripped
    assert count == 2

    # 3rd failure (trips)
    tripped, count, diag = DELEGATE_LOOP_GUARD.record_and_evaluate(
        "test-session", tasks, results, budget=3
    )
    assert tripped
    assert count == 3
    assert diag is not None
    assert "Delegate loop guard tripped" in diag
    assert "Change strategy" in diag


def test_changed_goal_resets_consecutive_count():
    tasks1 = [{"task": "fix bug A"}]
    tasks2 = [{"task": "fix bug B"}]
    fail = [{"status": "failed", "error": "err"}]

    DELEGATE_LOOP_GUARD.record_and_evaluate("test-session", tasks1, fail, budget=3)
    DELEGATE_LOOP_GUARD.record_and_evaluate("test-session", tasks1, fail, budget=3)
    assert DELEGATE_LOOP_GUARD.get_consecutive_failures("test-session") == 2

    # Different task resets count to 1
    tripped, count, diag = DELEGATE_LOOP_GUARD.record_and_evaluate(
        "test-session", tasks2, fail, budget=3
    )
    assert not tripped
    assert count == 1
    assert diag is None


def test_success_resets_consecutive_failures():
    tasks = [{"task": "fix bug"}]
    fail = [{"status": "failed", "error": "err"}]
    ok = [{"status": "completed", "result": "done"}]

    DELEGATE_LOOP_GUARD.record_and_evaluate("test-session", tasks, fail, budget=3)
    DELEGATE_LOOP_GUARD.record_and_evaluate("test-session", tasks, fail, budget=3)
    assert DELEGATE_LOOP_GUARD.get_consecutive_failures("test-session") == 2

    # Success resets counter
    tripped, count, diag = DELEGATE_LOOP_GUARD.record_and_evaluate(
        "test-session", tasks, ok, budget=3
    )
    assert not tripped
    assert count == 0
    assert diag is None
    assert DELEGATE_LOOP_GUARD.get_consecutive_failures("test-session") == 0


def test_session_isolation():
    tasks = [{"task": "fix bug"}]
    fail = [{"status": "failed", "error": "err"}]

    DELEGATE_LOOP_GUARD.record_and_evaluate("test-session", tasks, fail, budget=3)
    DELEGATE_LOOP_GUARD.record_and_evaluate("test-session", tasks, fail, budget=3)
    DELEGATE_LOOP_GUARD.record_and_evaluate("test-session-2", tasks, fail, budget=3)

    assert DELEGATE_LOOP_GUARD.get_consecutive_failures("test-session") == 2
    assert DELEGATE_LOOP_GUARD.get_consecutive_failures("test-session-2") == 1


def test_empty_children_early_return_triggers_guard():
    from unittest.mock import patch

    parent = MagicMock()
    parent.session_id = "test-session"
    parent._delegate_depth = 0
    parent.enabled_toolsets = ["terminal"]

    mock_child = MagicMock()
    mock_child.valid_tool_names = set()
    mock_child._delegate_resolved_toolsets = []
    mock_child._delegate_requested_toolsets = []
    mock_child._delegate_denied_toolsets = []
    mock_child.run_conversation.return_value = {
        "completed": False,
        "status": "failed",
        "error": "child failure",
        "final_response": "error",
    }

    tasks = [
        {"goal": "task 1 with empty tools"},
        {"goal": "task 2 with empty tools"},
    ]

    with patch("tools.delegate_tool._build_child_agent", return_value=mock_child):
        for _ in range(DEFAULT_DELEGATE_MAX_LOOP_FAILURES - 1):
            res = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
            assert "delegate_loop_guard_tripped" not in res

        # Nth attempt trips guard
        res = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
        assert res.get("delegate_loop_guard_tripped") is True
        assert res.get("consecutive_delegate_failures") == DEFAULT_DELEGATE_MAX_LOOP_FAILURES
        assert "strategy_recommendation" in res
