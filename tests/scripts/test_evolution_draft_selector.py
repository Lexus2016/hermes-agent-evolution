"""Tests for evolution_draft_selector.py — parallel draft mode (#798) + cost routing."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_draft_selector import (  # noqa: E402  # fmt: skip
    DEFAULT_MAX_DRAFTERS,
    _CMAP,
    _TIERS,
    build_draft_tasks,
    route_cost_tier,
    select_best_draft,
    main,
)


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


# ── route_cost_tier tests (#798 inc 2) ───────────────────────────────────────


def test_route_trivial():
    r = route_cost_tier("fix typo in README")
    assert r["complexity"] == "trivial"
    assert r["tier"] == "cheap"


def test_route_simple():
    r = route_cost_tier("add a test for the function")
    assert r["complexity"] == "simple"
    assert r["tier"] == "cheap"


def test_route_moderate():
    r = route_cost_tier("refactor the module to use dataclasses")
    assert r["complexity"] == "moderate"
    assert r["tier"] == "standard"


def test_route_complex():
    r = route_cost_tier("design a multi-agent protocol for security audit")
    assert r["complexity"] == "complex"
    assert r["tier"] == "frontier"


def test_route_unknown_falls_back_to_standard():
    r = route_cost_tier("")
    assert r["complexity"] == "unknown"
    assert r["tier"] == "standard"
    assert r["model"] == "standard"


def test_route_returns_all_three_keys():
    r = route_cost_tier("anything")
    assert set(r.keys()) == {"complexity", "tier", "model"}


def test_route_case_insensitive():
    r = route_cost_tier("FIX TYPO")
    assert r["complexity"] == "trivial"


def test_route_first_match_wins():
    # "trivial" appears before "complex" in _CMAP, so a string with both
    # should resolve to trivial, not complex.
    r = route_cost_tier("trivial complex task")
    assert r["complexity"] == "trivial"


def test_tiers_and_cmap_well_formed():
    # Every _CMAP label must exist in _TIERS.
    for _kw, label in _CMAP:
        assert label in _TIERS
    # _TIERS always has an "unknown" fallback.
    assert "unknown" in _TIERS


def test_route_cli(capsys):
    assert main(["x", "route", "--complexity", "fix typo"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["complexity"] == "trivial" and out["tier"] == "cheap"


def test_route_cli_positional(capsys):
    assert main(["x", "route", "complex design task"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["complexity"] == "complex"


def test_route_cli_missing_complexity(capsys):
    assert main(["x", "route"]) == 2


# ── anti-conformity tests (#2761) ────────────────────────────────────────────


def test_converged_population_keeps_contrarian_with_flag():
    """When drafts are near-identical (converged), anti-conformity picks
    the contrarian outlier instead of the best-scoring common draft."""
    # All OK; drafts 0-1 are near-identical, draft 2 is the contrarian.
    out = {"results": [
        {"task_index": 0, "status": "completed", "summary": "## H\n\nhttps://e.com\n" * 3},
        {"task_index": 1, "status": "completed", "summary": "## H\n\nhttps://e.com\n" * 3},
        {"task_index": 2, "status": "completed", "summary": "## X\n\nhttps://y.com\n" * 3},
    ]}  # fmt: skip
    bi, bs, drafts = select_best_draft(out, anti_conformity=True)
    # Draft 0 and 1 are identical -> consensus sim = 1.0 -> converged.
    # Contrarian is the one least similar to the consensus -> draft 2.
    assert bi == 2


def test_diverse_population_ignores_flag():
    """When drafts are diverse (not converged), anti-conformity falls back
    to the normal best-scoring winner — the flag does not distort a healthy
    population."""
    out = {"results": [
        {"task_index": 0, "status": "completed", "summary": "## H\n\nhttps://a.com\n" * 3},
        {"task_index": 1, "status": "completed", "summary": "## Q\n\nhttps://b.com\n" * 3},
        {"task_index": 2, "status": "completed", "summary": "## Z\n\nhttps://c.com\n" * 3},
    ]}  # fmt: skip
    bi, bs, drafts = select_best_draft(out, anti_conformity=True)
    # All three are disjoint -> consensus sim = 0.0 -> not converged -> best score wins.
    bi_no_flag, _, _ = select_best_draft(out)
    assert bi == bi_no_flag


def test_flag_off_picks_best_score():
    """Without the flag, converged populations pick best score (status quo)."""
    out = {"results": [
        {"task_index": 0, "status": "completed", "summary": "## H\n\nhttps://e.com\n" * 3},
        {"task_index": 1, "status": "completed", "summary": "## H\n\nhttps://e.com\n" * 3},
        {"task_index": 2, "status": "completed", "summary": "## X\n\nhttps://y.com\n" * 3},
    ]}  # fmt: skip
    bi, _, _ = select_best_draft(out, anti_conformity=False)
    # Draft 0 and 1 have the same score; the first one with score > bs wins.
    assert bi == 0


def test_no_ok_drafts_returns_none_with_flag():
    out = {"results": [
        {"task_index": 0, "status": "timeout", "summary": ""},
        {"task_index": 1, "status": "timeout", "summary": ""},
    ]}  # fmt: skip
    bi, bs, _ = select_best_draft(out, anti_conformity=True)
    assert bi == -1 and bs == 0.0
