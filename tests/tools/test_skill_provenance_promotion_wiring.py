# -*- coding: utf-8 -*-
"""Live-promotion wiring tests for bounded attribution (#2898 rework).

First attempt (PR #2903) was bounced: `debias_outcome_credit()` and
`record --attribution` were dead code. This rework wires them into the REAL
promotion flow — `tools.skill_provenance.record_promotion` (called from
`tools/skill_manager_tool.py:1882`). Tests use the real functions with a
temp HERMES_HOME: real usage record, real version ledger.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


def _seed_chain(skill: str, chain) -> None:
    from tools.skill_usage import _mutate

    _mutate(skill, lambda rec: rec.update({"source_chain": chain}))


def _ledger(tmp_path: Path, skill: str) -> Path:
    return tmp_path / "evolution" / "skill_versions" / f"{skill}.jsonl"


def _usage(tmp_path: Path):
    return json.loads((tmp_path / "skills" / ".usage.json").read_text(encoding="utf-8"))


class TestPromotionWiring:
    def test_promotion_records_attribution_and_bounded_credit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_chain("wired-skill", [
            {"source_type": "terminal", "source_id": "cmd-1", "trusted": True},
            {"source_type": "read_file", "source_id": "file-a", "trusted": True},
            {"source_type": "web_search", "source_id": "page-x", "trusted": False},
        ])

        from tools.skill_provenance import record_promotion

        record_promotion("wired-skill", reason="provenance_ok: trusted sources present")

        # Version ledger: attribution = load-bearing (trusted) sources only.
        entry = json.loads(_ledger(tmp_path, "wired-skill").read_text(encoding="utf-8").strip())
        assert entry["attribution"] == ["cmd-1", "file-a"]
        assert "page-x" not in entry["attribution"]
        # Usage record: outcome credit bounded, split across load-bearing only.
        assert _usage(tmp_path)["wired-skill"]["attribution_credit"] == {"cmd-1": 0.5, "file-a": 0.5}
