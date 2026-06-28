"""Tests for scripts/evolution_ci_diagnosis.py — issue #577 rework.

Focus: the script must detect a failed GitHub check run via the supported REST
API, fetch its annotations, classify the failure, and create a child issue
only when not in dry-run mode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import scripts.evolution_ci_diagnosis as diag


class FakeClient:
    """Injectable HTTP client that replays responses and records requests."""

    def __init__(self, responses: List[Tuple[int, Any]]):
        self.responses = list(responses)
        self.calls: List[Tuple[str, str, Optional[str]]] = []

    def __call__(
        self, method: str, url: str, body: Optional[str] = None
    ) -> Tuple[int, str]:
        self.calls.append((method, url, body))
        if not self.responses:
            return 500, ""
        status, payload = self.responses.pop(0)
        return status, json.dumps(payload) if not isinstance(payload, str) else payload


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    # hermes_constants caches the override context var on import, but get_hermes_home
    # reads os.environ when no override token is set, so env is sufficient for tests.
    return home


def _pr_payload(
    number: int, title: str, head_sha: str, head_branch: str = "feature"
) -> Dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/Lexus2016/hermes-agent-evolution/pull/{number}",
        "head": {"sha": head_sha, "ref": head_branch},
    }


def _check_runs_payload(checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"check_runs": checks}


def test_extract_snippet_bounds():
    lines = [f"line {i}" for i in range(50)]
    text = "\n".join(lines)
    offset = text.index("line 25")
    snippet = diag._extract_snippet(text, offset, context_lines=5)
    assert "line 20" in snippet
    assert "line 30" in snippet
    assert "line 19" not in snippet
    assert "line 31" not in snippet


def test_classify_failure_trivial():
    assert diag.classify_failure("lint") == "trivial"
    assert diag.classify_failure("unused-import") == "trivial"
    assert diag.classify_failure("type-error") == "complex"


def test_extract_from_text_detects_key_error():
    text = "FAILED tests/unit/test_x.py::test_y - KeyError: 'missing'"
    error_class, message = diag._extract_from_text(text)
    assert error_class == "pytest-error"
    assert "KeyError" in message


def test_extract_failure_prefers_annotations():
    check = diag.FailedCheck(
        check_run_id=123,
        name="tests",
        conclusion="failure",
        details_url="https://github.com/check/123",
        head_sha="sha1",
        annotations=[
            {
                "path": "tests/unit/test_x.py",
                "start_line": 42,
                "annotation_level": "failure",
                "message": "KeyError: 'missing'",
                "title": "test_y failed",
            }
        ],
    )
    failure = diag.extract_failure(check)
    assert failure.source == "annotations"
    assert failure.error_class == "key-error"
    assert failure.classification == "complex"
    assert "KeyError" in failure.message


def test_fetch_open_prs_parses_head_sha_and_branch():
    client = FakeClient([(200, [_pr_payload(7, "feat: x", "abc123", "evolution/x")])])
    prs = diag.fetch_open_prs(client)
    assert len(prs) == 1
    assert prs[0].number == 7
    assert prs[0].head_sha == "abc123"
    assert prs[0].head_branch == "evolution/x"


def test_fetch_failed_check_runs_returns_only_failures():
    client = FakeClient([
        (
            200,
            _check_runs_payload([
                {
                    "id": 1,
                    "name": "lint",
                    "conclusion": "success",
                    "details_url": "https://d/1",
                },
                {
                    "id": 2,
                    "name": "tests",
                    "conclusion": "failure",
                    "details_url": "https://d/2",
                },
            ]),
        ),
        (200, []),
    ])
    failed = diag.fetch_failed_check_runs(client, "sha1")
    assert len(failed) == 1
    assert failed[0].name == "tests"
    assert failed[0].check_run_id == 2


def test_extract_run_id_from_details_url():
    assert (
        diag.extract_run_id_from_details_url(
            "https://github.com/owner/repo/runs/12345/job/678"
        )
        == 12345
    )
    assert (
        diag.extract_run_id_from_details_url("https://github.com/owner/repo/runs/12345")
        == 12345
    )
    assert diag.extract_run_id_from_details_url("https://example.com") is None


def test_diagnose_prs_detects_failure_and_creates_child_issue(hermes_home, monkeypatch):
    """End-to-end with injected client: failure is detected and a child issue is created."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    state_path = hermes_home / "evolution" / "ci_diagnosis" / "recorded_issues.json"
    report_dir = hermes_home / "reports"

    pr = _pr_payload(42, "feat: broken", "sha42")
    checks = _check_runs_payload([
        {
            "id": 99,
            "name": "tests",
            "conclusion": "failure",
            "details_url": "https://github.com/runs/99",
        }
    ])
    annotations = [
        {
            "path": "tests/unit/test_y.py",
            "start_line": 10,
            "annotation_level": "failure",
            "message": "KeyError: 'missing'",
            "title": "test_y failed",
        }
    ]
    issue_response = {
        "number": 777,
        "html_url": "https://github.com/Lexus2016/hermes-agent-evolution/issues/777",
    }

    client = FakeClient([
        (200, [pr]),  # open PRs
        (200, checks),  # check runs for sha42
        (200, annotations),  # annotations for check 99
        (200, {"total_count": 0, "items": []}),  # existing issue search
        (201, issue_response),  # create child issue
    ])

    results = diag.diagnose_prs(
        dry_run=False,
        client=client,
        recorded_state_path=state_path,
        report_dir=report_dir,
    )

    assert len(results) == 1
    result = results[0]
    assert result["pr_number"] == 42
    assert result["conclusion"] == "failure"
    assert result["classification"] == "complex"
    assert result["error_class"] == "key-error"
    assert result["child_issue_url"] == issue_response["html_url"]

    # Verify state was recorded.
    assert state_path.is_file()
    recorded = json.loads(state_path.read_text(encoding="utf-8"))
    assert (
        recorded["Lexus2016/hermes-agent-evolution#42/tests"]
        == issue_response["html_url"]
    )


