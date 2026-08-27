"""Tests for scripts/evolution_trace_holdout.py — deterministic hold-back split (#3226)."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_trace_holdout import (  # noqa: E402
    build_holdout,
    discover_sessions,
    select_holdout,
)


def _session(tmp_path: Path, name: str, *, age_days: int = 0):
    p = tmp_path / f"{name}.jsonl"
    p.write_text(json.dumps({"role": "session_meta"}) + "\n", encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        import os

        os.utime(p, (old, old))
    return p


def _dump(
    tmp_path: Path,
    sid: str,
    msg_count: int,
    *,
    age_days: int = 0,
    session_id: str | None = None,
):
    # ``session_id`` is decoupled from the filename so a test can create two
    # dumps of the SAME session (same session_id) under different filenames —
    # the dedup case. Default: session_id == filename stem.
    p = tmp_path / f"request_dump_{sid}.json"
    p.write_text(
        json.dumps({
            "session_id": session_id if session_id is not None else sid,
            "request": {"messages": list(range(msg_count))},
        }),
        encoding="utf-8",
    )
    if age_days:
        old = time.time() - age_days * 86400
        import os

        os.utime(p, (old, old))
    return p


class TestDiscoverSessions:
    def test_finds_jsonl_and_request_dumps(self, tmp_path):
        _session(tmp_path, "s1")
        _session(tmp_path, "s2")
        _dump(tmp_path, "s3", 2)
        assert discover_sessions(tmp_path, window_days=7) == ["s1", "s2", "s3"]

    def test_excludes_old_sessions(self, tmp_path):
        _session(tmp_path, "recent")
        _session(tmp_path, "old", age_days=30)
        assert discover_sessions(tmp_path, window_days=7) == ["recent"]

    def test_dedups_request_dumps_keeping_largest(self, tmp_path):
        _dump(tmp_path, "dup", 2)
        _dump(tmp_path, "dup_final", 5, session_id="dup")  # same session_id
        # The dump with larger message count wins; id still appears once.
        assert discover_sessions(tmp_path, window_days=7) == ["dup"]

    def test_jsonl_takes_precedence_over_dump_name_collision(self, tmp_path):
        # *.jsonl stem matches dump session_id; both exist but dedup should keep
        # the session once.
        _session(tmp_path, "both")
        _dump(tmp_path, "both", 1)
        assert discover_sessions(tmp_path, window_days=7) == ["both"]


class TestSelectHoldout:
    def test_deterministic(self):
        ids = [f"s{i}" for i in range(10)]
        h1, t1 = select_holdout(ids, holdout_fraction=0.2, seed=7)
        h2, t2 = select_holdout(ids, holdout_fraction=0.2, seed=7)
        assert h1 == h2
        assert t1 == t2
        assert sorted(h1 + t1) == ids

    @pytest.mark.parametrize("seed", [0, 1, 42])
    def test_different_seeds_different_splits(self, seed):
        ids = [f"s{i}" for i in range(20)]
        h, _ = select_holdout(ids, holdout_fraction=0.25, seed=seed)
        assert len(h) == 5
        assert sorted(h) != h  # not the same as sorted ids

    def test_fraction_rounds_to_zero_gives_empty_holdout(self):
        ids = ["s1", "s2"]
        h, t = select_holdout(ids, holdout_fraction=0.05, seed=0)
        assert h == []
        assert t == ids

    def test_full_population_holdout(self):
        ids = ["s1"]
        h, t = select_holdout(ids, holdout_fraction=1.0, seed=0)
        assert h == ids
        assert t == []


class TestBuildHoldout:
    def test_train_excludes_holdout(self, tmp_path):
        for n in ("a", "b", "c", "d", "e"):
            _session(tmp_path, n)
        out = build_holdout(tmp_path, window_days=7, holdout_fraction=0.4, seed=3)
        assert len(out["holdout"]) == 2
        assert len(out["train"]) == 3
        assert not set(out["holdout"]) & set(out["train"])
        assert out["meta"]["total_sessions"] == 5

    def test_meta_roundtrip(self, tmp_path):
        _session(tmp_path, "only")
        out = build_holdout(tmp_path, window_days=7, holdout_fraction=0.1, seed=0)
        assert out["meta"]["window_days"] == 7
        assert out["meta"]["holdout_fraction"] == 0.1
        assert out["meta"]["seed"] == 0
        assert (
            out["meta"]["holdout_count"] + out["meta"]["train_count"]
            == out["meta"]["total_sessions"]
        )
