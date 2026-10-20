"""Tests for the evidence-typed verification gate (issue #3507)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from evolution_evidence_type_gate import (  # noqa: E402
    audit_destructive_paths,
    detect_evidence_type,
    evaluate_claim,
)

SAMPLE_DIFF = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
+new_line = True
"""

SAMPLE_TEST_RESULT = """============================= test session starts ==============================
tests/test_example.py .                                                  [100%]
============================== 1 passed in 0.05s ===============================
"""

SAMPLE_COMMAND_OUTPUT = """$ python3 --version
Python 3.11.0
Command exited with code 0.
"""


def test_detect_evidence_types(tmp_path):
    assert detect_evidence_type(SAMPLE_DIFF) == "diff"
    assert detect_evidence_type(SAMPLE_TEST_RESULT) == "test_result"
    assert detect_evidence_type(SAMPLE_COMMAND_OUTPUT) == "command_output"
    assert detect_evidence_type("I will write a patch tomorrow") == "description_only"
    assert detect_evidence_type("") == "empty"

    f = tmp_path / "foo.py"
    f.write_text("print('hello')", encoding="utf-8")
    assert detect_evidence_type("foo.py", repo_root=tmp_path) == "file_reference"


def test_code_change_requires_diff_or_file_ref(tmp_path):
    # Description alone must fail with evidence_type_mismatch
    res = evaluate_claim("code_change", "Fixed the bug by editing foo.py")
    assert not res["valid"]
    assert res["error"] == "evidence_type_mismatch"
    assert res["received_type"] == "description_only"

    # Real diff must pass
    res_diff = evaluate_claim("code_change", SAMPLE_DIFF)
    assert res_diff["valid"]
    assert res_diff["error"] is None
    assert res_diff["detected_type"] == "diff"


def test_test_pass_requires_test_result():
    # Diff alone for test_pass must fail
    res_diff = evaluate_claim("test_pass", SAMPLE_DIFF)
    assert not res_diff["valid"]
    assert res_diff["error"] == "evidence_type_mismatch"

    # Real test result passes
    res_test = evaluate_claim("test_pass", SAMPLE_TEST_RESULT)
    assert res_test["valid"]
    assert res_test["error"] is None
    assert res_test["detected_type"] == "test_result"


def test_issue_closure_already_exists(tmp_path):
    existing_file = tmp_path / "existing.py"
    existing_file.write_text("code = 1", encoding="utf-8")

    # Untethered claim fails
    res_fake = evaluate_claim(
        "issue_closure",
        "It already exists in some file",
        closure_reason="already-exists",
        repo_root=tmp_path,
    )
    assert not res_fake["valid"]
    assert res_fake["error"] == "evidence_type_mismatch"

    # Real file reference passes
    res_real = evaluate_claim(
        "issue_closure",
        "existing.py",
        closure_reason="already-exists",
        repo_root=tmp_path,
    )
    assert res_real["valid"]
    assert res_real["error"] is None


def test_audit_destructive_paths():
    repo_root = Path(__file__).resolve().parent.parent.parent
    audit = audit_destructive_paths(repo_root)
    assert audit["audited"]
    assert audit["multi_path_safe"]
    assert len(audit["missing_guards"]) == 0
