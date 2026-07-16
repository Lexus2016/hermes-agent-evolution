# -*- coding: utf-8 -*-
"""Comprehensive pytest suite for :mod:`feasibility_checker` (issue #1031).

Covers:

* :class:`FeasibilityStatus` enum values & identity
* :class:`FeasibilityResult` construction, defaults, ``to_dict``/``from_dict``
* Abstract :class:`FeasibilityCheck` base behaviour
* Each concrete check (File / Tool / Write / Dir / Regex / NonEmptyString)
  including injectable seams, happy path, failure path, edge cases
* :class:`FeasibilityGate` evaluation (single step, multi-step, empty, None)
* Blocking detection and filtering
* :meth:`FeasibilityGate.summarize` formatting
* Serialisation roundtrips and JSON compatibility
* Error resilience (checks raising exceptions become UNCERTAIN)

Run::

    cd /Users/admin/.hermes/profiles/user1/evolution && \
    python -m pytest tests/test_feasibility_checker.py -v
"""

from __future__ import annotations

import json
import re

import pytest

from feasibility_checker import (
    DirectoryExistenceCheck,
    FeasibilityCheck,
    FeasibilityGate,
    FeasibilityResult,
    FeasibilityStatus,
    FileExistenceCheck,
    NonEmptyStringCheck,
    RegexValidCheck,
    ToolAvailabilityCheck,
    WritePathCheck,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def feasible_result() -> FeasibilityResult:
    return FeasibilityResult(
        status=FeasibilityStatus.FEASIBLE,
        check_name="demo",
    )


@pytest.fixture
def infeasible_result() -> FeasibilityResult:
    return FeasibilityResult(
        status=FeasibilityStatus.INFEASIBLE,
        check_name="demo",
        reason="boom",
        suggestion="fix it",
        metadata={"k": "v"},
    )


@pytest.fixture
def uncertain_result() -> FeasibilityResult:
    return FeasibilityResult(
        status=FeasibilityStatus.UNCERTAIN,
        check_name="demo",
        reason="dunno",
    )


# ---------------------------------------------------------------------------
# 1. FeasibilityStatus enum
# ---------------------------------------------------------------------------


class TestFeasibilityStatus:
    def test_feasible_value(self):
        assert FeasibilityStatus.FEASIBLE.value == "feasible"

    def test_infeasible_value(self):
        assert FeasibilityStatus.INFEASIBLE.value == "infeasible"

    def test_uncertain_value(self):
        assert FeasibilityStatus.UNCERTAIN.value == "uncertain"

    def test_three_members(self):
        assert len(list(FeasibilityStatus)) == 3

    def test_is_str_enum(self):
        assert isinstance(FeasibilityStatus.FEASIBLE, str)

    def test_from_value(self):
        assert FeasibilityStatus("feasible") is FeasibilityStatus.FEASIBLE

    def test_distinct_members(self):
        vals = {m.value for m in FeasibilityStatus}
        assert len(vals) == 3

    def test_infeasible_identity(self):
        assert FeasibilityStatus.INFEASIBLE is FeasibilityStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# 2. FeasibilityResult
# ---------------------------------------------------------------------------


class TestFeasibilityResultConstruction:
    def test_minimal_construction(self):
        r = FeasibilityResult(status=FeasibilityStatus.FEASIBLE, check_name="c")
        assert r.reason == ""
        assert r.suggestion == ""
        assert r.metadata == {}

    def test_full_construction(self, infeasible_result):
        assert infeasible_result.status is FeasibilityStatus.INFEASIBLE
        assert infeasible_result.reason == "boom"
        assert infeasible_result.suggestion == "fix it"
        assert infeasible_result.metadata == {"k": "v"}

    def test_default_metadata_is_new_dict(self):
        a = FeasibilityResult(status=FeasibilityStatus.FEASIBLE, check_name="a")
        b = FeasibilityResult(status=FeasibilityStatus.FEASIBLE, check_name="b")
        a.metadata["x"] = 1
        assert "x" not in b.metadata

    def test_is_blocker_true_for_infeasible(self, infeasible_result):
        assert infeasible_result.is_blocker is True

    def test_is_blocker_false_for_feasible(self, feasible_result):
        assert feasible_result.is_blocker is False

    def test_is_blocker_false_for_uncertain(self, uncertain_result):
        assert uncertain_result.is_blocker is False


class TestFeasibilityResultSerialization:
    def test_to_dict_keys(self, infeasible_result):
        d = infeasible_result.to_dict()
        assert set(d.keys()) == {"status", "check_name", "reason", "suggestion", "metadata"}

    def test_to_dict_status_is_value(self, infeasible_result):
        assert infeasible_result.to_dict()["status"] == "infeasible"

    def test_to_dict_is_json_serializable(self, infeasible_result):
        s = json.dumps(infeasible_result.to_dict())
        assert isinstance(s, str)

    def test_from_dict_roundtrip(self, infeasible_result):
        d = infeasible_result.to_dict()
        r = FeasibilityResult.from_dict(d)
        assert r.status is infeasible_result.status
        assert r.check_name == infeasible_result.check_name
        assert r.reason == infeasible_result.reason
        assert r.suggestion == infeasible_result.suggestion
        assert r.metadata == infeasible_result.metadata

    def test_from_dict_accepts_enum_status(self):
        d = {"status": FeasibilityStatus.FEASIBLE, "check_name": "c"}
        r = FeasibilityResult.from_dict(d)
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_from_dict_defaults_missing_optional(self):
        d = {"status": "feasible", "check_name": "c"}
        r = FeasibilityResult.from_dict(d)
        assert r.reason == ""
        assert r.suggestion == ""
        assert r.metadata == {}

    def test_from_dict_metadata_copied(self):
        meta = {"a": 1}
        d = {"status": "feasible", "check_name": "c", "metadata": meta}
        r = FeasibilityResult.from_dict(d)
        meta["a"] = 999
        assert r.metadata == {"a": 1}


# ---------------------------------------------------------------------------
# 3. FeasibilityCheck abstract base
# ---------------------------------------------------------------------------


class TestFeasibilityCheckBase:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            FeasibilityCheck()  # type: ignore[abstract]

    def test_subclass_callable(self):
        class Dummy(FeasibilityCheck):
            name = "dummy"

            def check(self, context):
                return FeasibilityResult(status=FeasibilityStatus.FEASIBLE, check_name="dummy")

        d = Dummy()
        result = d({"x": 1})
        assert result.status is FeasibilityStatus.FEASIBLE

    def test_default_name_attribute(self):
        class Dummy(FeasibilityCheck):
            def check(self, context):
                return FeasibilityResult(status=FeasibilityStatus.FEASIBLE, check_name=self.name)

        assert Dummy().name == "FeasibilityCheck"

    def test_repr_contains_classname(self):
        chk = FileExistenceCheck(exists_func=lambda p: True)
        assert "FileExistenceCheck" in repr(chk)


# ---------------------------------------------------------------------------
# 4. FileExistenceCheck
# ---------------------------------------------------------------------------


class TestFileExistenceCheck:
    def test_all_files_exist(self):
        chk = FileExistenceCheck(exists_func=lambda p: True)
        r = chk.check({"required_files": ["a.txt", "b.txt"]})
        assert r.status is FeasibilityStatus.FEASIBLE
        assert r.metadata["checked"] == ["a.txt", "b.txt"]

    def test_missing_file_infeasible(self):
        chk = FileExistenceCheck(exists_func=lambda p: p == "a.txt")
        r = chk.check({"required_files": ["a.txt", "b.txt"]})
        assert r.status is FeasibilityStatus.INFEASIBLE
        assert "b.txt" in r.metadata["missing"]

    def test_empty_list_feasible(self):
        chk = FileExistenceCheck(exists_func=lambda p: False)
        r = chk.check({"required_files": []})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_missing_key_feasible(self):
        chk = FileExistenceCheck(exists_func=lambda p: False)
        r = chk.check({})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_none_value_feasible(self):
        chk = FileExistenceCheck(exists_func=lambda p: False)
        r = chk.check({"required_files": None})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_string_treated_as_single(self):
        chk = FileExistenceCheck(exists_func=lambda p: True)
        r = chk.check({"required_files": "only.txt"})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_custom_context_key(self):
        chk = FileExistenceCheck(context_key="inputs", exists_func=lambda p: False)
        r = chk.check({"inputs": ["x"]})
        assert r.status is FeasibilityStatus.INFEASIBLE

    def test_suggestion_present_on_failure(self):
        chk = FileExistenceCheck(exists_func=lambda p: False)
        r = chk.check({"required_files": ["x"]})
        assert r.suggestion != ""

    def test_uses_default_os_exists(self, tmp_path):
        f = tmp_path / "real.txt"
        f.write_text("hi")
        chk = FileExistenceCheck()
        r = chk.check({"required_files": [str(f)]})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_default_rejects_directory(self, tmp_path):
        # Regression: a FileExistenceCheck must not accept a directory where a
        # file is required (default now uses os.path.isfile, not exists).
        chk = FileExistenceCheck()
        r = chk.check({"required_files": [str(tmp_path)]})
        assert r.status is FeasibilityStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# 5. DirectoryExistenceCheck
# ---------------------------------------------------------------------------


class TestDirectoryExistenceCheck:
    def test_all_dirs_exist(self):
        chk = DirectoryExistenceCheck(exists_func=lambda p: True)
        r = chk.check({"required_dirs": ["/a", "/b"]})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_missing_dir_infeasible(self):
        chk = DirectoryExistenceCheck(exists_func=lambda p: p == "/a")
        r = chk.check({"required_dirs": ["/a", "/b"]})
        assert r.status is FeasibilityStatus.INFEASIBLE
        assert "/b" in r.metadata["missing"]

    def test_empty_dirs_feasible(self):
        chk = DirectoryExistenceCheck(exists_func=lambda p: False)
        r = chk.check({"required_dirs": []})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_missing_key_feasible(self):
        chk = DirectoryExistenceCheck(exists_func=lambda p: False)
        r = chk.check({})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_string_dir(self):
        chk = DirectoryExistenceCheck(exists_func=lambda p: True)
        r = chk.check({"required_dirs": "/single"})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_reason_mentions_directories(self):
        chk = DirectoryExistenceCheck(exists_func=lambda p: False)
        r = chk.check({"required_dirs": ["/x"]})
        assert "director" in r.reason

    def test_default_dir_exists(self, tmp_path):
        chk = DirectoryExistenceCheck()
        r = chk.check({"required_dirs": [str(tmp_path)]})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_default_rejects_file(self, tmp_path):
        # Regression: a DirectoryExistenceCheck must not accept a regular file
        # where a directory is required (default now uses os.path.isdir).
        f = tmp_path / "f.txt"
        f.write_text("x")
        chk = DirectoryExistenceCheck()
        r = chk.check({"required_dirs": [str(f)]})
        assert r.status is FeasibilityStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# 6. ToolAvailabilityCheck
# ---------------------------------------------------------------------------


class TestToolAvailabilityCheck:
    def test_all_tools_available(self):
        chk = ToolAvailabilityCheck(available_tools={"a", "b"})
        r = chk.check({"required_tools": ["a"]})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_missing_tool_infeasible(self):
        chk = ToolAvailabilityCheck(available_tools={"a"})
        r = chk.check({"required_tools": ["a", "z"]})
        assert r.status is FeasibilityStatus.INFEASIBLE
        assert "z" in r.metadata["missing"]

    def test_empty_required_feasible(self):
        chk = ToolAvailabilityCheck(available_tools=set())
        r = chk.check({"required_tools": []})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_no_required_key_feasible(self):
        chk = ToolAvailabilityCheck(available_tools={"a"})
        r = chk.check({})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_default_available_tools_empty(self):
        chk = ToolAvailabilityCheck()
        r = chk.check({"required_tools": ["a"]})
        assert r.status is FeasibilityStatus.INFEASIBLE

    def test_string_required(self):
        chk = ToolAvailabilityCheck(available_tools={"shell"})
        r = chk.check({"required_tools": "shell"})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_metadata_includes_available(self):
        chk = ToolAvailabilityCheck(available_tools={"x", "y"})
        r = chk.check({"required_tools": ["x"]})
        assert sorted(r.metadata["available"]) == ["x", "y"]

    def test_custom_context_key(self):
        chk = ToolAvailabilityCheck(available_tools={"t"}, context_key="tools")
        r = chk.check({"tools": ["t"]})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_none_available_tools_treated_as_empty(self):
        chk = ToolAvailabilityCheck(available_tools=None)
        r = chk.check({"required_tools": ["a"]})
        assert r.status is FeasibilityStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# 7. WritePathCheck
# ---------------------------------------------------------------------------


class TestWritePathCheck:
    def test_all_writable(self):
        chk = WritePathCheck(can_write_func=lambda p: True)
        r = chk.check({"write_paths": ["/tmp/a", "/tmp/b"]})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_unwritable_infeasible(self):
        chk = WritePathCheck(can_write_func=lambda p: p == "/ok")
        r = chk.check({"write_paths": ["/ok", "/bad"]})
        assert r.status is FeasibilityStatus.INFEASIBLE
        assert "/bad" in r.metadata["unwritable"]

    def test_empty_write_paths_feasible(self):
        chk = WritePathCheck(can_write_func=lambda p: False)
        r = chk.check({"write_paths": []})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_no_key_feasible(self):
        chk = WritePathCheck(can_write_func=lambda p: False)
        r = chk.check({})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_string_path(self):
        chk = WritePathCheck(can_write_func=lambda p: True)
        r = chk.check({"write_paths": "/tmp/x"})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_default_can_write_existing_file(self, tmp_path):
        f = tmp_path / "w.txt"
        f.write_text("x")
        chk = WritePathCheck()
        r = chk.check({"write_paths": [str(f)]})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_default_can_write_nonexistent_parent_ok(self, tmp_path):
        target = tmp_path / "newfile.txt"
        chk = WritePathCheck()
        r = chk.check({"write_paths": [str(target)]})
        assert r.status is FeasibilityStatus.FEASIBLE


# ---------------------------------------------------------------------------
# 8. RegexValidCheck
# ---------------------------------------------------------------------------


class TestRegexValidCheck:
    def test_valid_pattern(self):
        r = RegexValidCheck().check({"regex_pattern": r"\d+"})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_invalid_pattern_infeasible(self):
        r = RegexValidCheck().check({"regex_pattern": "((unclosed"})
        assert r.status is FeasibilityStatus.INFEASIBLE

    def test_multiple_patterns_one_invalid(self):
        r = RegexValidCheck().check({"regex_pattern": [r"\d+", "("]})
        assert r.status is FeasibilityStatus.INFEASIBLE
        assert len(r.metadata["invalid"]) == 1

    def test_multiple_patterns_all_valid(self):
        r = RegexValidCheck().check({"regex_pattern": [r"\d+", r"[a-z]"]})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_empty_patterns_feasible(self):
        r = RegexValidCheck().check({"regex_pattern": []})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_no_key_feasible(self):
        r = RegexValidCheck().check({})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_non_string_pattern(self):
        r = RegexValidCheck().check({"regex_pattern": 12345})
        # int is not a valid regex → TypeError caught → infeasible
        assert r.status is FeasibilityStatus.INFEASIBLE

    def test_custom_key(self):
        r = RegexValidCheck(context_key="pat").check({"pat": r"\w+"})
        assert r.status is FeasibilityStatus.FEASIBLE


# ---------------------------------------------------------------------------
# 9. NonEmptyStringCheck
# ---------------------------------------------------------------------------


class TestNonEmptyStringCheck:
    def test_all_present(self):
        chk = NonEmptyStringCheck(required_keys=["a", "b"])
        r = chk.check({"a": "x", "b": "y"})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_missing_key_infeasible(self):
        chk = NonEmptyStringCheck(required_keys=["a", "b"])
        r = chk.check({"a": "x"})
        assert r.status is FeasibilityStatus.INFEASIBLE
        assert "b" in r.metadata["missing"]

    def test_empty_string_infeasible(self):
        chk = NonEmptyStringCheck(required_keys=["a"])
        r = chk.check({"a": ""})
        assert r.status is FeasibilityStatus.INFEASIBLE

    def test_whitespace_only_infeasible_by_default(self):
        chk = NonEmptyStringCheck(required_keys=["a"])
        r = chk.check({"a": "   "})
        assert r.status is FeasibilityStatus.INFEASIBLE

    def test_whitespace_allowed(self):
        chk = NonEmptyStringCheck(required_keys=["a"], allow_whitespace=True)
        r = chk.check({"a": "   "})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_none_value_infeasible(self):
        chk = NonEmptyStringCheck(required_keys=["a"])
        r = chk.check({"a": None})
        assert r.status is FeasibilityStatus.INFEASIBLE

    def test_non_string_value_infeasible(self):
        chk = NonEmptyStringCheck(required_keys=["a"])
        r = chk.check({"a": 123})
        assert r.status is FeasibilityStatus.INFEASIBLE

    def test_no_required_keys_feasible(self):
        chk = NonEmptyStringCheck()
        r = chk.check({})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_empty_required_keys_list_feasible(self):
        chk = NonEmptyStringCheck(required_keys=[])
        r = chk.check({"a": "x"})
        assert r.status is FeasibilityStatus.FEASIBLE


# ---------------------------------------------------------------------------
# 10. FeasibilityGate — evaluation
# ---------------------------------------------------------------------------


class TestGateEvaluateStep:
    def test_single_step_all_pass(self):
        gate = FeasibilityGate(
            [
                NonEmptyStringCheck(required_keys=["action"]),
                ToolAvailabilityCheck(available_tools={"read"}),
            ]
        )
        results = gate.evaluate_step({"action": "read", "required_tools": ["read"]})
        assert len(results) == 2
        assert all(r.status is FeasibilityStatus.FEASIBLE for r in results)

    def test_single_step_one_blocker(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["action"])])
        results = gate.evaluate_step({"action": ""})
        assert len(results) == 1
        assert results[0].status is FeasibilityStatus.INFEASIBLE

    def test_none_step_treated_as_empty(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["a"])])
        results = gate.evaluate_step(None)  # type: ignore[arg-type]
        assert results[0].status is FeasibilityStatus.INFEASIBLE

    def test_non_dict_step_treated_as_empty(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["a"])])
        results = gate.evaluate_step("not a dict")  # type: ignore[arg-type]
        assert results[0].status is FeasibilityStatus.INFEASIBLE

    def test_check_raising_exception_becomes_uncertain(self):
        class Boom(FeasibilityCheck):
            name = "boom"

            def check(self, context):
                raise RuntimeError("kaboom")

        gate = FeasibilityGate([Boom()])
        results = gate.evaluate_step({})
        assert len(results) == 1
        assert results[0].status is FeasibilityStatus.UNCERTAIN
        assert "kaboom" in results[0].reason

    def test_results_preserve_check_order(self):
        gate = FeasibilityGate(
            [
                NonEmptyStringCheck(required_keys=["a"], name="first"),
                NonEmptyStringCheck(required_keys=["b"], name="second"),
            ]
        )
        results = gate.evaluate_step({"a": "x", "b": "y"})
        assert [r.check_name for r in results] == ["first", "second"]


