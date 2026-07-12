"""Tests for scripts/evolution_meta_editor.py (#906 — AEvo meta-editing slice).

Covers the deterministic, bounded procedure-adjustment primitive: process-level
state aggregation, reading current YAML values, proposing bounded edits, and
the independent validation gate. No LLM, no network — every function here is
pure or takes explicit paths, matching the rest of the evolution_* family.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_meta_editor import (  # noqa: E402
    TUNABLE_PARAMS,
    TunableParam,
    aggregate_state,
    format_proposals,
    main,
    propose_edits,
    read_current_value,
    validate_proposal,
    validate_proposal_against_registry,
    write_meta_record,
)


def _write_funnel_records(metrics_file: Path, records):
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    metrics_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cycle(date, selected=2, rejected=1, merged=1, created=3):
    return {
        "date": date,
        "issues_created": created,
        "selected": selected,
        "rejected": rejected,
        "merged": merged,
        "skipped": 0,
    }


# ---------------------------------------------------------------------------
# aggregate_state
# ---------------------------------------------------------------------------


class TestAggregateState:
    def test_no_history_is_empty_but_safe(self, tmp_path):
        state = aggregate_state(tmp_path)
        assert state["cycles"] == 0
        assert state["reject_rate"] == 0.0
        assert state["merged_zero_streak"] == 0
        assert state["halted"] is False

    def test_high_reject_rate_reflected(self, tmp_path):
        records = [
            _cycle(f"2026-06-{10 + i:02d}", selected=1, rejected=9, merged=1)
            for i in range(6)
        ]
        _write_funnel_records(tmp_path / "metrics.jsonl", records)
        state = aggregate_state(tmp_path)
        assert state["cycles"] == 6
        assert state["reject_rate"] > 0.7

    def test_halted_flag_from_halt_file(self, tmp_path):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "halt-state.txt").write_text("# halted\n", encoding="utf-8")
        state = aggregate_state(tmp_path)
        assert state["halted"] is True

    def test_merged_zero_streak_reflected(self, tmp_path):
        records = [
            _cycle(f"2026-06-{10 + i:02d}", selected=1, rejected=0, merged=0)
            for i in range(4)
        ]
        _write_funnel_records(tmp_path / "metrics.jsonl", records)
        state = aggregate_state(tmp_path)
        assert state["merged_zero_streak"] == 4


# ---------------------------------------------------------------------------
# read_current_value
# ---------------------------------------------------------------------------


class TestReadCurrentValue:
    PARAM = TUNABLE_PARAMS["research.min_priority_score"]

    def test_reads_nested_section_key(self, tmp_path):
        cron_dir = tmp_path / "cron" / "evolution"
        cron_dir.mkdir(parents=True)
        (cron_dir / "research.yaml").write_text(
            "name: evolution-research\nlimits:\n  min_priority_score: 0.75\n",
            encoding="utf-8",
        )
        value = read_current_value(tmp_path, self.PARAM)
        assert value == 0.75

    def test_missing_repo_root_is_none(self):
        assert read_current_value(None, self.PARAM) is None

    def test_missing_file_is_none(self, tmp_path):
        assert read_current_value(tmp_path, self.PARAM) is None

    def test_malformed_yaml_is_none(self, tmp_path):
        cron_dir = tmp_path / "cron" / "evolution"
        cron_dir.mkdir(parents=True)
        (cron_dir / "research.yaml").write_text(
            "not: [valid: yaml: at all\n", encoding="utf-8"
        )
        assert read_current_value(tmp_path, self.PARAM) is None

    def test_missing_key_is_none(self, tmp_path):
        cron_dir = tmp_path / "cron" / "evolution"
        cron_dir.mkdir(parents=True)
        (cron_dir / "research.yaml").write_text(
            "name: evolution-research\nlimits:\n  max_proposals_per_day: 20\n",
            encoding="utf-8",
        )
        assert read_current_value(tmp_path, self.PARAM) is None

    def test_non_numeric_value_is_none(self, tmp_path):
        cron_dir = tmp_path / "cron" / "evolution"
        cron_dir.mkdir(parents=True)
        (cron_dir / "research.yaml").write_text(
            "name: evolution-research\nlimits:\n  min_priority_score: true\n",
            encoding="utf-8",
        )
        assert read_current_value(tmp_path, self.PARAM) is None


# ---------------------------------------------------------------------------
# propose_edits
# ---------------------------------------------------------------------------


class TestProposeEdits:
    def _state(self, **overrides):
        base = {
            "cycles": 6,
            "reject_rate": 0.5,
            "merged_zero_streak": 0,
            "flags": [],
            "halted": False,
        }
        base.update(overrides)
        return base

    def test_not_enough_cycles_no_proposal(self):
        state = self._state(cycles=2, reject_rate=0.9)
        proposals = propose_edits(state, {})
        assert proposals == []

    def test_halted_no_proposal(self):
        state = self._state(reject_rate=0.9, halted=True)
        proposals = propose_edits(state, {})
        assert proposals == []

    def test_high_reject_rate_proposes_increase(self):
        state = self._state(reject_rate=0.85)
        current = {name: 0.7 for name in TUNABLE_PARAMS}
        proposals = propose_edits(state, current)
        assert len(proposals) == len(TUNABLE_PARAMS)
        by_name = {p["name"]: p for p in proposals}
        for name, param in TUNABLE_PARAMS.items():
            p = by_name[name]
            assert p["current"] == 0.7
            assert p["proposed"] == pytest.approx(0.75)
            assert p["delta"] == pytest.approx(0.05)
            assert "raise the quality bar" in p["reason"]

    def test_low_reject_rate_and_healthy_merges_proposes_decrease(self):
        state = self._state(reject_rate=0.1, merged_zero_streak=0)
        current = {name: 0.7 for name in TUNABLE_PARAMS}
        proposals = propose_edits(state, current)
        assert len(proposals) == len(TUNABLE_PARAMS)
        for p in proposals:
            assert p["proposed"] == pytest.approx(0.65)
            assert p["delta"] == pytest.approx(-0.05)
            assert "ease it" in p["reason"]

    def test_low_reject_rate_but_merges_stuck_no_proposal(self):
        """Don't loosen the quality bar while integration itself looks broken."""
        state = self._state(reject_rate=0.05, merged_zero_streak=3)
        current = {name: 0.7 for name in TUNABLE_PARAMS}
        proposals = propose_edits(state, current)
        assert proposals == []

    def test_moderate_reject_rate_no_proposal(self):
        state = self._state(reject_rate=0.5)
        current = {name: 0.7 for name in TUNABLE_PARAMS}
        proposals = propose_edits(state, current)
        assert proposals == []

    def test_clamped_at_max_bound_produces_no_op(self):
        state = self._state(reject_rate=0.85)
        current = {name: 0.9 for name in TUNABLE_PARAMS}  # already at max
        proposals = propose_edits(state, current)
        assert proposals == []

    def test_clamped_at_min_bound_produces_no_op(self):
        state = self._state(reject_rate=0.1, merged_zero_streak=0)
        current = {name: 0.5 for name in TUNABLE_PARAMS}  # already at min
        proposals = propose_edits(state, current)
        assert proposals == []

    def test_missing_current_value_falls_back_to_default(self):
        state = self._state(reject_rate=0.85)
        proposals = propose_edits(state, {name: None for name in TUNABLE_PARAMS})
        assert len(proposals) == len(TUNABLE_PARAMS)
        for p in proposals:
            assert p["current"] == 0.7  # the registry default

    def test_every_proposal_passes_the_registry_validation_gate(self):
        state = self._state(reject_rate=0.85)
        current = {name: 0.7 for name in TUNABLE_PARAMS}
        proposals = propose_edits(state, current)
        for p in proposals:
            ok, err = validate_proposal_against_registry(p)
            assert ok, err


