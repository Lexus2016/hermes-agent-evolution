"""Tests for evolution_draft_selector.py — parallel draft mode (#798 inc 1)."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_draft_selector import DEFAULT_MAX_DRAFTERS, build_draft_tasks, select_best_draft, main  # noqa: E402  # fmt: skip


def test_build():
    tasks, dropped = build_draft_tasks("X", 3)
    assert dropped == 0 and len(tasks) == 3
    assert all(t["goal"] == "X" and t["role"] == "leaf" for t in tasks)
    assert len(build_draft_tasks("g")[0]) == DEFAULT_MAX_DRAFTERS
    assert len(build_draft_tasks("g", 0)[0]) == 1
    t2 = build_draft_tasks("g", 1, toolsets=["t"])[0]
    assert all(t["toolsets"] == ["t"] for t in t2)


def test_select():
    out = {"results": [
        {"task_index": 0, "status": "completed", "summary": "Short."},
        {"task_index": 1, "status": "completed", "summary": "## H\n\n```python\nx\n```\n\nhttps://e.com\n" * 3},
    ]}  # fmt: skip
    bi, bs, drafts = select_best_draft(out)
    assert bi == 1 and bs > 0.0 and drafts[1]["score"] > drafts[0]["score"]
    bi2, bs2, _ = select_best_draft({
        "results": [{"task_index": 0, "status": "timeout", "summary": ""}]
    })
    assert bi2 == -1 and bs2 == 0.0
    for bad in (None, 42, "x", {"no_results": True}):
        assert select_best_draft(bad)[0] == -1


def test_cli(capsys, monkeypatch):
    assert main(["x", "build", "--goal", "g", "--drafters", "2"]) == 0
    assert len(json.loads(capsys.readouterr().out)["tasks"]) == 2
    assert main(["x", "build", "--drafters", "2"]) == 2
    payload = json.dumps({"results": [
        {"task_index": 0, "status": "completed", "summary": "s"},
        {"task_index": 1, "status": "completed", "summary": "## H\nhttps://x.com\n"},
    ]})  # fmt: skip
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["x", "select"]) == 0
    assert json.loads(capsys.readouterr().out)["best_index"] == 1
    assert main(["x"]) == 2 and main(["x", "frob"]) == 2
