# -*- coding: utf-8 -*-
"""SkillProx Slice 3 — retroactive proximal shrinkage tests (#2779)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.lib.skill_prox import record_verdict  # noqa: E402
from evolution.lib.skill_shrink import shrink_negative_sections  # noqa: E402

_BODY = """# My Skill

Intro paragraph.

## Good Section

Kept content.

## Bad Section

This content failed every re-execution.

## Another Good

Also kept.
"""


def _reject_section(tmp_path: Path, header: str, text: str, skill: str = "my-skill"):
    """Record the section's exact text as rejected in the verdict store."""
    import hashlib

    key = hashlib.sha256(
        f"{skill}\n{header}\n{text}".encode("utf-8")
    ).hexdigest()
    store = tmp_path / "verdicts.jsonl"
    # write a raw record whose key IS the section key (verdict-store shape)
    rec = {"key": key, "skill": skill, "passed": False}
    with store.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return store


def test_negative_section_removed_others_kept(tmp_path):
    header = "## Bad Section"
    text = next(t for h, t in
                __import__("evolution.lib.skill_shrink", fromlist=["x"])
                ._iter_sections(_BODY) if h == header)
    store = _reject_section(tmp_path, header, text)
    result = shrink_negative_sections("my-skill", _BODY, store_path=store)
    assert result.shrunk is True
    assert result.removed_sections == [header]
    assert "failed every re-execution" not in result.new_body
    assert "Kept content." in result.new_body
    assert "Also kept." in result.new_body


def test_prior_body_archived_to_history(tmp_path):
    header = "## Bad Section"
    text = next(t for h, t in
                __import__("evolution.lib.skill_shrink", fromlist=["x"])
                ._iter_sections(_BODY) if h == header)
    store = _reject_section(tmp_path, header, text)
    result = shrink_negative_sections("my-skill", _BODY, store_path=store,
                                      history_dir=tmp_path / "hist")
    assert result.history_path is not None
    rec = json.loads(result.history_path.read_text().splitlines()[0])
    assert rec["removed_sections"] == [header]
    assert rec["prior_body"] == _BODY  # auditable + reversible


def test_clean_body_is_untouched(tmp_path):
    store = tmp_path / "verdicts.jsonl"  # empty/absent store
    result = shrink_negative_sections("my-skill", _BODY, store_path=store)
    assert result.shrunk is False
    assert result.new_body == _BODY
    assert result.removed_sections == []


def test_accepted_section_is_not_removed(tmp_path):
    # record_verdict(passed=True) for the exact section content → verdict
    # latest=True ⇒ NOT negative utility.
    import hashlib

    header = "## Good Section"
    text = next(t for h, t in
                __import__("evolution.lib.skill_shrink", fromlist=["x"])
                ._iter_sections(_BODY) if h == header)
    key = hashlib.sha256(f"my-skill\n{header}\n{text}".encode()).hexdigest()
    store = tmp_path / "verdicts.jsonl"
    with store.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "skill": "my-skill", "passed": True}) + "\n")
    result = shrink_negative_sections("my-skill", _BODY, store_path=store)
    assert result.shrunk is False