class TestGateEvaluate:
    def test_multi_step_flat_results(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["a"])])
        steps = [{"a": "x"}, {"a": ""}, {}]
        results = gate.evaluate(steps)
        assert len(results) == 3
        assert results[0].status is FeasibilityStatus.FEASIBLE
        assert results[1].status is FeasibilityStatus.INFEASIBLE
        assert results[2].status is FeasibilityStatus.INFEASIBLE

    def test_empty_steps_returns_empty(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["a"])])
        assert gate.evaluate([]) == []

    def test_none_steps_returns_empty(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["a"])])
        assert gate.evaluate(None) == []  # type: ignore[arg-type]

    def test_no_checks_returns_empty(self):
        gate = FeasibilityGate([])
        assert gate.evaluate([{"a": 1}]) == []

    def test_results_count_equals_steps_times_checks(self):
        gate = FeasibilityGate(
            [
                NonEmptyStringCheck(required_keys=["a"]),
                NonEmptyStringCheck(required_keys=["b"]),
            ]
        )
        results = gate.evaluate([{"a": "1", "b": "2"}, {"a": "3", "b": "4"}])
        assert len(results) == 4


# ---------------------------------------------------------------------------
# 11. FeasibilityGate — blocking detection
# ---------------------------------------------------------------------------