# ---------------------------------------------------------------------------
# validate_proposal / validate_proposal_against_registry
# ---------------------------------------------------------------------------


class TestValidateProposal:
    PARAM = TUNABLE_PARAMS["research.min_priority_score"]

    def _valid(self, **overrides):
        base = {
            "name": "research.min_priority_score",
            "stage": "research",
            "yaml_file": "research.yaml",
            "section": "limits",
            "key": "min_priority_score",
            "current": 0.7,
            "proposed": 0.75,
        }
        base.update(overrides)
        return base

    def test_valid_proposal_passes(self):
        ok, err = validate_proposal(self._valid(), self.PARAM)
        assert ok is True
        assert err is None

    def test_exceeds_step_rejected(self):
        proposal = self._valid(proposed=0.9)  # step is 0.05, current 0.7
        ok, err = validate_proposal(proposal, self.PARAM)
        assert ok is False
        assert "step" in err

    def test_exceeds_max_bound_rejected(self):
        proposal = self._valid(current=0.88, proposed=0.93)
        ok, err = validate_proposal(proposal, self.PARAM)
        assert ok is False
        assert "bounds" in err

    def test_below_min_bound_rejected(self):
        proposal = self._valid(current=0.52, proposed=0.47)
        ok, err = validate_proposal(proposal, self.PARAM)
        assert ok is False
        assert "bounds" in err

    def test_no_op_rejected(self):
        proposal = self._valid(proposed=0.7)
        ok, err = validate_proposal(proposal, self.PARAM)
        assert ok is False
        assert "no-op" in err

    def test_mismatched_identity_rejected(self):
        proposal = self._valid(yaml_file="issues.yaml")
        ok, err = validate_proposal(proposal, self.PARAM)
        assert ok is False
        assert "identity" in err

    def test_non_numeric_proposed_rejected(self):
        proposal = self._valid(proposed="0.75")
        ok, err = validate_proposal(proposal, self.PARAM)
        assert ok is False
        assert "numeric" in err


