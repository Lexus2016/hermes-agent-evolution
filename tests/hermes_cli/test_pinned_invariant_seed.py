"""`hermes update` seeds the pinned invariant surface itself (#3518).

A host that updates must be able to judge self-edits without anyone running the
manual ``--install-pinned`` command. The step raises to ``_best_effort``: a seed
failure is reported, never fatal to the update.
"""

from __future__ import annotations

import sys
import types

import pytest

from hermes_cli import update_cmd_maint as maint


def _stub_seed(fn, monkeypatch):
    """The step imports the filter by name at call time; hand it a stub.

    ``monkeypatch.setitem`` restores the real module after each test — a direct
    ``sys.modules[...] = stub`` would leak into unrelated test modules.
    """
    stub = types.ModuleType("evolution_invariant_filter")
    stub.install_pinned_seed = fn
    monkeypatch.setitem(sys.modules, "evolution_invariant_filter", stub)


def test_seed_pinned_invariant_surface_invokes_installer(tmp_path, monkeypatch, capsys):
    seeded = {}

    def fake_seed():
        seeded["called"] = True
        return [tmp_path / "evolution" / "pinned" / "evolution_invariant_rules.json"]

    _stub_seed(fake_seed, monkeypatch)

    maint._seed_pinned_invariant_surface()

    assert seeded.get("called") is True
    out = capsys.readouterr().out
    assert "Seeding the pinned evolution invariant surface" in out
    assert "evolution_invariant_rules.json" in out


def test_seed_failure_raises_to_caller_for_best_effort_handling(monkeypatch):
    def boom():
        raise ValueError("pinned surface differs from the in-tree seed")

    _stub_seed(boom, monkeypatch)

    with pytest.raises(ValueError, match="pinned surface differs"):
        maint._seed_pinned_invariant_surface()
