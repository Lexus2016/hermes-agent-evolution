# -*- coding: utf-8 -*-
"""Tests for the example-level flip gate (issue #1446, Child B of #1308)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_flip_gate import (  # noqa: E402
    BLOCK,
    FIX,
    HELD,
    MISSING_AFTER,
    MISSING_BEFORE,
    PROMOTE,
    REGRESSION,
    STILL_BROKEN,
    _passed,
    compare_runs,
    flip_verdict,
    format_table,
    main,
)


class TestPassedCoercion:
    """A grader may emit any of several shapes; an unreadable one must not
    look like a pass, or a regression slips through as P->P."""

    def test_bare_bool(self):
        assert _passed(True) is True
        assert _passed(False) is False

    def test_dict_variants(self):
        for key in ("passed", "pass", "ok", "success"):
            assert _passed({key: True}) is True
            assert _passed({key: False}) is False

    def test_status_string_in_dict(self):
        assert _passed({"status": "pass"}) is True
        assert _passed({"status": "PASSED"}) is True
        assert _passed({"status": "error"}) is False

    def test_bare_string(self):
        assert _passed("pass") is True
        assert _passed("fail") is False

    def test_unrecognised_is_a_fail(self):
        """Fail closed — an unparseable result must never read as a pass."""
        assert _passed(None) is False
        assert _passed(42) is False
        assert _passed({}) is False
        assert _passed({"weird": "shape"}) is False


class TestCompareRuns:
    def test_four_cells(self):
        table = compare_runs(
            before={"a": True, "b": False, "c": True, "d": False},
            after={"a": False, "b": True, "c": True, "d": False},
        )
        assert table.cells["a"] == REGRESSION
        assert table.cells["b"] == FIX
        assert table.cells["c"] == HELD
        assert table.cells["d"] == STILL_BROKEN

    def test_counts_and_id_lists(self):
        table = compare_runs(
            before={"a": True, "b": True, "c": False},
            after={"a": False, "b": False, "c": True},
        )
        assert table.count(REGRESSION) == 2
        assert table.regressions == ["a", "b"]
        assert table.fixes == ["c"]

    def test_missing_probes_are_marked_not_dropped(self):
        table = compare_runs(before={"a": True, "gone": True}, after={"a": True, "new": True})
        assert table.cells["gone"] == MISSING_AFTER
        assert table.cells["new"] == MISSING_BEFORE
        assert len(table.cells) == 3

    def test_empty_runs(self):
        assert compare_runs({}, {}).cells == {}


class TestFlipVerdict:
    def test_clean_run_promotes(self):
        table = compare_runs({"a": True, "b": False}, {"a": True, "b": True})
        v = flip_verdict(table)
        assert v.verdict == PROMOTE
        assert v.blocked is False
        assert v.fixes == ["b"]

    def test_single_regression_blocks_by_default(self):
        table = compare_runs({"a": True}, {"a": False})
        v = flip_verdict(table)
        assert v.verdict == BLOCK
        assert v.regressions == ["a"]

    def test_gains_do_not_offset_regressions(self):
        """The whole premise: an aggregate can rise while something breaks."""
        table = compare_runs(
            before={"a": True, "b": False, "c": False, "d": False},
            after={"a": False, "b": True, "c": True, "d": True},
        )
        v = flip_verdict(table)
        assert v.verdict == BLOCK
        assert len(v.fixes) == 3 and len(v.regressions) == 1

    def test_threshold_allows_slack_when_asked(self):
        table = compare_runs({"a": True, "b": True}, {"a": False, "b": False})
        assert flip_verdict(table, max_regressions=2).verdict == PROMOTE
        assert flip_verdict(table, max_regressions=1).verdict == BLOCK

    def test_missing_probe_blocks(self):
        """A frozen set losing a probe makes the comparison unsound, not merely
        incomplete — it could be hiding the regression."""
        table = compare_runs({"a": True, "gone": True}, {"a": True})
        v = flip_verdict(table)
        assert v.verdict == BLOCK
        assert "gone" in v.reason

    def test_missing_probe_can_be_allowed_explicitly(self):
        table = compare_runs({"a": True, "gone": True}, {"a": True})
        assert flip_verdict(table, block_on_missing=False).verdict == PROMOTE

    def test_new_probe_alone_does_not_block(self):
        """An added probe has no baseline, but nothing regressed."""
        table = compare_runs({"a": True}, {"a": True, "new": False})
        assert flip_verdict(table).verdict == PROMOTE

    def test_reason_names_the_regressed_probes(self):
        table = compare_runs({"x": True}, {"x": False})
        assert "x" in flip_verdict(table).reason


class TestFormatTable:
    def test_summary_names_verdict_and_ids(self):
        table = compare_runs({"a": True, "b": False}, {"a": False, "b": True})
        out = format_table(table, flip_verdict(table))
        assert "BLOCK" in out
        assert "regressed: a" in out
        assert "fixed: b" in out


class TestCli:
    @staticmethod
    def _write(tmp_path, name, data):
        p = tmp_path / name
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    def test_exit_zero_on_promote(self, tmp_path, capsys):
        b = self._write(tmp_path, "b.json", {"a": True})
        a = self._write(tmp_path, "a.json", {"a": True})
        assert main([b, a]) == 0
        assert json.loads(capsys.readouterr().out)["verdict"] == PROMOTE

    def test_exit_one_on_block(self, tmp_path, capsys):
        b = self._write(tmp_path, "b.json", {"a": True})
        a = self._write(tmp_path, "a.json", {"a": False})
        assert main([b, a]) == 1
        assert json.loads(capsys.readouterr().out)["verdict"] == BLOCK

    def test_max_regressions_flag(self, tmp_path, capsys):
        b = self._write(tmp_path, "b.json", {"a": True})
        a = self._write(tmp_path, "a.json", {"a": False})
        assert main([b, a, "--max-regressions", "1"]) == 0
        capsys.readouterr()

    def test_allow_missing_flag(self, tmp_path, capsys):
        b = self._write(tmp_path, "b.json", {"a": True, "gone": True})
        a = self._write(tmp_path, "a.json", {"a": True})
        assert main([b, a]) == 1
        capsys.readouterr()
        assert main([b, a, "--allow-missing"]) == 0
        capsys.readouterr()

    def test_bad_input_exits_two(self, tmp_path, capsys):
        assert main(["nope.json", "also-nope.json"]) == 2
        capsys.readouterr()

    def test_wrong_arg_count_exits_two(self, capsys):
        assert main(["only-one.json"]) == 2
        capsys.readouterr()

    def test_non_object_payload_exits_two(self, tmp_path, capsys):
        p = self._write(tmp_path, "list.json", ["not", "an", "object"])
        assert main([p, p]) == 2
        capsys.readouterr()

    def test_help_exits_zero(self, capsys):
        assert main(["--help"]) == 0
        assert "usage" in capsys.readouterr().out
