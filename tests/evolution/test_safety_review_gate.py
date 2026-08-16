# -*- coding: utf-8 -*-
"""Unit tests for the safety-review gate (#2575)."""

from evolution.lib.safety_review_gate import (
    SafetyGateVerdict,
    check_safety_gate,
    scan_code_for_safety_violations,
)


def _safe_code() -> str:
    return (
        "def helper():\n"
        "    return 42\n"
        "\n"
        "def main():\n"
        "    result = helper()\n"
        "    print(result)\n"
    )


def _dangerous_code() -> str:
    return "import os\ndef main():\n    os.system('rm -rf /')\n    eval(user_input)\n"


class TestScanCodeForSafetyViolations:
    def test_safe_code_no_violations(self):
        violations = scan_code_for_safety_violations({"mod.py": _safe_code()})
        assert violations == []

    def test_dangerous_code_flagged(self):
        violations = scan_code_for_safety_violations({"mod.py": _dangerous_code()})
        assert len(violations) >= 2
        joined = "\n".join(violations)
        assert "os.system" in joined
        assert "eval" in joined

    def test_non_python_files_skipped(self):
        violations = scan_code_for_safety_violations({
            "script.sh": "os.system('rm -rf /')"
        })
        assert violations == []

    def test_line_numbers_reported(self):
        violations = scan_code_for_safety_violations({"mod.py": _dangerous_code()})
        assert any(v.startswith("mod.py:3:") for v in violations)
        assert any(v.startswith("mod.py:4:") for v in violations)

    def test_comment_lines_skipped(self):
        code = "# os.system('rm -rf /')\nprint('ok')\n"
        violations = scan_code_for_safety_violations({"mod.py": code})
        assert violations == []


class TestCheckSafetyGate:
    def test_opt_out_when_no_contents(self):
        files = [{"path": "mod.py", "additions": 1, "deletions": 0}]
        assert check_safety_gate(files, source_contents=None) == []
        assert check_safety_gate(files, source_contents={}) == []

    def test_blocks_dangerous_changed_file(self):
        files = [{"path": "mod.py", "additions": 1, "deletions": 0}]
        contents = {"mod.py": _dangerous_code()}
        violations = check_safety_gate(files, source_contents=contents)
        assert violations
        assert all(v.startswith("SAFETY_GATE_VIOLATION:") for v in violations)

    def test_only_scans_changed_files(self):
        files = [{"path": "mod.py", "additions": 1, "deletions": 0}]
        contents = {
            "mod.py": _safe_code(),
            "other.py": _dangerous_code(),  # not in the PR's changed set
        }
        assert check_safety_gate(files, source_contents=contents) == []

    def test_verdict_serialization(self):
        v = SafetyGateVerdict(safe=False, violations=["v1"], files_scanned=2)
        d = v.to_dict()
        restored = SafetyGateVerdict.from_dict(d)
        assert restored.safe is False
        assert restored.violations == ["v1"]
        assert restored.files_scanned == 2
