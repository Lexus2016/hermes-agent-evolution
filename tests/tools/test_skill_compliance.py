"""Tests for tools/skill_compliance.py (#2183). Uses mocks — no live skills."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def test_record_compliance_bumps_counts(tmp_path: Path) -> None:
    """record_compliance increments trigger/comply/violation counters."""
    import tools.skill_compliance as sc

    store: dict = {}
    with patch.object(sc, "_mutate") as mock_mutate, patch.object(
        sc, "_find_skill_dir", return_value=None
    ):
        # Capture the mutator closure and apply it to a fake record.
        sc.record_compliance("my-skill", triggered=True, complied=True, boundary_violated=True)
        assert mock_mutate.called
        skill_name, mutator = mock_mutate.call_args[0][0], mock_mutate.call_args[0][1]
        assert skill_name == "my-skill"
        rec = {"trigger_count": 0, "comply_count": 0, "boundary_violation_count": 0}
        mutator(rec)
        assert rec == {"trigger_count": 1, "comply_count": 1, "boundary_violation_count": 1}


def test_check_boundary_violations_detects_forbidden_tool(tmp_path: Path) -> None:
    """A tool call matching forbidden_tools is flagged as a violation."""
    import tools.skill_compliance as sc

    with patch.object(
        sc, "_read_forbidden_tools", return_value=["terminal", "bash"]
    ):
        assert sc.check_boundary_violations("sealed-skill", ["read_file", "terminal"]) is True
        assert sc.check_boundary_violations("sealed-skill", ["read_file", "web_search"]) is False


def test_check_boundary_violations_no_forbidden_list(tmp_path: Path) -> None:
    """No forbidden_tools declared -> never a violation."""
    import tools.skill_compliance as sc

    with patch.object(sc, "_read_forbidden_tools", return_value=[]):
        assert sc.check_boundary_violations("open-skill", ["terminal", "bash"]) is False


def test_quality_summary_aggregates_rates(tmp_path: Path) -> None:
    """quality_summary aggregates per-skill trigger/comply/violation rates."""
    import tools.skill_compliance as sc

    fake_usage = {
        "alpha": {"trigger_count": 10, "comply_count": 8, "boundary_violation_count": 1},
        "beta": {"trigger_count": 5, "comply_count": 5, "boundary_violation_count": 0},
        "gamma": {"trigger_count": 0, "comply_count": 0, "boundary_violation_count": 0},
    }
    with patch.object(sc, "load_usage", return_value=fake_usage):
        qs = sc.quality_summary()
    # gamma excluded (zero triggers); alpha + beta included with correct rates.
    assert set(qs.keys()) == {"alpha", "beta"}
    assert qs["alpha"] == {"triggers": 10, "complies": 8, "violations": 1, "comply_rate": 0.8}
    assert qs["beta"] == {"triggers": 5, "complies": 5, "violations": 0, "comply_rate": 1.0}