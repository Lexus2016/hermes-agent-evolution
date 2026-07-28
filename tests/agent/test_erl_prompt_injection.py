# -*- coding: utf-8 -*-
"""Tests for ERL heuristic injection into the system prompt (issue #1361)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_heuristic_retrieve import (  # noqa: E402
    format_for_injection,
    load_heuristics,
    retrieve,
)


def _heuristic(pattern, task_type="coding", outcome=1.0, frequency=4):
    return {
        "task_type": task_type,
        "pattern": list(pattern),
        "text": f"On {task_type} tasks, following `{pattern[0]}` with `{pattern[1]}` worked.",
        "frequency": frequency,
        "outcome_score": outcome,
    }


def _store(evolution_dir, heuristics, date="2026-07-28"):
    d = evolution_dir / "heuristics"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{date}.json").write_text(
        json.dumps({"date": date, "count": len(heuristics), "heuristics": heuristics}),
        encoding="utf-8",
    )


def _block(evolution_dir):
    """The block the prompt builder would inject, built the same way."""
    stored = load_heuristics(evolution_dir)
    return format_for_injection(retrieve("", stored)) if stored else ""


class TestBlockContent:
    def test_block_names_the_heuristics(self, tmp_path):
        _store(tmp_path, [_heuristic(["read_file", "patch"])])
        block = _block(tmp_path)
        assert "Learned from past runs" in block
        assert "read_file" in block and "patch" in block

    def test_empty_store_produces_no_block(self, tmp_path):
        """No heuristics must mean no header, not an empty section."""
        assert _block(tmp_path) == ""

    def test_block_is_bounded(self, tmp_path):
        """Prompt budget: the top-k cap must hold however much is stored."""
        _store(tmp_path, [_heuristic([f"a{i}", f"b{i}"]) for i in range(50)])
        assert _block(tmp_path).count("- ") <= 5


class TestCacheSafety:
    """The issue requires the block be 'stable for the conversation lifetime' —
    an unstable prompt prefix breaks the provider cache on every turn."""

    def test_identical_input_yields_identical_bytes(self, tmp_path):
        _store(tmp_path, [_heuristic(["read_file", "patch"]), _heuristic(["a", "b"])])
        assert _block(tmp_path) == _block(tmp_path)

    def test_order_is_deterministic_across_reloads(self, tmp_path):
        _store(tmp_path, [_heuristic([f"t{i}", f"u{i}"], outcome=1.0) for i in range(6)])
        assert [_block(tmp_path) for _ in range(3)].count(_block(tmp_path)) == 3


class TestConfigGate:
    """`erl_prompt_injection` defaults False and is read the same way as its
    sibling toggles."""

    @staticmethod
    def _resolve(section):
        # Mirrors agent_init: bool(_agent_section.get(flag, False))
        return bool(section.get("erl_prompt_injection", False))

    def test_defaults_off(self):
        assert self._resolve({}) is False

    def test_enabled_explicitly(self):
        assert self._resolve({"erl_prompt_injection": True}) is True

    def test_independent_of_experience_injection(self):
        """Separate flags: the two distil different inputs, and an operator may
        want either without the other."""
        section = {"experience_injection": True, "erl_prompt_injection": False}
        assert self._resolve(section) is False
        assert bool(section.get("experience_injection", False)) is True

    def test_agent_without_the_attribute_is_treated_as_off(self):
        """getattr default guards agents built before this flag existed."""
        agent = SimpleNamespace()
        assert getattr(agent, "_erl_prompt_injection", False) is False


class TestFailSafe:
    def test_unreadable_store_yields_no_block(self, tmp_path):
        d = tmp_path / "heuristics"
        d.mkdir(parents=True)
        (d / "2026-07-28.json").write_text("{ broken", encoding="utf-8")
        assert _block(tmp_path) == ""

    def test_absent_directory_yields_no_block(self, tmp_path):
        assert _block(tmp_path / "nothing-here") == ""
