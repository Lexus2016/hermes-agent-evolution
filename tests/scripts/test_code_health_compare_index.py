"""Path indexing is an optimization only: matching and ratchet verdicts stay identical."""
from importlib import import_module
from pathlib import Path

import pytest

from scripts.code_health.measure import Measurer
from scripts.code_health.ruff_runner import resolve_ruff

comparison = import_module("scripts.code_health.compare")


def _old_scan_index(same_body):
    return {path: {qual for hpath, qual in same_body if hpath == path}
            for path, _ in same_body}


@pytest.mark.parametrize("same_body", [
    set(),
    {("a.py", "same"), ("b.py", "same"), ("a.py", "same#2"),
     ("moved.py", "Outer.inner"), ("renamed.py", "new_name")},
    {(f"pkg/{path}.py", f"f#{scope}") for path in range(30) for scope in range(10)},
])
def test_path_index_equals_previous_scan_without_mutating_identity(same_body):
    original = same_body.copy()
    indexed = comparison._unchanged_scopes_by_path(same_body)
    assert dict(indexed) == _old_scan_index(same_body)
    assert indexed.get("missing.py", set()) == set()
    assert same_body == original
    if indexed:
        next(iter(indexed.values())).add("not-a-match")
        assert same_body == original


def test_completed_matches_are_traversed_once():
    class CountedSet(set):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    matches = CountedSet((f"{p}.py", "same") for p in range(100))
    indexed = comparison._unchanged_scopes_by_path(matches)
    for path in range(100):
        assert indexed[f"{path}.py"] == {"same"}
    assert matches.iterations == 1


_BAD = "def repeated():\n    try:\n        pass\n    except Exception:\n        pass\n"
_GOOD = "def repeated():\n    return 1\n"


@pytest.mark.parametrize("head_sources", [
    {"a.py": _BAD + _GOOD},  # unchanged repeated names
    {"a.py": _GOOD, "moved.py": _BAD},  # departed repeated body moves
    {"a.py": _BAD + _GOOD, "copy.py": _BAD},  # a surviving origin cannot pay twice
    {"a.py": _BAD.replace("repeated", "renamed") + _GOOD},
    {"a.py": _BAD.replace("        pass", "        return 2", 1) + _GOOD},
])
def test_comparison_matches_old_scan_for_moves_copies_and_edits(tmp_path, monkeypatch, head_sources):
    repo = Path(__file__).resolve().parents[2]
    measurer = Measurer(repo, resolve_ruff(repo), known_env=set())

    def measure(dirname, sources):
        root = tmp_path / dirname
        root.mkdir()
        for name, text in sources.items():
            (root / name).write_text(text, encoding="utf-8")
        return measurer._measure_in(root, sources)

    base = measure("base", {"a.py": _BAD + _GOOD})
    head = measure("head", head_sources)
    optimized = comparison.compare(base, head, [])
    monkeypatch.setattr(comparison, "_unchanged_scopes_by_path", _old_scan_index)
    assert comparison.compare(base, head, []) == optimized
    if "copy.py" in head_sources:
        assert any(f.path == "copy.py" and f.rule == "BLE001" for f in optimized)