def test_diagnose_prs_dry_run_does_not_create_issue(hermes_home, monkeypatch):
    """Dry-run must detect the failure but must not call the POST issue endpoint."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    report_dir = hermes_home / "reports"

    pr = _pr_payload(43, "feat: dry", "sha43")
    checks = _check_runs_payload([
        {
            "id": 100,
            "name": "lint",
            "conclusion": "failure",
            "details_url": "https://github.com/runs/100",
        }
    ])
    annotations = [
        {
            "path": "scripts/evolution_x.py",
            "start_line": 5,
            "annotation_level": "failure",
            "message": "module os imported but unused (F401)",
            "title": "unused import",
        }
    ]

    client = FakeClient([
        (200, [pr]),
        (200, checks),
        (200, annotations),
    ])

    results = diag.diagnose_prs(
        dry_run=True,
        client=client,
        report_dir=report_dir,
    )

    assert len(results) == 1
    assert results[0]["classification"] == "trivial"
    assert results[0]["error_class"] == "unused-import"
    assert results[0]["child_issue_url"] is None

    # No POST call to issues endpoint.
    post_calls = [(m, u) for m, u, _ in client.calls if m == "POST" and "/issues" in u]
    assert post_calls == []


def test_diagnose_prs_no_failed_checks_marks_success(hermes_home, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    report_dir = hermes_home / "reports"

    pr = _pr_payload(44, "feat: green", "sha44")
    client = FakeClient([
        (200, [pr]),
        (200, _check_runs_payload([])),
    ])

    results = diag.diagnose_prs(
        dry_run=True,
        client=client,
        report_dir=report_dir,
    )

    assert len(results) == 1
    assert results[0]["conclusion"] == "success"
    assert results[0]["classification"] is None


def test_missing_github_token_exits(hermes_home, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = FakeClient([])
    with pytest.raises(SystemExit) as exc_info:
        diag.diagnose_prs(client=client)
    assert exc_info.value.code == 1


def test_main_cli_runs_with_dry_run(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    called = {"dry_run": None}

    def fake_diagnose_prs(**kw):
        called["dry_run"] = kw.get("dry_run")
        return []

    monkeypatch.setattr(diag, "diagnose_prs", fake_diagnose_prs)
    rc = diag.main(["evolution_ci_diagnosis.py", "--dry-run"])
    assert rc == 0
    assert called["dry_run"] is True
