# -*- coding: utf-8 -*-
"""Tests for the canonical skill version line (issue #1448, Child D of #1308)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_skill_version import (  # noqa: E402
    SkillVersion,
    current_version,
    format_current,
    load_versions,
    main,
    record_promotion,
    rollback_target,
)


class TestRecordPromotion:
    def test_first_promotion_is_version_one(self, tmp_path):
        v = record_promotion("my-skill", store_dir=tmp_path)
        assert v.version == 1

    def test_versions_increment(self, tmp_path):
        for expected in (1, 2, 3):
            assert record_promotion("s", store_dir=tmp_path).version == expected

    def test_version_derived_from_disk_not_passed_in(self, tmp_path):
        """Two callers cannot disagree about which number is next."""
        record_promotion("s", store_dir=tmp_path)
        record_promotion("s", store_dir=tmp_path)
        assert current_version("s", store_dir=tmp_path).version == 2

    def test_skills_are_independent(self, tmp_path):
        record_promotion("a", store_dir=tmp_path)
        record_promotion("a", store_dir=tmp_path)
        assert record_promotion("b", store_dir=tmp_path).version == 1

    def test_evidence_is_stored(self, tmp_path):
        v = record_promotion(
            "s", flip_verdict="promote", fixes=["p1", "p2"], regressions=[],
            diff_ref="abc1234", critic_ref="critic-9", note="why",
            store_dir=tmp_path,
        )
        assert v.flip_verdict == "promote"
        assert v.fixes == ["p1", "p2"]
        assert v.diff_ref == "abc1234"
        assert v.critic_ref == "critic-9"

    def test_history_is_append_only(self, tmp_path):
        """The history IS the artifact — overwriting would leave the current
        version with no record of what it replaced."""
        record_promotion("s", note="first", store_dir=tmp_path)
        record_promotion("s", note="second", store_dir=tmp_path)
        assert [v.note for v in load_versions("s", store_dir=tmp_path)] == ["first", "second"]

    def test_path_is_safe_for_awkward_names(self, tmp_path):
        record_promotion("group/my skill", store_dir=tmp_path)
        assert (tmp_path / "group_my_skill.jsonl").exists()


class TestCurrentVersion:
    def test_none_when_never_promoted(self, tmp_path):
        assert current_version("nope", store_dir=tmp_path) is None

    def test_highest_wins_not_last_written(self, tmp_path):
        """An out-of-order append — a retried promotion, a merged history —
        must not make an older version look current."""
        p = tmp_path / "s.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            for n in (1, 3, 2):
                fh.write(json.dumps({"skill": "s", "version": n}) + "\n")
        assert current_version("s", store_dir=tmp_path).version == 3


class TestRollbackTarget:
    def test_none_with_a_single_version(self, tmp_path):
        record_promotion("s", store_dir=tmp_path)
        assert rollback_target("s", store_dir=tmp_path) is None

    def test_none_when_never_promoted(self, tmp_path):
        assert rollback_target("s", store_dir=tmp_path) is None

    def test_returns_the_one_below_current(self, tmp_path):
        record_promotion("s", note="v1", store_dir=tmp_path)
        record_promotion("s", note="v2", store_dir=tmp_path)
        record_promotion("s", note="v3", store_dir=tmp_path)
        target = rollback_target("s", store_dir=tmp_path)
        assert target.version == 2
        assert target.note == "v2"

    def test_carries_the_evidence_that_approved_it(self, tmp_path):
        """Reverting BY VERSION means knowing what approved that version."""
        record_promotion("s", flip_verdict="promote", diff_ref="old", store_dir=tmp_path)
        record_promotion("s", flip_verdict="promote", diff_ref="new", store_dir=tmp_path)
        assert rollback_target("s", store_dir=tmp_path).diff_ref == "old"


class TestLoadRobustness:
    def test_malformed_lines_skipped(self, tmp_path):
        record_promotion("s", store_dir=tmp_path)
        with open(tmp_path / "s.jsonl", "a", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"no": "skill key"}) + "\n")
        assert len(load_versions("s", store_dir=tmp_path)) == 1

    def test_a_corrupt_entry_does_not_hide_the_rest(self, tmp_path):
        """The history is what a rollback depends on."""
        record_promotion("s", store_dir=tmp_path)
        with open(tmp_path / "s.jsonl", "a", encoding="utf-8") as fh:
            fh.write("{broken\n")
        record_promotion("s", store_dir=tmp_path)
        assert [v.version for v in load_versions("s", store_dir=tmp_path)] == [1, 2]

    def test_missing_file_is_empty(self, tmp_path):
        assert load_versions("absent", store_dir=tmp_path) == []

    def test_round_trip(self, tmp_path):
        record_promotion("s", flip_verdict="promote", fixes=["a"], store_dir=tmp_path)
        loaded = load_versions("s", store_dir=tmp_path)[0]
        assert isinstance(loaded, SkillVersion)
        assert loaded.fixes == ["a"]


class TestFormatCurrent:
    def test_answers_the_question(self, tmp_path):
        record_promotion("s", flip_verdict="promote", fixes=["p1"], diff_ref="abc",
                         store_dir=tmp_path)
        line = format_current("s", store_dir=tmp_path)
        assert "v1" in line
        assert "promote" in line
        assert "abc" in line

    def test_says_so_when_nothing_recorded(self, tmp_path):
        assert "no recorded promotions" in format_current("s", store_dir=tmp_path)


class TestCli:
    def test_record_then_current(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert main(["record", "s", "--verdict", "promote", "--diff", "abc"]) == 0
        capsys.readouterr()
        assert main(["current", "s"]) == 0
        assert json.loads(capsys.readouterr().out)["version"] == 1

    def test_current_exits_one_when_unknown(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert main(["current", "never-promoted"]) == 1
        capsys.readouterr()

    def test_rollback_exits_one_with_a_single_version(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        main(["record", "s"])
        capsys.readouterr()
        assert main(["rollback", "s"]) == 1
        capsys.readouterr()

    def test_rollback_returns_the_previous(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        main(["record", "s", "--diff", "old"])
        main(["record", "s", "--diff", "new"])
        capsys.readouterr()
        assert main(["rollback", "s"]) == 0
        assert json.loads(capsys.readouterr().out)["diff_ref"] == "old"

    def test_history_lists_everything(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        main(["record", "s"])
        main(["record", "s"])
        capsys.readouterr()
        assert main(["history", "s"]) == 0
        assert len(json.loads(capsys.readouterr().out)) == 2

    def test_bad_command_exits_two(self, capsys):
        assert main(["nonsense", "s"]) == 2
        capsys.readouterr()

    def test_missing_skill_exits_two(self, capsys):
        assert main(["current"]) == 2
        capsys.readouterr()

    def test_help_exits_zero(self, capsys):
        assert main(["--help"]) == 0
        assert "usage" in capsys.readouterr().out
