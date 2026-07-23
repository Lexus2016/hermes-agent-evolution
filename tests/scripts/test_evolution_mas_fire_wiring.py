"""Tests for MAS-FIRE wiring (#1211) — verifies evolution_mas_fire's fault suite
is invoked from evolution_funnel.main(), giving the harness a live consumer in
the evolution pipeline (not dead code).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_funnel as ef  # noqa: E402


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch):
    """Prevent network calls during tests."""
    monkeypatch.setattr(ef, "_gh_pr_list_merged", lambda *a, **k: None, raising=False)


@pytest.fixture
def funnel_env(tmp_path, monkeypatch):
    """Set up a minimal evolution dir with stage reports so main() runs cleanly."""
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    d = "2026-07-23"
    _write(
        tmp_path / "analysis" / f"{d}.json",
        {"selected_for_implementation": [{"issue_number": 1}], "rejected": []},
    )
    _write(tmp_path / "integration" / f"{d}.json", {"merged": [], "skipped": []})
    (tmp_path / "metrics.jsonl").write_text(
        json.dumps({"date": d, "selected": 1, "rejected": 0, "merged": 0}) + "\n",
        encoding="utf-8",
    )
    return tmp_path, d


class TestMasFireWiring:
    """#1211 — evolution_mas_fire fault suite is run from the funnel cycle."""

    def test_mas_fire_sidecar_written(self, funnel_env):
        tmp_path, d = funnel_env
        rc = ef.main(["evolution_funnel.py", d])
        assert rc == 0
        mas_fire_file = tmp_path / "mas-fire" / f"{d}.json"
        assert mas_fire_file.is_file()
        data = json.loads(mas_fire_file.read_text())
        assert data["total"] > 0
        assert "detected" in data
        assert "silently_used" in data

    def test_mas_fire_results_are_real(self, funnel_env):
        """The funnel must actually run the fault suite, not just write empty data."""
        tmp_path, d = funnel_env
        rc = ef.main(["evolution_funnel.py", d])
        assert rc == 0
        data = json.loads((tmp_path / "mas-fire" / f"{d}.json").read_text())
        # The MAS-FIRE suite has 4 fault cases
        assert data["total"] == 4
        assert data["detected"] + data["silently_used"] == data["total"]

    def test_funnel_survives_missing_module(self, tmp_path, monkeypatch):
        """If evolution_mas_fire can't be imported, the funnel still completes."""
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        d = "2026-07-23"
        _write(
            tmp_path / "analysis" / f"{d}.json",
            {"selected_for_implementation": [{"issue_number": 1}], "rejected": []},
        )
        _write(tmp_path / "integration" / f"{d}.json", {"merged": [], "skipped": []})
        (tmp_path / "metrics.jsonl").write_text(
            json.dumps({"date": d, "selected": 1, "rejected": 0, "merged": 0}) + "\n",
            encoding="utf-8",
        )
        # Clear cached imports and restrict path so mas_fire can't be found
        original_path = sys.path[:]
        sys.path[:] = [p for p in sys.path if "scripts" not in p]
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith("evolution_"):
                del sys.modules[mod_name]
        try:
            rc = ef.main(["evolution_funnel.py", d])
            assert rc == 0
        finally:
            sys.path[:] = original_path