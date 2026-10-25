#!/usr/bin/env python3
"""Sealed-evidence check: diff a stage's reported outcome against the
artifact target's OWN record (#3516, CATT evidence topology).

The evidence-typed gate (evolution_evidence_type_gate.py) only classifies
text the agent supplies. This check is the counterfactual: the record lives
outside the agent's claim, and a mismatch is a hard failure.

Three record kinds, each read from a file the caller points at (CI log,
`sha256sum` output, or a saved GitHub API response):

  * ci_run         — reported conclusion vs the run JSON's own conclusion
  * file_hash      — reported sha256 vs the hash of the bytes on disk
  * github_api     — reported outcome vs the response body's own field

Usage:
  python scripts/evolution_sealed_evidence_check.py \
      --kind ci_run --reported success --record ci_run.json
  python scripts/evolution_sealed_evidence_check.py \
      --kind file_hash --reported <sha256> --target path/to/artifact
  python scripts/evolution_sealed_evidence_check.py \
      --kind github_api --reported open --record issue.json --field state

Exit 0 on match, 1 on mismatch or malformed input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"record must be a JSON object: {path}")
    return data


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def check_ci_run(reported: str, record: dict[str, Any]) -> dict[str, Any]:
    """Compare a reported CI conclusion with the run record's own field.

    Accepts a workflow-run object (`{"conclusion": ...}`) or a check-run
    object (`{"status": ..., "conclusion": ...}`).
    """
    actual = record.get("conclusion")
    if actual is None:
        return {"match": False, "error": "record_missing_conclusion", "actual": None}
    match = _norm(reported) == _norm(actual)
    return {"match": match, "actual": actual, "reported": reported}


def check_file_hash(reported: str, target: Path) -> dict[str, Any]:
    """Compare a reported sha256 with the hash of the file's own bytes."""
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    match = _norm(reported) == digest
    return {"match": match, "actual": digest, "reported": _norm(reported)}


def check_github_api(
    reported: str, record: dict[str, Any], field: str
) -> dict[str, Any]:
    """Compare a reported outcome with one field of a saved API response."""
    if field not in record:
        return {"match": False, "error": "record_missing_field", "field": field}
    actual = record[field]
    match = _norm(reported) == _norm(actual)
    return {"match": match, "actual": actual, "reported": reported, "field": field}


def run_check(
    kind: str,
    reported: str,
    record_path: Optional[Path] = None,
    target: Optional[Path] = None,
    field: str = "state",
) -> dict[str, Any]:
    """Dispatch one sealed check. Raises ValueError on bad invocation."""
    if kind == "file_hash":
        if target is None:
            raise ValueError("file_hash requires --target")
        result = check_file_hash(reported, target)
    elif kind in ("ci_run", "github_api"):
        if record_path is None:
            raise ValueError(f"{kind} requires --record")
        record = _load_json(record_path)
        result = (
            check_ci_run(reported, record)
            if kind == "ci_run"
            else check_github_api(reported, record, field)
        )
    else:
        raise ValueError(f"unknown kind: {kind}")
    result["kind"] = kind
    return result


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Sealed-evidence outcome diff (#3516)")
    parser.add_argument(
        "--kind", required=True, choices=["ci_run", "file_hash", "github_api"]
    )
    parser.add_argument(
        "--reported", required=True, help="outcome string the stage claims"
    )
    parser.add_argument(
        "--record", type=Path, help="JSON record held by the artifact target"
    )
    parser.add_argument(
        "--target", type=Path, help="file whose bytes are the record (file_hash)"
    )
    parser.add_argument(
        "--field", default="state", help="github_api response field (default: state)"
    )
    args = parser.parse_args(argv)

    try:
        result = run_check(
            args.kind, args.reported, args.record, args.target, args.field
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"match": False, "error": str(exc)}))
        return 1

    print(json.dumps(result))
    return 0 if result["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
