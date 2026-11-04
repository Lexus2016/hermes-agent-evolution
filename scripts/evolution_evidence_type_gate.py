#!/usr/bin/env python3
"""Evidence-typed verification gate for evolution claims (#3507).

DefuzeX AgentBehaviorBench finding: untethered claims (agent assertions of
'implemented / verified / done' backed only by self-reported text or plans)
cause false successes and dropped requirements.

This gate enforces that every claim of an implementation, bug fix, command run,
or issue closure is backed by an evidence artifact of the matching structural
type. A description is not a diff; a plan is not an output.

Evidence types:
  * diff: unified diff / patch showing concrete file modifications
  * command_output: stdout / stderr / exit code traces from real terminal execution
  * test_result: test runner output / summary proving test passage
  * file_reference: real on-disk file path and symbol reference
  * description_only: plain prose, plan, or unsupported assertion (triggers mismatch)

Usage:
  python scripts/evolution_evidence_type_gate.py --claim-type code_change --evidence-file patch.diff
  python scripts/evolution_evidence_type_gate.py --audit-paths
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from evolution_evidence_gate_rules import load_rules


def _rules() -> dict[str, Any]:
    """Pinned evidence-gate markers and claim map (#3567)."""
    return load_rules()["type_gate"]


def _markers() -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """Compile the pinned markers; none of them stay hard-coded here."""
    rules = _rules()
    return (
        re.compile(rules["diff_markers"], re.MULTILINE),
        re.compile(rules["test_result_markers"], re.MULTILINE),
        re.compile(rules["command_output_markers"], re.MULTILINE),
    )


def detect_evidence_type(evidence: str, repo_root: Optional[Path] = None) -> str:
    """Classify the structural type of an evidence string."""
    if not evidence or not evidence.strip():
        return "empty"

    text = evidence.strip()
    markers = _markers()

    # Test runner output takes precedence if present
    if markers[1].search(text):
        return "test_result"

    # Unified diff format
    if markers[0].search(text):
        return "diff"

    # Command execution output trace
    if markers[2].search(text):
        return "command_output"

    # File reference check on disk
    if repo_root and ("\n" not in text) and len(text) < 300:
        candidate_path = text.split(":")[0].strip()
        full_path = (repo_root / candidate_path).resolve()
        try:
            if full_path.exists() and full_path.is_file():
                return "file_reference"
        except (OSError, ValueError):
            pass

    return "description_only"


def _expected_map() -> dict[str, set[str]]:
    """Claim type -> acceptable evidence types, from the pinned surface."""
    return {k: set(v) for k, v in _rules()["expected_evidence"].items()}


def _closure_overrides() -> dict[str, set[str]]:
    """Closure reason -> narrowed evidence types, from the pinned surface."""
    return {k: set(v) for k, v in _rules()["closure_overrides"].items()}


def evaluate_claim(
    claim_type: str,
    evidence: str,
    statement: str = "",
    closure_reason: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Evaluate a single claim against evidence requirements.

    Returns a dict with 'valid': bool, 'error': str | None, and diagnostic fields.
    """
    claim_norm = claim_type.strip().lower()
    expected = _expected_map()
    if claim_norm not in expected:
        return {
            "valid": False,
            "error": "unknown_claim_type",
            "message": f"Unknown claim type: '{claim_type}'",
            "claim_type": claim_type,
        }

    detected_type = detect_evidence_type(evidence, repo_root=repo_root)
    allowed_types = set(expected[claim_norm])

    # Special handling for issue closures
    if claim_norm == "issue_closure" and closure_reason:
        reason_norm = closure_reason.strip().lower()
        overrides = _closure_overrides()
        if reason_norm in overrides:
            allowed_types = set(overrides[reason_norm])

    if detected_type not in allowed_types:
        return {
            "valid": False,
            "error": "evidence_type_mismatch",
            "message": (
                f"Claim of type '{claim_type}' requires evidence of type {sorted(allowed_types)}, "
                f"but received '{detected_type}'."
            ),
            "claim_type": claim_type,
            "statement": statement,
            "expected_types": sorted(allowed_types),
            "received_type": detected_type,
        }

    return {
        "valid": True,
        "error": None,
        "claim_type": claim_type,
        "statement": statement,
        "detected_type": detected_type,
    }


def audit_destructive_paths(repo_root: Path) -> Dict[str, Any]:
    """Audit safety gates for multi-path destructive file operations (#3507).

    DefuzeX documents a critical defect class where one path (e.g. terminal rm)
    is gated, but alternative paths (e.g. file-op delete or write) bypass the gate.
    This audit verifies that all file operations (delete, move, write) in tools
    use get_write_denied_error.
    """
    file_ops_path = repo_root / "tools" / "file_operations.py"
    if not file_ops_path.exists():
        return {"audited": False, "error": "tools/file_operations.py not found"}

    content = file_ops_path.read_text(encoding="utf-8-sig")

    required_checks = [
        ("delete_file", "get_write_denied_error"),
        ("move_file", "get_write_denied_error"),
    ]

    missing: List[str] = []
    for method, guard in required_checks:
        pattern = rf"def {method}\s*\([^)]*\)\s*(?:->[^:]+)?:[\s\S]*?{guard}"
        if not re.search(pattern, content):
            missing.append(f"{method} is missing {guard}")

    return {
        "audited": True,
        "multi_path_safe": len(missing) == 0,
        "missing_guards": missing,
        "methods_checked": [m for m, _ in required_checks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claim-type",
        choices=sorted(_expected_map()),
        help="Type of claim being verified",
    )
    parser.add_argument("--statement", default="", help="Claim statement text")
    parser.add_argument("--evidence", default="", help="Verbatim evidence text")
    parser.add_argument("--evidence-file", type=Path, help="Path to evidence file")
    parser.add_argument("--closure-reason", help="Issue closure reason if applicable")
    parser.add_argument(
        "--audit-paths",
        action="store_true",
        help="Run multi-path safety gate audit on file operations",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent

    if args.audit_paths:
        res = audit_destructive_paths(repo_root)
        print(json.dumps(res, indent=2))
        return 0 if res.get("multi_path_safe") else 1

    if not args.claim_type:
        parser.error("Either --claim-type or --audit-paths is required.")

    evidence_text = args.evidence
    if args.evidence_file:
        if not args.evidence_file.exists():
            print(f"Error: evidence file '{args.evidence_file}' not found.", file=sys.stderr)
            return 2
        evidence_text = args.evidence_file.read_text(encoding="utf-8-sig")

    result = evaluate_claim(
        claim_type=args.claim_type,
        evidence=evidence_text,
        statement=args.statement,
        closure_reason=args.closure_reason,
        repo_root=repo_root,
    )

    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
