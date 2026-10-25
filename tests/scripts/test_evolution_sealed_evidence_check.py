"""Tests for the sealed-evidence CI check (issue #3516)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from evolution_sealed_evidence_check import (
    check_ci_run,
    check_file_hash,
    check_github_api,
    main,
)


def test_ci_run_matches_record_conclusion():
    assert check_ci_run("success", {"conclusion": "success"})["match"] is True
    assert check_ci_run("SUCCESS", {"conclusion": "success"})["match"] is True


def test_ci_run_mismatch_is_a_hard_failure():
    result = check_ci_run("success", {"conclusion": "failure"})
    assert result["match"] is False
    assert result["actual"] == "failure"


def test_ci_run_record_without_conclusion():
    result = check_ci_run("success", {"status": "completed"})
    assert result["match"] is False
    assert result["error"] == "record_missing_conclusion"


def test_file_hash_matches_bytes_on_disk(tmp_path):
    artifact = tmp_path / "report.md"
    artifact.write_text("sealed record\n", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(b"sealed record\n").hexdigest()
    assert check_file_hash(digest, artifact)["match"] is True
    assert check_file_hash("0" * 64, artifact)["match"] is False


def test_github_api_diffs_response_field():
    record = {"state": "open", "number": 3516}
    assert check_github_api("open", record, "state")["match"] is True
    mismatch = check_github_api("closed", record, "state")
    assert mismatch["match"] is False
    assert mismatch["actual"] == "open"


def test_github_api_missing_field():
    result = check_github_api("open", {"number": 1}, "state")
    assert result["match"] is False
    assert result["error"] == "record_missing_field"


def test_cli_exit_codes(tmp_path, capsys):
    record = tmp_path / "run.json"
    record.write_text(json.dumps({"conclusion": "success"}), encoding="utf-8")

    assert (
        main(["--kind", "ci_run", "--reported", "success", "--record", str(record)])
        == 0
    )
    assert (
        main(["--kind", "ci_run", "--reported", "failure", "--record", str(record)])
        == 1
    )

    out = capsys.readouterr().out.strip().splitlines()
    assert json.loads(out[-1])["match"] is False


def test_cli_rejects_malformed_record(tmp_path):
    record = tmp_path / "bad.json"
    record.write_text("[1, 2, 3]", encoding="utf-8")
    assert (
        main(["--kind", "github_api", "--reported", "open", "--record", str(record)])
        == 1
    )