class TestGateBlocking:
    def test_get_blocking_results_filters_infeasible(self):
        results = [
            FeasibilityResult(FeasibilityStatus.FEASIBLE, "a"),
            FeasibilityResult(FeasibilityStatus.INFEASIBLE, "b"),
            FeasibilityResult(FeasibilityStatus.UNCERTAIN, "c"),
        ]
        blockers = FeasibilityGate.get_blocking_results(results)
        assert len(blockers) == 1
        assert blockers[0].check_name == "b"

    def test_get_blocking_results_empty(self):
        assert FeasibilityGate.get_blocking_results([]) == []

    def test_get_blocking_results_all_feasible(self):
        results = [FeasibilityResult(FeasibilityStatus.FEASIBLE, "a")]
        assert FeasibilityGate.get_blocking_results(results) == []

    def test_has_blockers_true(self):
        results = [FeasibilityResult(FeasibilityStatus.INFEASIBLE, "a")]
        assert FeasibilityGate.has_blockers(results) is True

    def test_has_blockers_false_all_feasible(self):
        results = [
            FeasibilityResult(FeasibilityStatus.FEASIBLE, "a"),
            FeasibilityResult(FeasibilityStatus.UNCERTAIN, "b"),
        ]
        assert FeasibilityGate.has_blockers(results) is False

    def test_has_blockers_empty(self):
        assert FeasibilityGate.has_blockers([]) is False

    def test_add_check_appends(self):
        gate = FeasibilityGate([])
        gate.add_check(NonEmptyStringCheck(required_keys=["a"]))
        assert len(gate) == 1


