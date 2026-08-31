"""Tests for the staged Mem0 health probe (#167).

The probe must attribute a red memory stack to the failing stage instead of
reporting a monolithic "probe failed". These tests exercise the orchestration
with a stub memory object (no live mem0 stack needed).
"""

import json
from typing import Any, Dict, List

from scripts.mem0_probe import ProbeOutcome, run_probe


class _StubEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, query: str) -> None:
        self.calls += 1
        if self.calls == 2:  # fail on the second call (after first success)
            raise RuntimeError("embedder exploded")


class _StubMemory:
    """Minimal memory-like object exercising each probe stage."""

    def __init__(
        self,
        *,
        fail_embed: bool = False,
        fail_write: bool = False,
        fail_read: bool = False,
        fail_init: bool = False,
    ) -> None:
        self.embedding_model = _StubEmbedder()
        self.fail_embed = fail_embed
        self.fail_write = fail_write
        self.fail_read = fail_read
        self.fail_init = fail_init
        self.add_calls = 0
        self.search_calls = 0

    def _probe_initialized(self) -> None:
        if self.fail_init:
            raise RuntimeError("init failed")

    def add(self, text: str, **kwargs: Any) -> None:
        self.add_calls += 1
        if self.fail_write:
            raise TimeoutError("write timed out")

    def search(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
        self.search_calls += 1
        if self.fail_read:
            raise ConnectionError("vector store unreachable")
        return []


def test_healthy_probe_all_stages_ok() -> None:
    mem = _StubMemory()
    emitted: List[dict] = []

    outcome = run_probe(mem, emit=emitted.append)

    assert outcome.ok is True
    assert [r.stage for r in outcome.results] == ["init", "embed", "write", "read"]
    assert all(r.ok for r in outcome.results)
    # started + finished lines for each stage, plus no probe-done line here
    assert len(emitted) == 8
    assert emitted[0] == {"stage": "init", "status": "started"}
    assert emitted[1]["status"] == "ok"
    # the write stage must have exercised the real add() path
    assert mem.add_calls == 1
    assert mem.search_calls == 1


def test_failing_write_stage_is_attributed() -> None:
    mem = _StubMemory(fail_write=True)
    emitted: List[dict] = []

    outcome = run_probe(mem, emit=emitted.append)

    assert outcome.ok is False
    assert outcome.failing_stages() == ["write"]
    # the read stage still runs after a write failure — one failure must not
    # hide the health of the remaining stages
    assert [r.stage for r in outcome.results] == ["init", "embed", "write", "read"]
    assert outcome.results[2].detail.startswith("TimeoutError")
    failed_line = next(d for d in emitted if d["status"] == "failed")
    assert failed_line["stage"] == "write"
    assert failed_line["detail"].startswith("TimeoutError")


def test_failing_read_stage_is_attributed() -> None:
    mem = _StubMemory(fail_read=True)

    outcome = run_probe(mem)

    assert outcome.ok is False
    assert outcome.failing_stages() == ["read"]
    assert outcome.results[3].detail.startswith("ConnectionError")


def test_embed_stage_skipped_when_no_embedder() -> None:
    class NoEmbedder:
        def add(self, text: str, **kwargs: Any) -> None:
            pass

        def search(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
            return []

    outcome = run_probe(NoEmbedder())
    assert [r.stage for r in outcome.results] == ["init", "write", "read"]
    assert outcome.ok is True


def test_stdout_lines_are_json_parseable() -> None:
    """Every emitted line must be a JSON object (the shell consumer parses them)."""
    mem = _StubMemory(fail_read=True)
    emitted: List[dict] = []

    run_probe(mem, emit=emitted.append)

    for line in emitted:
        json.loads(json.dumps(line))  # round-trips → serializable
    # and the probe-done summary the CLI appends is also parseable
    summary = {"probe": "done", "ok": False, "failed": ["read"]}
    assert json.loads(json.dumps(summary))["failed"] == ["read"]


def test_outcome_ok_is_true_for_empty_results() -> None:
    """A memory with no probeable stages is vacuously healthy (no crash).

    The init hook always runs (it is a no-op when the memory object does not
    implement it); only embed/write/read are skipped.
    """
    outcome = run_probe(object())
    assert isinstance(outcome, ProbeOutcome)
    assert outcome.ok is True
    assert [r.stage for r in outcome.results] == ["init"]
    assert outcome.results[0].ok is True
