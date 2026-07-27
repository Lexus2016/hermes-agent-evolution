"""Tests for evolution_probe_set.py (#1355, child of #1308)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.evolution_probe_set import (
    DEFAULT_PROBE_DIR,
    Probe,
    ProbeSet,
    SCHEMA_VERSION,
    _probe_path,
    load_probe_set,
    save_probe_set,
)


# ---------------------------------------------------------------------------
# Probe dataclass
# ---------------------------------------------------------------------------


class TestProbe:
    def test_basic_creation(self):
        p = Probe(id="p1", input="Sum 2+2", expected_behavior="Returns 4")
        assert p.id == "p1"
        assert p.input == "Sum 2+2"
        assert p.expected_behavior == "Returns 4"

    def test_to_dict(self):
        p = Probe(id="p1", input="test", expected_behavior="ok")
        d = p.to_dict()
        assert d == {"id": "p1", "input": "test", "expected_behavior": "ok"}

    def test_from_dict(self):
        d = {"id": "p2", "input": "hello", "expected_behavior": "greets"}
        p = Probe.from_dict(d)
        assert p.id == "p2"
        assert p.input == "hello"

    def test_from_dict_missing_fields_defaults_to_empty(self):
        p = Probe.from_dict({})
        assert p.id == ""
        assert p.input == ""
        assert p.expected_behavior == ""

    def test_roundtrip(self):
        original = Probe(id="x", input="in", expected_behavior="out")
        restored = Probe.from_dict(original.to_dict())
        assert restored == original


# ---------------------------------------------------------------------------
# ProbeSet dataclass
# ---------------------------------------------------------------------------


class TestProbeSet:
    def test_basic_creation(self):
        ps = ProbeSet(skill_name="my-skill")
        assert ps.skill_name == "my-skill"
        assert ps.probes == []
        assert ps.schema_version == SCHEMA_VERSION

    def test_normalizes_raw_dicts_in_probes(self):
        ps = ProbeSet(
            skill_name="s",
            probes=[
                {"id": "a", "input": "i1", "expected_behavior": "b1"},
                Probe(id="b", input="i2", expected_behavior="b2"),
            ],
        )
        assert all(isinstance(p, Probe) for p in ps.probes)
        assert ps.probes[0].id == "a"
        assert ps.probes[1].id == "b"

    def test_probe_ids(self):
        ps = ProbeSet(
            skill_name="s",
            probes=[
                Probe(id="x", input="", expected_behavior=""),
                Probe(id="y", input="", expected_behavior=""),
            ],
        )
        assert ps.probe_ids == ["x", "y"]

    def test_to_dict_includes_metadata(self):
        ps = ProbeSet(
            skill_name="test",
            probes=[Probe(id="p1", input="i", expected_behavior="e")],
        )
        d = ps.to_dict()
        assert d["skill_name"] == "test"
        assert d["schema_version"] == SCHEMA_VERSION
        assert d["probe_count"] == 1
        assert len(d["probes"]) == 1

    def test_from_dict(self):
        data = {
            "skill_name": "my-skill",
            "schema_version": "1.0",
            "probes": [
                {"id": "p1", "input": "q1", "expected_behavior": "r1"},
                {"id": "p2", "input": "q2", "expected_behavior": "r2"},
            ],
        }
        ps = ProbeSet.from_dict(data)
        assert ps.skill_name == "my-skill"
        assert len(ps.probes) == 2
        assert ps.probes[0].id == "p1"

    def test_from_dict_requires_skill_name(self):
        with pytest.raises(ValueError, match="skill_name"):
            ProbeSet.from_dict({"probes": []})

    def test_from_dict_rejects_non_dict(self):
        with pytest.raises(ValueError):
            ProbeSet.from_dict("not a dict")  # type: ignore[arg-type]

    def test_from_dict_tolerates_missing_probes(self):
        ps = ProbeSet.from_dict({"skill_name": "ok"})
        assert ps.probes == []

    def test_roundtrip(self):
        original = ProbeSet(
            skill_name="rt",
            probes=[
                Probe(id="a", input="ia", expected_behavior="ea"),
                Probe(id="b", input="ib", expected_behavior="eb"),
            ],
        )
        restored = ProbeSet.from_dict(original.to_dict())
        assert restored.skill_name == original.skill_name
        assert restored.probe_ids == original.probe_ids
        assert restored.probes == original.probes


# ---------------------------------------------------------------------------
# Filesystem storage
# ---------------------------------------------------------------------------


class TestStorage:
    def test_save_and_load_roundtrip(self, tmp_path: Path):
        ps = ProbeSet(
            skill_name="test-skill",
            probes=[Probe(id="p1", input="input1", expected_behavior="behavior1")],
        )
        path = save_probe_set(ps, store_dir=tmp_path)
        assert path.exists()
        assert path.name == "test-skill.json"

        loaded = load_probe_set("test-skill", store_dir=tmp_path)
        assert loaded is not None
        assert loaded.skill_name == "test-skill"
        assert loaded.probe_ids == ["p1"]

    def test_load_returns_none_when_missing(self, tmp_path: Path):
        result = load_probe_set("nonexistent", store_dir=tmp_path)
        assert result is None

    def test_load_returns_none_on_corrupt_json(self, tmp_path: Path):
        path = _probe_path("corrupt", store_dir=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NOT JSON {{{", encoding="utf-8")
        result = load_probe_set("corrupt", store_dir=tmp_path)
        assert result is None

    def test_save_creates_directory(self, tmp_path: Path):
        nested = tmp_path / "a" / "b" / "c"
        ps = ProbeSet(skill_name="s", probes=[])
        path = save_probe_set(ps, store_dir=nested)
        assert path.exists()
        assert nested.exists()

    def test_probe_path_sanitizes_skill_name(self):
        path = _probe_path("dir/sub skill", store_dir=Path("/tmp"))
        assert "/" not in path.name
        assert " " not in path.name

    def test_save_multiple_probe_sets(self, tmp_path: Path):
        for name in ["skill-a", "skill-b", "skill-c"]:
            save_probe_set(ProbeSet(skill_name=name), store_dir=tmp_path)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 3


# ---------------------------------------------------------------------------
# Integration: JSON on disk is valid
# ---------------------------------------------------------------------------


class TestJSONIntegrity:
    def test_saved_file_is_valid_json(self, tmp_path: Path):
        ps = ProbeSet(
            skill_name="json-test",
            probes=[
                Probe(id="p1", input="What is 2+2?", expected_behavior="Returns 4"),
                Probe(
                    id="p2", input="Reverse 'abc'", expected_behavior="Returns 'cba'"
                ),
            ],
        )
        path = save_probe_set(ps, store_dir=tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["skill_name"] == "json-test"
        assert raw["probe_count"] == 2
        assert raw["probes"][0]["id"] == "p1"
