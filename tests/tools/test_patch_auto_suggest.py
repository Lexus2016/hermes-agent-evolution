"""Tests for patch auto-suggest and hard stop (#1037)."""
import json
import pytest


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home


@pytest.fixture
def fresh_tracker():
    from tools.file_tools import _patch_failure_tracker, _patch_failure_lock
    with _patch_failure_lock:
        _patch_failure_tracker.clear()
    yield
    with _patch_failure_lock:
        _patch_failure_tracker.clear()


class TestSuggestClosestMatch:
    def test_finds_closest_match(self):
        from tools.fuzzy_match import suggest_closest_match
        content = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        result = suggest_closest_match("def foo():\n    return 42", content)
        assert "def foo():" in result and "return 1" in result

    def test_empty_and_no_match(self):
        from tools.fuzzy_match import suggest_closest_match
        assert suggest_closest_match("", "content") == ""
        assert suggest_closest_match("old", "") == ""
        assert suggest_closest_match("zzzzz_nothing", "x = 1\n") == ""


class TestPatchHardStop:
    def test_hard_stop_after_threshold(self, hermes_home, tmp_path, fresh_tracker):
        from tools.file_tools import _handle_patch
        target = tmp_path / "f.py"
        target.write_text("def foo():\n    return 1\n")
        for _i in range(3):
            _handle_patch({"mode": "replace", "path": str(target),
                "old_string": f"ZZZ_NO_MATCH_{_i}_XYZ123", "new_string": "x"},
                task_id="hs_t1")
        result = _handle_patch({"mode": "replace", "path": str(target),
            "old_string": "ZZZ_NO_MATCH_AGAIN_456", "new_string": "x"},
            task_id="hs_t1")
        assert "PATCH REFUSED" in json.loads(result).get("error", "")

    def test_hard_stop_suggests_match(self, hermes_home, tmp_path, fresh_tracker):
        from tools.file_tools import _handle_patch
        target = tmp_path / "f.py"
        target.write_text("def foo():\n    return 1\n")
        for _i in range(3):
            _handle_patch({"mode": "replace", "path": str(target),
                "old_string": f"ZZZ_GHOST_{_i}_ZZZ", "new_string": "x"},
                task_id="hs_t2")
        result = _handle_patch({"mode": "replace", "path": str(target),
            "old_string": "def foo():\n    return 99999_NOT_THERE",
            "new_string": "x"}, task_id="hs_t2")
        err = json.loads(result).get("error", "")
        assert "PATCH REFUSED" in err
        assert "return 1" in err, f"Expected auto-suggest, got: {err[:200]}"