# ---------------------------------------------------------------------------
# 12. FeasibilityGate — summarize
# ---------------------------------------------------------------------------


class TestGateSummarize:
    def test_empty_results_message(self):
        s = FeasibilityGate.summarize([])
        assert "No feasibility checks" in s

    def test_all_feasible_summary(self):
        results = [
            FeasibilityResult(FeasibilityStatus.FEASIBLE, "a"),
            FeasibilityResult(FeasibilityStatus.FEASIBLE, "b"),
        ]
        s = FeasibilityGate.summarize(results)
        assert "2 feasible" in s
        assert "No blockers" in s

    def test_summary_with_blockers(self):
        results = [
            FeasibilityResult(FeasibilityStatus.FEASIBLE, "a"),
            FeasibilityResult(FeasibilityStatus.INFEASIBLE, "b", reason="bad"),
        ]
        s = FeasibilityGate.summarize(results)
        assert "1 infeasible" in s
        assert "Blockers" in s
        assert "[b]" in s
        assert "bad" in s

    def test_summary_counts_uncertain(self):
        results = [FeasibilityResult(FeasibilityStatus.UNCERTAIN, "u")]
        s = FeasibilityGate.summarize(results)
        assert "1 uncertain" in s

    def test_summary_counts_total(self):
        results = [
            FeasibilityResult(FeasibilityStatus.FEASIBLE, "a"),
            FeasibilityResult(FeasibilityStatus.INFEASIBLE, "b"),
            FeasibilityResult(FeasibilityStatus.UNCERTAIN, "c"),
        ]
        s = FeasibilityGate.summarize(results)
        assert "3 check(s)" in s


