"""Tests for the shared retrieval/failure memo (#65).

Verifies the three contracts of issue #65:
1. Consult-before-fetch — a worker that finds a URL in the memo skips the
   fetch and uses the recorded summary (second worker never re-fetches).
2. Failure-note dedup — a known-failing edit is not re-attempted by the
   parallel draft fan-out.
3. Persistence — the memo round-trips through its shared JSON file.

Plus the real call-site wiring in ``scripts/evolution_orchestrator.py``
(research fan-out) and ``scripts/evolution_draft_selector.py`` (draft/edit
fan-out).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_draft_selector import build_draft_tasks
from evolution_orchestrator import (
    _memo_context_block,
    _memo_hits_for,
    build_worker_tasks,
    main as orchestrator_main,
)
from evolution_shared_memo import (
    SharedMemo,
    consult_before_fetch,
    default_memo_path,
    load_memo,
    main as memo_main,
)

URL = "https://arxiv.org/abs/2608.10424"
SUMMARY = "shared memo cuts parallel-subagent redundancy from 46% to 7.8%"


@pytest.fixture
def memo_path(tmp_path: Path) -> Path:
    return tmp_path / "evolution" / "shared-memo.json"


# ── consult-before-fetch: second worker skips the duplicate fetch ────────────


def test_consult_before_fetch_skips_when_url_already_in_memo(memo_path: Path):
    memo = load_memo(memo_path)
    memo.record_summary(URL, SUMMARY)
    memo.save()

    # Worker 2: the URL is already in the shared memo — the fetch function
    # must NEVER be called (that is the whole point of the memo).
    def fetch_fn(url: str) -> str:
        raise AssertionError(f"duplicate fetch attempted for {url}")

    source, content = consult_before_fetch(memo, URL, fetch_fn)
    assert source == "memo"
    assert content == SUMMARY


def test_consult_before_fetch_records_after_real_fetch(memo_path: Path):
    memo = load_memo(memo_path)
    calls: list[str] = []

    def fetch_fn(url: str) -> str:
        calls.append(url)
        return "raw page text of " + url

    # Worker 1: memo miss -> fetch runs, result recorded + persisted.
    source, content = consult_before_fetch(memo, URL, fetch_fn)
    assert source == "fetch"
    assert calls == [URL]
    assert content == "raw page text of " + URL
    assert memo.lookup_summary(URL) is not None

    # Worker 2 (fresh process view): loads the persisted file, finds the URL,
    # and skips the fetch entirely.
    memo2 = load_memo(memo_path)
    assert memo2.lookup_summary(URL) is not None
    calls.clear()
    source2, _ = consult_before_fetch(memo2, URL, fetch_fn)
    assert source2 == "memo"
    assert calls == []


def test_consult_before_fetch_is_before_fetch_not_after(memo_path: Path):
    # Ordering contract: the memo is consulted BEFORE the fetch. A memo hit
    # must short-circuit before fetch_fn is even constructed/available.
    memo = load_memo(memo_path)
    memo.record_summary(URL, SUMMARY)
    memo.save()
    source, content = consult_before_fetch(memo, URL, lambda u: 1 / 0)
    assert source == "memo"
    assert content == SUMMARY


# ── failure-note dedup: known-failing edit not re-attempted ──────────────────


def test_known_failing_goal_skips_draft_fanout(memo_path: Path):
    memo = load_memo(memo_path)
    memo.record_failure(
        "Implement the shared retrieval memo",
        "already tried twice — held-out gate blocks adoption",
    )
    memo.save()

    tasks, dropped = build_draft_tasks(
        "Implement the shared retrieval memo",
        3,
        memo_path=str(memo_path),
    )
    assert tasks == []
    assert dropped == 3  # reported, not silently queued


def test_unknown_goal_fans_out_with_memo_instructions(memo_path: Path):
    tasks, dropped = build_draft_tasks(
        "Add a brand-new feature",
        2,
        memo_path=str(memo_path),
    )
    assert dropped == 0
    assert len(tasks) == 2
    for task in tasks:
        assert "SHARED FAILURE MEMO" in task["context"]
        assert str(memo_path) in task["context"]


def test_no_memo_path_keeps_legacy_behavior():
    tasks, dropped = build_draft_tasks("Some goal", 2)
    assert dropped == 0
    assert len(tasks) == 2
    assert "SHARED FAILURE MEMO" not in tasks[0]["context"]


# ── persistence round-trip ───────────────────────────────────────────────────


def test_memo_file_persistence_round_trip(memo_path: Path):
    memo = load_memo(memo_path)
    assert memo.record_summary(URL, SUMMARY) is True
    assert memo.record_failure("goal-key", "tried and failed") is True
    memo.save()
    assert memo_path.exists()

    on_disk = json.loads(memo_path.read_text(encoding="utf-8"))
    assert on_disk["retrievals"][URL] == SUMMARY
    assert on_disk["failures"]["goal-key"] == "tried and failed"

    fresh = load_memo(memo_path)  # a different process/worker's view
    assert fresh.lookup_summary(URL) == SUMMARY
    assert fresh.is_known_failing("goal-key") is True
    assert fresh.is_known_failing("never-tried") is False


def test_missing_or_corrupt_memo_file_is_empty_not_crash(tmp_path: Path):
    assert load_memo(tmp_path / "absent" / "shared-memo.json").retrievals == {}
    bad = tmp_path / "shared-memo.json"
    bad.write_text("{not json", encoding="utf-8")
    memo = load_memo(bad)
    assert memo.retrievals == {}
    assert memo.failures == {}


def test_record_summary_keeps_first_writer(memo_path: Path):
    memo = load_memo(memo_path)
    assert memo.record_summary(URL, SUMMARY) is True
    assert memo.record_summary(URL, "a later, different summary") is False
    assert memo.lookup_summary(URL) == SUMMARY


def test_default_memo_path_follows_evolution_dir_convention(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path / "evo"))
    assert default_memo_path() == tmp_path / "evo" / "shared-memo.json"
    monkeypatch.delenv("EVOLUTION_PROFILE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert default_memo_path() == tmp_path / "evolution" / "shared-memo.json"


# ── research fan-out wiring (evolution_orchestrator.build) ───────────────────


def test_build_worker_tasks_embeds_memo_hits_before_fanout(memo_path: Path):
    memo = load_memo(memo_path)
    memo.record_summary(URL, SUMMARY)
    memo.save()

    tasks, dropped = build_worker_tasks(
        f"Research whether {URL} supports shared memos",
        ["official docs", "failure modes"],
        memo_path=str(memo_path),
    )
    assert dropped == 0
    assert len(tasks) == 2
    for task in tasks:
        assert task["memo_hits"] == [URL]
        assert "DO NOT fetch" in task["context"]
        assert URL in task["context"]
        assert SUMMARY in task["context"]
        assert "SHARED RETRIEVAL MEMO" in task["context"]


def test_build_worker_tasks_no_memo_no_hits():
    tasks, dropped = build_worker_tasks(f"Research {URL}", ["angle"])
    assert dropped == 0
    assert "memo_hits" not in tasks[0]
    assert "DO NOT fetch" not in tasks[0]["context"]


def test_memo_hits_for_skips_unknown_urls(memo_path: Path):
    memo = load_memo(memo_path)
    memo.record_summary(URL, SUMMARY)
    memo.save()
    hits = _memo_hits_for(
        f"See {URL} and https://example.com/never-fetched",
        ["no urls here"],
        str(memo_path),
    )
    assert hits == [(URL, SUMMARY)]


def test_memo_context_block_formatting():
    block = _memo_context_block([(URL, SUMMARY)], "/tmp/shared-memo.json")
    assert block.startswith("SHARED RETRIEVAL MEMO")
    assert f"- {URL}: {SUMMARY}" in block
    assert "/tmp/shared-memo.json" in block
    assert _memo_context_block([], "/tmp/shared-memo.json") == ""


# ── CLI wiring: orchestrator `memo` subcommand + build/draft flags ───────────


def test_orchestrator_memo_cli_round_trip(memo_path: Path, capsys):
    rc = orchestrator_main([
        "evolution_orchestrator.py",
        "memo",
        "record",
        URL,
        SUMMARY,
        "--memo",
        str(memo_path),
    ])
    assert rc == 0
    assert memo_path.exists()
    capsys.readouterr()  # drain the record output before the next command

    rc = orchestrator_main([
        "evolution_orchestrator.py",
        "memo",
        "lookup",
        URL,
        "--memo",
        str(memo_path),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "memo"
    assert out["summary"] == SUMMARY

    rc = orchestrator_main([
        "evolution_orchestrator.py",
        "memo",
        "failing",
        "goal-key",
        "tried and failed",
        "--memo",
        str(memo_path),
    ])
    assert rc == 0
    capsys.readouterr()  # drain before the final status read

    rc = orchestrator_main([
        "evolution_orchestrator.py",
        "memo",
        "status",
        "--memo",
        str(memo_path),
    ])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["retrieval_entries"] == 1
    assert status["failure_entries"] == 1


def test_orchestrator_build_reports_memo_stats(memo_path: Path, capsys):
    memo = load_memo(memo_path)
    memo.record_summary(URL, SUMMARY)
    memo.save()
    rc = orchestrator_main([
        "evolution_orchestrator.py",
        "build",
        "--subtask",
        f"research {URL}",
        "--angle",
        "docs",
        "--memo",
        str(memo_path),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["memo"]["retrieval_entries"] == 1
    assert out["tasks"][0]["memo_hits"] == [URL]


def test_orchestrator_draft_skips_known_failing_goal(memo_path: Path, capsys):
    memo = load_memo(memo_path)
    memo.record_failure("Implement the shared retrieval memo", "already tried")
    memo.save()
    rc = orchestrator_main([
        "evolution_orchestrator.py",
        "draft",
        "--goal",
        "Implement the shared retrieval memo",
        "--drafters",
        "3",
        "--memo",
        str(memo_path),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tasks"] == []
    assert out["dropped"] == 3
    assert out["memo_skip"] is True


def test_memo_module_cli_status(memo_path: Path, capsys):
    assert memo_main(["x", "status", "--memo", str(memo_path)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["path"] == str(memo_path)
    assert memo_main(["x", "bogus", "--memo", str(memo_path)]) == 2
