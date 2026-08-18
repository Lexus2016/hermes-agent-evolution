# -*- coding: utf-8 -*-
"""Harness-integrity audit gate tests (#2808) — the seven BenchJack classes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_harness_audit import audit_harness_files  # noqa: E402


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_clean_harness_passes(tmp_path):
    p = _write(tmp_path, "grader.py", """
def validate(output, expected):
    parsed = json.loads(output)
    return parsed.get("value") == expected
""")
    assert audit_harness_files([p]).passed


def test_e1_shared_container_flagged(tmp_path):
    p = _write(tmp_path, "runner.py", """
def apply(candidate_patch):
    apply_patch(candidate_patch)  # writes conftest.py in THIS process
    run_tests()
""")
    r = audit_harness_files([p])
    assert not r.passed and r.findings[0].exploit_class == "E1_no_isolation"


def test_e2_gold_in_config_flagged(tmp_path):
    p = _write(tmp_path, "tasks-gold.json", '{"1": "answer"}')
    r = audit_harness_files([p])
    assert any(f.exploit_class == "E2_gold_in_config" for f in r.findings)


def test_e3_eval_of_untrusted_flagged(tmp_path):
    p = _write(tmp_path, "grader.py", """
def score(answer):
    return eval(answer)
""")
    r = audit_harness_files([p])
    assert any(f.exploit_class == "E3_eval_of_untrusted" for f in r.findings)


def test_e4_judge_injection_flagged(tmp_path):
    p = _write(tmp_path, "judge.py", """
def build(answer):
    judge_prompt = f"Evaluate this answer: {answer}"
    return judge_prompt
""")
    r = audit_harness_files([p])
    assert any(f.exploit_class == "E4_judge_unsanitized" for f in r.findings)


def test_e5_weak_match_flagged(tmp_path):
    p = _write(tmp_path, "grader.py", """
def validate(output, expected):
    return expected in str(output).lower()
""")
    r = audit_harness_files([p])
    assert any(f.exploit_class == "E5_weak_string_match" for f in r.findings)


def test_e6_constant_verdict_flagged_and_comparison_absolves(tmp_path):
    bad = _write(tmp_path, "bad.py", """
def validate(payload):
    if payload.get("role") == "assistant":
        return True
""")
    good = _write(tmp_path, "good.py", """
def validate(output, expected):
    if output == expected:
        return True
    return False
""")
    r_bad = audit_harness_files([bad])
    assert any(f.exploit_class == "E6_never_compares" for f in r_bad.findings)
    assert audit_harness_files([good]).passed


def test_e7_trusted_verify_execution_flagged(tmp_path):
    p = _write(tmp_path, "runner.py", """
def check(task):
    subprocess.run(task.verify_script, shell=True)
""")
    r = audit_harness_files([p])
    assert any(f.exploit_class == "E7_trust_output" for f in r.findings)


def test_real_harnesses_are_clean():
    """The shipped eval harnesses must pass their own audit gate."""
    repo = Path(__file__).resolve().parents[2]
    r = audit_harness_files([
        repo / "scripts/evolution_evaluator.py",
        repo / "scripts/evolution_rubric_judge.py",
        repo / "scripts/eval_runner.py",
    ])
    assert r.passed, r.summary()
