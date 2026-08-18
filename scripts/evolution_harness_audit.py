#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harness-integrity audit gate — the seven BenchJack exploit classes (#2808).

Berkeley RDI (arXiv 2605.12673) broke eight top agent benchmarks without
solving tasks via seven recurring structural exploits. Hermes's evolution
gates are the same kind of harness — this module runs each class as a
DETERMINISTIC CHECKLIST against the pipeline's eval code so a new harness
lands only if it passes (a regression gate on eval/harness code).

The checks are static/heuristic (grep-level, path-level) — deliberately
cheap and deterministic; they gate STRUCTURE (is the grader isolated from
the candidate? is gold data shipped in config? ...), not semantics.

Usage: python3 scripts/evolution_harness_audit.py [paths...] — exit 1 with
a BLOCKED report naming the exploit class for every finding.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

__all__ = ["AuditFinding", "HarnessAuditReport", "audit_harness_files"]

# ── The seven exploit classes (BenchJack taxonomy) ─────────────────────
E1_SHARED_CONTAINER = "E1_no_isolation"       # agent code runs where tests run
E2_GOLD_IN_CONFIG = "E2_gold_in_config"       # answers shipped with task config
E3_EVAL_UNTRUSTED = "E3_eval_of_untrusted"    # eval() on agent output
E4_JUDGE_INJECTION = "E4_judge_unsanitized"   # raw agent text into judge prompt
E5_WEAK_MATCH = "E5_weak_string_match"        # substring pass on verbose answer
E6_NO_GROUND_TRUTH = "E6_never_compares"      # verdict without comparing
E7_TRUST_UNTRUSTED_CODE = "E7_trust_output"   # executing agent-supplied verify

_E3_RE = re.compile(r"\beval\s*\(\s*[a-z_]*(?:output|result|answer|response|content|text)", re.I)
_E4_RAW_FMT = re.compile(r"(?:judge|verdict|grade|rubric)_prompt[^\n]*\{(?:answer|output|result|response)\b", re.I)
_E5_RE = re.compile(
    r"(?:expected|gold|answer)[^\n]{0,40}\bin\b[^\n]{0,40}(?:output|result|answer|response)[^\n]{0,20}\.lower\(\)"
    r"|(?:output|result|answer|response)\.lower\(\)[^\n]{0,20}\bin\b[^\n]{0,40}(?:expected|gold|answer)",
    re.I,
)
_E6_CONST_RETURN = re.compile(
    r"return\s+(?:True|1(?:\.0)?|\"pass(?:ed)?\"|'pass(?:ed)?')\s*(?:#.*)?$", re.I
)
_E6_DEF = re.compile(r"def\s+(?:validate|score|grade|verdict)[a-z_]*\s*\(", re.I)
_E7_RE = re.compile(r"(?:subprocess|os\.system|os\.popen)[^\n]*(?:run|call|check_output|Popen)?\([^\n]*(?:verify|check)_?(?:script|cmd|command|output)", re.I)
_GOLD_KEYS = ("gold", "answer", "expected_output", "expected_result", "solution")


@dataclass
class AuditFinding:
    exploit_class: str
    path: str
    line: int
    snippet: str
    hint: str


@dataclass
class HarnessAuditReport:
    findings: List[AuditFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        if self.passed:
            return "harness audit OK — no BenchJack-class findings"
        lines = [f"harness audit BLOCKED — {len(self.findings)} finding(s):"]
        for f in self.findings:
            lines.append(f"  • {f.exploit_class} {f.path}:{f.line}: {f.hint}")
        return "\n".join(lines)


def _findings_for(path: Path, text: str) -> List[AuditFinding]:
    out: List[AuditFinding] = []
    rel = str(path)

    def line_of(m: re.Match) -> int:
        return text.count("\n", 0, m.start()) + 1

    # E1: harness imports/executes the CANDIDATE's code in-process (the
    # conftest-rewrites-tests shape): applying a patch/file write inside the
    # same process that then scores it.
    if re.search(r"(?:apply_patch|write_text|shutil\.copy)[^\n]{0,80}(?:conftest|test_)", text, re.I):
        out.append(AuditFinding(E1_SHARED_CONTAINER, rel, 0, "",
            "harness writes into test/conftest files in-process — score the "
            "verified state transition in an ISOLATED process/container, never "
            "the same one that produced the patch"))
    # E2: gold answers shipped in task/config files the candidate can read.
    stem_keys = [k for k in _GOLD_KEYS if k in path.name.lower()]
    if stem_keys and path.suffix in (".json", ".yaml", ".yml", ".toml"):
        out.append(AuditFinding(E2_GOLD_IN_CONFIG, rel, 0, "",
            "task-config file name carries a gold/answer/solution marker — "
            "ground truth must live outside every path the candidate can read"))
    for m in _E3_RE.finditer(text):
        out.append(AuditFinding(E3_EVAL_UNTRUSTED, rel, line_of(m), m.group(0)[:80],
            "eval() over candidate-controlled text — never evaluate untrusted "
            "input; parse to data instead"))
    for m in _E4_RAW_FMT.finditer(text):
        out.append(AuditFinding(E4_JUDGE_INJECTION, rel, line_of(m), m.group(0)[:80],
            "candidate answer interpolated RAW into a judge prompt — "
            "sanitize/delimit untrusted text before judge composition"))
    for m in _E5_RE.finditer(text):
        out.append(AuditFinding(E5_WEAK_MATCH, rel, line_of(m), m.group(0)[:80],
            "lowercased-substring containment as a pass rule — verbose "
            "answers game it; compare structurally (parsed values/sets)"))
    matches = list(_E6_DEF.finditer(text))
    for i, m in enumerate(matches):
        # A validate/score body of ONLY constant returns (no comparison ops,
        # no ground-truth references before the return) is a dead evaluation.
        body_start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:end]
        ret = _E6_CONST_RETURN.search(body)
        if ret and not re.search(
            r"\b(?:expected|gold|ground_truth|ground|label|answer_key)\b",
            body[: ret.start()],
            re.I,
        ):
            out.append(AuditFinding(E6_NO_GROUND_TRUTH, rel, line_of(m), m.group(0)[:80],
                "a validate/score path returns a constant verdict without "
                "comparing to ground truth — dead evaluation"))
    for m in _E7_RE.finditer(text):
        out.append(AuditFinding(E7_TRUST_UNTRUSTED_CODE, rel, line_of(m), m.group(0)[:80],
            "executing a candidate-supplied verify command — the "
            "curl|sh trojan shape; verify in harness-owned code only"))
    return out


def audit_harness_files(paths: Sequence[Path]) -> HarnessAuditReport:
    """Run the seven-class checklist over eval/harness source files."""
    report = HarnessAuditReport()
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        report.findings.extend(_findings_for(Path(p), text))
    report.findings.sort(key=lambda f: (f.exploit_class, f.path, f.line))
    return report


def main(argv: List[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [
        Path("scripts/evolution_evaluator.py"),
        Path("scripts/evolution_rubric_judge.py"),
        Path("scripts/eval_runner.py"),
    ]
    files: List[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    report = audit_harness_files(files)
    print(report.summary())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
