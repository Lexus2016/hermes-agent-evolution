# -*- coding: utf-8 -*-
"""Live-promotion wiring tests for bounded attribution (#2898 rework).

PR #2903 was bounced: debias_outcome_credit() was dead code. This wires it
into the REAL promotion flow (tools.skill_provenance.record_promotion,
called from tools/skill_manager_tool.py:1882) — real functions, temp
HERMES_HOME, real records.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


class TestPromotionWiring:
    def test_promotion_records_attribution_and_bounded_credit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.skill_usage import _mutate

        _mutate("wired-skill", lambda rec: rec.update({"source_chain": [
            {"source_type": "terminal", "source_id": "cmd-1", "trusted": True},
            {"source_type": "read_file", "source_id": "file-a", "trusted": True},
            {"source_type": "web_search", "source_id": "page-x", "trusted": False},
        ]}))

        from tools.skill_provenance import record_promotion

        record_promotion("wired-skill", reason="provenance_ok: trusted sources present")

        # Version ledger: attribution = load-bearing (trusted) sources only.
        ledger = tmp_path / "evolution" / "skill_versions" / "wired-skill.jsonl"
        entry = json.loads(ledger.read_text(encoding="utf-8").strip())
        assert entry["attribution"] == ["cmd-1", "file-a"]
        assert "page-x" not in entry["attribution"]
        usage = json.loads((tmp_path / "skills" / ".usage.json").read_text(encoding="utf-8"))
        assert usage["wired-skill"]["attribution_credit"] == {"cmd-1": 0.5, "file-a": 0.5}