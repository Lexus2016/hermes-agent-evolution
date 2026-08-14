"""Content-hash dispatch-ledger tests for the Hydra gate (#2425).

A rewritten-but-identical upstream file (newer mtime, same bytes) must NOT
re-fire an edge already dispatched today; only a real content change may.
Legacy mtime-only ledger entries keep the old semantics until refreshed.
"""

from pathlib import Path

from scripts import evolution_hydra_gate as hydra

EDGE = "analysis→implementation"


def _write_stage(evo_dir: Path, stage: str, content: str) -> None:
    d = evo_dir / stage
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{hydra._today()}.md").write_text(content, encoding="utf-8")


def _record(tmp_path: Path) -> None:
    """Record a dispatch for EDGE at analysis' current mtime + content hash."""
    hydra._record_dispatch(
        tmp_path,
        EDGE,
        hydra._latest_output(tmp_path, "analysis"),
        hydra._stage_content_hash(tmp_path, "analysis"),
    )


def _dispatched(tmp_path: Path) -> bool:
    return hydra._already_dispatched_today(
        tmp_path,
        EDGE,
        hydra._latest_output(tmp_path, "analysis"),
        hydra._stage_content_hash(tmp_path, "analysis"),
    )


class TestContentHashLedger:
    def test_same_content_rewrite_stays_suppressed(self, tmp_path):
        _write_stage(tmp_path, "analysis", "selection v1")
        _record(tmp_path)
        _write_stage(tmp_path, "analysis", "selection v1")  # identical bytes
        assert _dispatched(tmp_path) is True

    def test_real_content_change_re_fires(self, tmp_path):
        _write_stage(tmp_path, "analysis", "selection v1")
        _record(tmp_path)
        _write_stage(tmp_path, "analysis", "selection v2 — NEW picks")
        assert _dispatched(tmp_path) is False

    def test_hash_stable_across_identical_rewrites(self, tmp_path):
        _write_stage(tmp_path, "issues", "issue list")
        h1 = hydra._stage_content_hash(tmp_path, "issues")
        _write_stage(tmp_path, "issues", "issue list")
        assert hydra._stage_content_hash(tmp_path, "issues") == h1

    def test_hash_changes_on_content_change(self, tmp_path):
        _write_stage(tmp_path, "issues", "issue list A")
        h1 = hydra._stage_content_hash(tmp_path, "issues")
        _write_stage(tmp_path, "issues", "issue list B")
        assert hydra._stage_content_hash(tmp_path, "issues") != h1

    def test_missing_stage_hash_is_defined(self, tmp_path):
        # No output at all — hash over "<missing>" markers, never an exception.
        assert isinstance(hydra._stage_content_hash(tmp_path, "research"), str)

    def test_legacy_mtime_entry_still_suppresses_without_hash(self, tmp_path):
        # Pre-#2425 ledger entry (no upstream_hash) → legacy mtime comparison.
        ledger = hydra._read_ledger(tmp_path)
        ledger[EDGE] = {"date": hydra._today(), "upstream_mtime": 100.0}
        hydra._write_ledger(tmp_path, ledger)
        assert hydra._already_dispatched_today(tmp_path, EDGE, 90.0, None) is True
        assert hydra._already_dispatched_today(tmp_path, EDGE, 200.0, None) is False