# ---------------------------------------------------------------------------
# 13. Integration / end-to-end scenarios
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_full_pipeline_no_blockers(self):
        gate = FeasibilityGate(
            [
                FileExistenceCheck(exists_func=lambda p: True),
                ToolAvailabilityCheck(available_tools={"tool1"}),
                WritePathCheck(can_write_func=lambda p: True),
                NonEmptyStringCheck(required_keys=["action"]),
            ]
        )
        steps = [
            {"action": "do", "required_files": ["f"], "required_tools": ["tool1"], "write_paths": ["/o"]},
        ]
        results = gate.evaluate(steps)
        assert not FeasibilityGate.has_blockers(results)
        assert len(results) == 4

    def test_full_pipeline_with_blockers(self):
        gate = FeasibilityGate(
            [
                FileExistenceCheck(exists_func=lambda p: False),
                ToolAvailabilityCheck(available_tools=set()),
                NonEmptyStringCheck(required_keys=["action"]),
            ]
        )
        steps = [{"action": "", "required_files": ["f"], "required_tools": ["t"]}]
        results = gate.evaluate(steps)
        blockers = FeasibilityGate.get_blocking_results(results)
        assert len(blockers) == 3
        summary = FeasibilityGate.summarize(results)
        assert "3 infeasible" in summary

    def test_mixed_step_outcomes(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["action"])])
        steps = [{"action": "ok"}, {"action": ""}, {"action": "fine"}]
        results = gate.evaluate(steps)
        blockers = FeasibilityGate.get_blocking_results(results)
        assert len(blockers) == 1

    def test_results_json_roundtrip(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["a"])])
        results = gate.evaluate([{"a": ""}])
        payload = json.dumps([r.to_dict() for r in results])
        restored = [FeasibilityResult.from_dict(d) for d in json.loads(payload)]
        assert restored[0].status is FeasibilityStatus.INFEASIBLE
        assert restored[0].check_name == "NonEmptyStringCheck"

    def test_gate_repr(self):
        gate = FeasibilityGate([NonEmptyStringCheck(required_keys=["a"])])
        assert "FeasibilityGate" in repr(gate)

    def test_check_callable_protocol(self):
        chk = NonEmptyStringCheck(required_keys=["a"])
        r = chk({"a": "x"})
        assert r.status is FeasibilityStatus.FEASIBLE

    def test_custom_name_override(self):
        chk = NonEmptyStringCheck(required_keys=["a"], name="my_check")
        r = chk.check({"a": ""})
        assert r.check_name == "my_check"
