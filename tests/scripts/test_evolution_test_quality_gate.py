"""Tests for scripts/evolution_test_quality_gate.py — the deterministic
test-quality gates (#1209 fabricated-reproduction detection, #1210 mock-ratio
quality gate).

Pure functions only — no network, no git, no file IO.  All test data is
constructed inline so the tests are hermetic and fast.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_test_quality_gate import (  # noqa: E402
    DEFAULT_MOCK_RATIO_THRESHOLD,
    detect_fabricated_reproduction,
    compute_mock_ratio,
    compute_mock_ratio_from_diff,
    check_test_quality,
    FabricationReport,
    MockRatioResult,
    _is_test_file,
    _diff_adds_mocks,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _f(path, additions=1, deletions=0, patch=None):
    f = {"path": path, "additions": additions, "deletions": deletions}
    if patch is not None:
        f["patch"] = patch
    return f


# ── #1209: Fabricated-reproduction detection ──────────────────────────────


class TestIsTestFile:
    def test_standard_test_file(self):
        assert _is_test_file("tests/scripts/test_evolution_merge_gate.py")
        assert _is_test_file("tests/agent/test_foo.py")

    def test_root_test_file(self):
        assert _is_test_file("test_evolution_test_quality_gate.py")

    def test_non_test_file(self):
        assert not _is_test_file("scripts/evolution_merge_gate.py")
        assert not _is_test_file("agent/memory.py")

    def test_non_python(self):
        assert not _is_test_file("tests/test_foo.md")

    def test_underscore_test_suffix(self):
        assert _is_test_file("tests/utils/my_helper_test.py")


class TestFabricatedReproduction:
    def test_clean_test_no_findings(self):
        """A real integration test with assertions on real output — no flags."""
        content = (
            "def test_real_feature(tmp_path):\n"
            "    result = compute_something()\n"
            "    assert result == 42\n"
        )
        report = detect_fabricated_reproduction({"tests/test_real.py": content})
        assert not report.is_flagged
        assert report.files_scanned == 1
        assert report.files_flagged == 0

    def test_mock_only_test_flagged(self):
        """A test whose body is all mock setup with assertions on return_value."""
        content = (
            "def test_mocked_feature(mock_obj):\n"
            "    mock = MagicMock()\n"
            "    mock.return_value = 42\n"
            "    assert mock.return_value\n"
        )
        report = detect_fabricated_reproduction({"tests/test_mock.py": content})
        assert report.is_flagged
        assert report.files_flagged >= 1

    def test_integration_test_with_some_mocks_not_flagged(self):
        """A test that uses mocks for an external API but exercises real code."""
        content = (
            "def test_with_mocked_api(tmp_path):\n"
            "    mock = MagicMock()\n"
            "    mock.return_value = {'status': 'ok'}\n"
            "    patch('requests.get', return_value=mock.return_value)\n"
            "    result = process_data(tmp_path / 'input.txt')\n"
            "    assert result is not None\n"
        )
        report = detect_fabricated_reproduction({"tests/test_integration.py": content})
        # Has real-behavior counter-signals (tmp_path, real function call)
        assert not report.is_flagged

    def test_asserts_return_value_flagged(self):
        """Assertions on mock.return_value indicate fabricated verification."""
        content = (
            "def test_fabricated():\n"
            "    mock = MagicMock()\n"
            "    mock.return_value = True\n"
            "    assert mock.return_value\n"
        )
        report = detect_fabricated_reproduction({"tests/test_fab.py": content})
        assert report.is_flagged

    def test_non_test_files_skipped(self):
        """Only test files are scanned."""
        content = "mock = MagicMock()\nassert mock.return_value\n"
        report = detect_fabricated_reproduction({
            "scripts/evolution_merge_gate.py": content
        })
        assert report.files_scanned == 0
        assert not report.is_flagged

    def test_empty_input_clean(self):
        report = detect_fabricated_reproduction({})
        assert not report.is_flagged
        assert report.files_scanned == 0

    def test_multiple_files_mixed(self):
        clean = "def test_real(tmp_path):\n    assert open(tmp_path).read() == ''\n"
        fabricated = (
            "def test_fab():\n"
            "    mock = MagicMock()\n"
            "    mock.return_value = 42\n"
            "    assert mock.return_value\n"
        )
        report = detect_fabricated_reproduction({
            "tests/test_clean.py": clean,
            "tests/test_fab.py": fabricated,
        })
        assert report.files_scanned == 2
        assert report.files_flagged == 1
        assert any(f.file_path == "tests/test_fab.py" for f in report.findings)

    def test_summary_string(self):
        report = FabricationReport(files_scanned=5, files_flagged=0)
        assert "CLEAN" in report.summary()
        report = FabricationReport(
            files_scanned=5,
            files_flagged=2,
            findings=[type("F", (), {"pattern_name": "ASSERTS_RETURN_VALUE"})()],
        )
        assert "FLAGGED" in report.summary()


# ── #1210: Mock-ratio quality gate ──────────────────────────────────────────


class TestDiffAddsMocks:
    def test_mock_import_added(self):
        diff = "+from unittest.mock import MagicMock\n"
        assert _diff_adds_mocks(diff)

    def test_patch_decorator_added(self):
        diff = "+@patch('module.func')\n"
        assert _diff_adds_mocks(diff)

    def test_mock_object_added(self):
        diff = "+    mock = MagicMock()\n"
        assert _diff_adds_mocks(diff)

    def test_monkeypatch_added(self):
        diff = "+    monkeypatch.setattr('os.path.exists', True)\n"
        assert _diff_adds_mocks(diff)

    def test_no_mock_lines(self):
        diff = "+import json\n+from pathlib import Path\n+def test_foo():\n+    pass\n"
        assert not _diff_adds_mocks(diff)

    def test_unchanged_lines_not_counted(self):
        diff = " from unittest.mock import MagicMock\n"  # context line, not added
        assert not _diff_adds_mocks(diff)


class TestComputeMockRatio:
    def test_no_test_files(self):
        files = [_f("scripts/foo.py", 10), _f("agent/bar.py", 20)]
        result = compute_mock_ratio(files)
        assert result.total_test_files == 0
        assert result.mock_ratio == 0.0
        assert not result.flagged

    def test_all_clean_test_files(self):
        files = [
            _f("tests/test_a.py", 30),
            _f("tests/test_b.py", 20),
            _f("scripts/foo.py", 10),
        ]
        result = compute_mock_ratio(files)
        assert result.total_test_files == 2
        assert result.mock_adding_test_files == 0
        assert result.mock_ratio == 0.0
        assert not result.flagged

    def test_all_mock_files_flagged(self):
        # With patch content showing mock additions
        mock_patch = "+from unittest.mock import MagicMock\n+mock = MagicMock()\n"
        files = [
            _f("tests/test_a.py", 10, patch=mock_patch),
            _f("tests/test_b.py", 10, patch=mock_patch),
        ]
        result = compute_mock_ratio(files, threshold=0.30)
        assert result.total_test_files == 2
        assert result.mock_adding_test_files == 2
        assert result.mock_ratio == 1.0
        assert result.flagged

    def test_half_mock_files_not_flagged_at_30pct(self):
        """50% mock ratio IS flagged at 30% threshold."""
        mock_patch = "+from unittest.mock import MagicMock\n"
        clean_patch = "+import json\n+def test_x():\n+    pass\n"
        files = [
            _f("tests/test_mock.py", 10, patch=mock_patch),
            _f("tests/test_clean.py", 10, patch=clean_patch),
        ]
        result = compute_mock_ratio(files, threshold=0.30)
        assert result.total_test_files == 2
        assert result.mock_adding_test_files == 1
        assert result.mock_ratio == 0.5
        assert result.flagged  # 50% > 30%

    def test_custom_threshold(self):
        mock_patch = "+from unittest.mock import MagicMock\n"
        files = [_f("tests/test_a.py", 10, patch=mock_patch)]
        # 100% mock ratio — flagged at any threshold < 100%
        result = compute_mock_ratio(files, threshold=0.50)
        assert result.flagged
        # Not flagged at 100% threshold
        result = compute_mock_ratio(files, threshold=1.0)
        assert not result.flagged  # 100% is not > 100%

    def test_summary_string(self):
        result = MockRatioResult(total_test_files=0)
        assert "not applicable" in result.summary()

        result = MockRatioResult(
            mock_adding_test_files=2,
            total_test_files=3,
            mock_ratio=0.667,
            threshold=0.30,
            flagged=True,
        )
        assert "FLAGGED" in result.summary()
        assert "67%" in result.summary()


class TestComputeMockRatioFromDiff:
    def test_precise_from_diff(self):
        diff = (
            "diff --git a/tests/test_a.py b/tests/test_a.py\n"
            "--- a/tests/test_a.py\n"
            "+++ b/tests/test_a.py\n"
            "+from unittest.mock import MagicMock\n"
            "+mock = MagicMock()\n"
            "diff --git a/tests/test_b.py b/tests/test_b.py\n"
            "--- b/tests/test_b.py\n"
            "+++ b/tests/test_b.py\n"
            "+import json\n"
            "+def test_b():\n"
            "+    pass\n"
            "diff --git a/scripts/foo.py b/scripts/foo.py\n"
            "+x = 1\n"
        )
        result = compute_mock_ratio_from_diff(diff, threshold=0.30)
        assert result.total_test_files == 2
        assert result.mock_adding_test_files == 1
        assert result.mock_ratio == 0.5
        assert result.flagged

    def test_no_test_files_in_diff(self):
        diff = "diff --git a/scripts/foo.py b/scripts/foo.py\n+x = 1\n"
        result = compute_mock_ratio_from_diff(diff)
        assert result.total_test_files == 0
        assert not result.flagged

    def test_empty_diff(self):
        result = compute_mock_ratio_from_diff("")
        assert result.total_test_files == 0
        assert not result.flagged

    def test_none_diff(self):
        result = compute_mock_ratio_from_diff(None)  # type: ignore[arg-type]
        assert result.total_test_files == 0


# ── Integration: check_test_quality ──────────────────────────────────────────


class TestCheckTestQuality:
    def test_clean_pr_no_violations(self):
        files = [
            _f("tests/test_clean.py", 20),
            _f("scripts/foo.py", 30),
        ]
        violations = check_test_quality(files)
        assert violations == []

    def test_high_mock_ratio_flagged(self):
        mock_patch = "+from unittest.mock import MagicMock\n+mock = MagicMock()\n"
        files = [
            _f("tests/test_a.py", 10, patch=mock_patch),
            _f("tests/test_b.py", 10, patch=mock_patch),
        ]
        violations = check_test_quality(files)
        assert any("HIGH_MOCK_RATIO" in v for v in violations)

    def test_fabricated_reproduction_flagged(self):
        content = (
            "def test_fab():\n"
            "    mock = MagicMock()\n"
            "    mock.return_value = 42\n"
            "    assert mock.return_value\n"
        )
        files = [_f("tests/test_fab.py", 10)]
        violations = check_test_quality(
            files, test_contents={"tests/test_fab.py": content}
        )
        assert any("FABRICATED_REPRODUCTION" in v for v in violations)

    def test_both_gats_can_fire(self):
        mock_patch = "+from unittest.mock import MagicMock\n+mock = MagicMock()\n"
        content = (
            "def test_fab():\n"
            "    mock = MagicMock()\n"
            "    mock.return_value = 42\n"
            "    assert mock.return_value\n"
        )
        files = [_f("tests/test_fab.py", 10, patch=mock_patch)]
        violations = check_test_quality(
            files, test_contents={"tests/test_fab.py": content}
        )
        assert any("HIGH_MOCK_RATIO" in v for v in violations)
        assert any("FABRICATED_REPRODUCTION" in v for v in violations)

    def test_no_test_contents_skips_fabrication_gate(self):
        """When test_contents is None, only mock-ratio runs (fail-open)."""
        files = [_f("tests/test_fab.py", 10)]
        violations = check_test_quality(files, test_contents=None)
        # No mock_ratio violations (no patch content), no fabrication violations
        assert violations == []

    def test_empty_files_clean(self):
        violations = check_test_quality([])
        assert violations == []
