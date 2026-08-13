# -*- coding: utf-8 -*-
"""Tests for trajectory-attribution provenance on skill promotion (#2288).

PoisonedEvolution (arXiv:2608.05563) shows a skill that looks causally useful
and recurrent can still be poisoned via cross-trajectory credit assignment.
Treating provisional→trusted promotion as a security boundary means recording
which trajectories contributed to a skill's content, and refusing to trust a
promotion that carries no attribution at all.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_skill_version import (  # noqa: E402
    current_version,
    load_versions,
    main,
    provenance_ok,
    record_promotion,
)


class TestTrajectoryAttribution:
    def test_trajectory_refs_are_stored(self, tmp_path):
        v = record_promotion("s", trajectory_refs=["t1", "t2"], store_dir=tmp_path)
        assert v.trajectory_refs == ["t1", "t2"]

    def test_trajectory_refs_round_trip(self, tmp_path):
        record_promotion("s", trajectory_refs=["t1", "t2"], store_dir=tmp_path)
        loaded = load_versions("s", store_dir=tmp_path)[0]
        assert loaded.trajectory_refs == ["t1", "t2"]

    def test_trajectory_refs_serialized_in_dict(self, tmp_path):
        v = record_promotion("s", trajectory_refs=["t1"], store_dir=tmp_path)
        assert v.to_dict()["trajectory_refs"] == ["t1"]

    def test_default_is_empty(self, tmp_path):
        v = record_promotion("s", store_dir=tmp_path)
        assert v.trajectory_refs == []

    def test_old_records_without_field_load_as_empty(self, tmp_path):
        """Backward compat: a promotion recorded before this field existed
        must load with empty trajectory_refs, not crash."""
        p = tmp_path / "s.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"skill": "s", "version": 1}) + "\n")
        assert load_versions("s", store_dir=tmp_path)[0].trajectory_refs == []


class TestProvenanceGate:
    def test_ok_when_attribution_present(self, tmp_path):
        v = record_promotion("s", trajectory_refs=["t1"], store_dir=tmp_path)
        assert provenance_ok(v) is True

    def test_not_ok_when_no_attribution(self, tmp_path):
        v = record_promotion("s", store_dir=tmp_path)
        assert provenance_ok(v) is False

    def test_cli_records_trajectories(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert main(["record", "s", "--trajectories", "t1,t2"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["trajectory_refs"] == ["t1", "t2"]
        store = tmp_path / "skill_versions"
        assert current_version("s", store_dir=store).trajectory_refs == ["t1", "t2"]
