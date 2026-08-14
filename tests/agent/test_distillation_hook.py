"""Tests for the contrastive distillation hook (issue #2235, Slice B).

Verifies:
- a contrasting pair (successful vs failing) yields a non-empty invariant
- the invariant is model-agnostic (tagged with model-identity families)
- storage round-trips and de-duplicates
- retrieval filters by task dimension
- malformed store lines are skipped (never raises)
"""

from agent.distillation_hook import (
    DistilledInvariant,
    contrast_trajectories,
    load_invariants,
    retrieve,
    store_invariant,
)


def _steps(role_text_pairs):
    return [{"role": r, "content": c} for r, c in role_text_pairs]


# A grounded, corrective trajectory (the "good" approach).
GOOD = _steps([
    ("user", "Fix the flaky CI retry in the upload job."),
    ("assistant", "Let me first check the retry loop and reproduce the failure."),
    ("tool", "read_file(upload.py)"),
    ("assistant", "I will verify the timeout config and trace the error path."),
    ("tool", "terminal(pytest upload -x)"),
    ("assistant", "Confirmed: the retry is on a 429 with no backoff. Patch it."),
])

# A stall-heavy trajectory (the "bad" approach that loops on the same error).
BAD = _steps([
    ("user", "Fix the flaky CI retry in the upload job."),
    ("assistant", "This failed again. Let me retry the same upload."),
    ("tool", "terminal(upload)"),
    ("assistant", "Same error again, retrying once more."),
    ("tool", "terminal(upload)"),
    ("assistant", "Still failing with the same timeout."),
])


def test_contrast_produces_nonempty_invariant():
    task = {"type": "coding", "dimension": "coding"}
    inv = contrast_trajectories(
        task, GOOD, "anthropic/claude-opus-4-8", BAD, "openai/gpt-5"
    )
    assert isinstance(inv, DistilledInvariant)
    assert inv.text
    assert "coding" in inv.text
    assert inv.confidence > 0.5  # retained approach is more grounded


def test_invariant_tags_source_families():
    task = {"type": "coding"}
    inv = contrast_trajectories(task, GOOD, "claude-opus-4-8", BAD, "gpt-5")
    assert "anthropic" in inv.source_families
    assert "openai" in inv.source_families
    assert inv.source_models == ["claude-opus-4-8", "gpt-5"]


def test_contrast_swaps_when_b_is_grounded():
    task = {"type": "coding"}
    inv = contrast_trajectories(task, BAD, "openai/gpt-5", GOOD, "claude-opus-4-8")
    # The grounded trajectory is now the second one; the invariant must retain it.
    assert inv.approach == "b"


def test_store_roundtrip_and_dedupe(tmp_path):
    task = {"type": "coding"}
    inv = contrast_trajectories(
        task, GOOD, "claude-opus-4-8", BAD, "gpt-5", _now="2026-08-14T00:00:00Z"
    )
    assert store_invariant(inv, store_dir=tmp_path)
    assert store_invariant(inv, store_dir=tmp_path)  # dedupe
    loaded = load_invariants(store_dir=tmp_path)
    assert len(loaded) == 1
    assert loaded[0].text == inv.text
    assert loaded[0].confidence == inv.confidence


def test_store_skips_malformed_lines(tmp_path):
    (tmp_path / "distilled-invariants.jsonl").write_text("not json\n", encoding="utf-8")
    assert load_invariants(store_dir=tmp_path) == []


def test_retrieve_filters_by_dimension(tmp_path):
    coding = contrast_trajectories(
        {"type": "coding"},
        GOOD,
        "claude-opus-4-8",
        BAD,
        "gpt-5",
        _now="2026-08-14T00:00:00Z",
    )
    creative = contrast_trajectories(
        {"type": "creative"},
        GOOD,
        "claude-opus-4-8",
        BAD,
        "gpt-5",
        _now="2026-08-14T00:00:00Z",
    )
    store_invariant(coding, store_dir=tmp_path)
    store_invariant(creative, store_dir=tmp_path)
    hits = retrieve({"type": "coding"}, store_dir=tmp_path)
    assert len(hits) == 1
    assert hits[0].dimension == "coding"


def test_empty_trajectory_does_not_raise():
    inv = contrast_trajectories({"type": "general"}, [], "claude-opus-4-8", [], "gpt-5")
    assert inv.text
    assert inv.confidence >= 0.1