class TestValidateProposalAgainstRegistry:
    def test_unregistered_name_rejected(self):
        proposal = {
            "name": "not.a.real.param",
            "stage": "research",
            "yaml_file": "research.yaml",
            "section": "limits",
            "key": "min_priority_score",
            "current": 0.7,
            "proposed": 0.75,
        }
        ok, err = validate_proposal_against_registry(proposal)
        assert ok is False
        assert "not a registered" in err

    def test_registered_name_delegates_to_validate_proposal(self):
        proposal = {
            "name": "research.min_priority_score",
            "stage": "research",
            "yaml_file": "research.yaml",
            "section": "limits",
            "key": "min_priority_score",
            "current": 0.7,
            "proposed": 0.75,
        }
        ok, err = validate_proposal_against_registry(proposal)
        assert ok is True
        assert err is None


# ---------------------------------------------------------------------------
# format_proposals / write_meta_record
# ---------------------------------------------------------------------------


class TestFormatProposals:
    def test_empty_proposals_reads_as_no_change(self):
        state = {"cycles": 6, "reject_rate": 0.5}
        line = format_proposals("2026-07-12", state, [])
        assert "no procedure changes proposed" in line

    def test_proposals_are_summarized(self):
        state = {"cycles": 6, "reject_rate": 0.85}
        proposals = [
            {"name": "research.min_priority_score", "current": 0.7, "proposed": 0.75}
        ]
        line = format_proposals("2026-07-12", state, proposals)
        assert "research.min_priority_score: 0.7->0.75" in line


class TestWriteMetaRecord:
    def test_writes_and_overwrites_idempotently(self, tmp_path):
        record1 = {"date": "2026-07-12", "state": {}, "proposals": []}
        path = write_meta_record(tmp_path, "2026-07-12", record1)
        assert path == tmp_path / "meta" / "2026-07-12.json"
        assert json.loads(path.read_text(encoding="utf-8"))["proposals"] == []

        record2 = {"date": "2026-07-12", "state": {}, "proposals": [{"name": "x"}]}
        write_meta_record(tmp_path, "2026-07-12", record2)
        assert json.loads(path.read_text(encoding="utf-8"))["proposals"] == [
            {"name": "x"}
        ]


# ---------------------------------------------------------------------------
# main() — end-to-end wiring
# ---------------------------------------------------------------------------


class TestMain:
    def _make_repo(self, tmp_path, min_priority_score=0.7):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        cron_dir = repo / "cron" / "evolution"
        cron_dir.mkdir(parents=True)
        (cron_dir / "research.yaml").write_text(
            f"name: evolution-research\nlimits:\n  min_priority_score: {min_priority_score}\n",
            encoding="utf-8",
        )
        (cron_dir / "issues.yaml").write_text(
            f"name: evolution-issues\nlimits:\n  min_priority_score: {min_priority_score}\n",
            encoding="utf-8",
        )
        (cron_dir / "introspection.yaml").write_text(
            f"name: evolution-introspection\nlimits:\n  min_priority_score: {min_priority_score}\n",
            encoding="utf-8",
        )
        (cron_dir / "analysis.yaml").write_text(
            f"name: evolution-analysis\nsafety:\n  min_priority_score: {min_priority_score}\n",
            encoding="utf-8",
        )
        return repo

    def test_end_to_end_no_history_writes_empty_proposals(self, tmp_path, monkeypatch):
        evolution_dir = tmp_path / "evolution"
        repo = self._make_repo(tmp_path)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evolution_dir))
        monkeypatch.setenv("EVOLUTION_REPO_DIR", str(repo))

        rc = main(["evolution_meta_editor.py", "2026-07-12"])
        assert rc == 0

        out = json.loads((evolution_dir / "meta" / "2026-07-12.json").read_text())
        assert out["date"] == "2026-07-12"
        assert out["proposals"] == []
        assert (evolution_dir / "meta-proposals.txt").is_file()

    def test_end_to_end_high_reject_rate_writes_proposals(self, tmp_path, monkeypatch):
        evolution_dir = tmp_path / "evolution"
        repo = self._make_repo(tmp_path, min_priority_score=0.7)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evolution_dir))
        monkeypatch.setenv("EVOLUTION_REPO_DIR", str(repo))

        records = [
            _cycle(f"2026-07-{1 + i:02d}", selected=1, rejected=9, merged=1)
            for i in range(6)
        ]
        _write_funnel_records(evolution_dir / "metrics.jsonl", records)

        rc = main(["evolution_meta_editor.py", "2026-07-12"])
        assert rc == 0

        out = json.loads((evolution_dir / "meta" / "2026-07-12.json").read_text())
        assert len(out["proposals"]) == len(TUNABLE_PARAMS)
        for p in out["proposals"]:
            assert p["current"] == 0.7
            assert p["proposed"] == pytest.approx(0.75)

        sidecar = (evolution_dir / "meta-proposals.txt").read_text()
        assert "proposing" in sidecar
