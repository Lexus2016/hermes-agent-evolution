"""Tests for scripts/evolution_ci_diagnosis.py — issue #577 rework.

Focus: the script must detect a failed GitHub check run via the supported REST
API, fetch its annotations, classify the failure, and create a child issue
only when not in dry-run mode.
"""

from __future__ import annotations

import json
import os
import urllib.parse
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
        recorded["Lexus2016/hermes-agent-evolution#42:key-error"]
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
    # Abort only when NO token is resolvable: env unset AND gh CLI unavailable.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(diag, "_resolve_github_token", lambda: "")
    client = FakeClient([])
    with pytest.raises(SystemExit) as exc_info:
        diag.diagnose_prs(client=client)
    assert exc_info.value.code == 1


def test_gh_cli_token_fallback_when_env_stripped(hermes_home, monkeypatch):
    # The no_agent cron env sanitizer strips GITHUB_TOKEN from the subprocess
    # environment; the gh CLI credential (`gh auth token`) must keep the job
    # authenticated rather than aborting.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        diag.subprocess,
        "run",
        lambda *a, **k: diag.subprocess.CompletedProcess(
            a[0] if a else ["gh"], 0, "ghp_fallback\n", ""
        ),
    )
    assert diag._resolve_github_token() == "ghp_fallback"
    # With a gh-provided token, diagnose_prs must run instead of exiting.
    diag.diagnose_prs(client=FakeClient([]))


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


# --- #2467: root-cause extraction + raw-excerpt fallback ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("E   AssertionError: assert 1 == 2", "assertion-error"),
        ("Process completed with exit code 1.", "exit-code"),
        ("runner exited with exit code 2", "exit-code"),
        ("Operation timed out after 120s", "timeout"),
        ("Permission denied: /nix/store", "permission-error"),
        ("No module named 'hermes_foo'", "module-not-found"),
        ("scripts/x.py:42:5: E501 line too long", "lint"),
    ],
)
def test_extract_from_text_new_patterns(text, expected):
    error_class, _ = diag._extract_from_text(text)
    assert error_class == expected


def test_raw_tail_returns_last_lines():
    text = "\n".join(f"line {i}" for i in range(50))
    assert diag._raw_tail(text, max_lines=5).splitlines() == [
        "line 45",
        "line 46",
        "line 47",
        "line 48",
        "line 49",
    ]


def test_extract_failure_unknown_surfaces_raw_tail():
    check = diag.FailedCheck(
        check_run_id=1,
        name="tests (slice 3)",
        conclusion="failure",
        details_url="https://github.com/runs/1",
        head_sha="sha1",
        annotations=[
            {
                "path": "tests/x.py",
                "start_line": 1,
                "annotation_level": "failure",
                "message": "some opaque native error without a known shape",
                "title": "test failed",
            }
        ],
    )
    failure = diag.extract_failure(check)
    assert failure.error_class == "unknown"
    assert failure.classification == "complex"
    assert "opaque native error" in failure.snippet


def test_create_child_issue_surfaces_unclassified_raw_excerpt(hermes_home):
    pr = diag.PRInfo(
        number=60,
        title="feat: opaque failure",
        head_sha="sha60",
        head_branch="evolution/x",
        html_url="https://github.com/Lexus2016/hermes-agent-evolution/pull/60",
    )
    check = diag.FailedCheck(
        check_run_id=7,
        name="tests (slice 4)",
        conclusion="failure",
        details_url="https://github.com/runs/7",
        head_sha="sha60",
        annotations=[],
    )
    failure = diag.FailureDetails(
        error_class="unknown",
        classification="complex",
        message="Unrecognized failure pattern",
        snippet="line A\nline B\nopaque detail",
        source="gh-run-log",
    )
    client = FakeClient([
        (200, {"total_count": 0, "items": []}),
        (
            201,
            {
                "html_url": "https://github.com/Lexus2016/hermes-agent-evolution/issues/999"
            },
        ),
    ])
    url = diag.create_child_issue(
        client, pr, [(check, failure)], "unknown", dry_run=False
    )
    assert url == "https://github.com/Lexus2016/hermes-agent-evolution/issues/999"
    post_body = json.loads(client.calls[-1][2] or "{}")
    assert "opaque detail" in post_body["body"]
    assert "Raw log/annotation excerpt" in post_body["body"]


# --- #2523: MAST failure-mode tagging ---


def test_mast_failure_modes_complete():
    assert len(diag.MAST_FAILURE_MODES) == 14
    assert set(diag.MAST_CATEGORIES) == {"1", "2", "3"}


def test_classify_mast_mode_by_error_class():
    assert diag.classify_mast_mode("test-failure") == "3.3"
    assert diag.classify_mast_mode("assertion-error") == "3.3"
    assert diag.classify_mast_mode("timeout") == "3.1"
    assert diag.classify_mast_mode("module-not-found") == "1.1"
    assert diag.classify_mast_mode("syntax-error") == "2.6"


def test_classify_mast_mode_message_hint_overrides():
    # a "timeout" keyword in the message wins over a non-timeout error class
    assert (
        diag.classify_mast_mode("exit-code", "Operation timed out after 120s") == "3.1"
    )
    assert diag.classify_mast_mode("key-error", "assert x == y failed") == "3.3"


def test_classify_mast_mode_unknown_defaults():
    assert diag.classify_mast_mode("unknown", "") == "2.6"
    assert diag.classify_mast_mode("some-future-class") == "2.6"


def test_mast_mode_label():
    assert diag.mast_mode_label("3.3") == "3.3 Incorrect Verification"
    assert diag.mast_mode_label("") == ""


def test_extract_failure_tags_mast_mode():
    check = diag.FailedCheck(
        check_run_id=1,
        name="tests",
        conclusion="failure",
        details_url="https://github.com/runs/1",
        head_sha="sha1",
        annotations=[
            {
                "path": "tests/x.py",
                "start_line": 1,
                "annotation_level": "failure",
                "message": "KeyError: 'missing'",
                "title": "test failed",
            }
        ],
    )
    failure = diag.extract_failure(check)
    assert failure.mast_mode == "2.6"  # key-error -> reasoning-action mismatch


# --- #2952: dedup by (PR, root-cause signature) ---


def test_issue_key_includes_error_class():
    assert diag._issue_key(42, "key-error") == (
        "Lexus2016/hermes-agent-evolution#42:key-error"
    )
    assert diag._issue_key(42, "test-failure") != diag._issue_key(42, "key-error")


def test_find_existing_issue_searches_error_class_in_query():
    item = {
        "html_url": "https://github.com/Lexus2016/hermes-agent-evolution/issues/777"
    }
    client = FakeClient([(200, {"total_count": 1, "items": [item]})])
    url = diag._find_existing_issue(client, 42, "key-error")
    assert url == item["html_url"]
    query_url = client.calls[-1][1]
    assert urllib.parse.quote('"CI failure on PR #42"') in query_url
    assert urllib.parse.quote('"key-error"') in query_url


def test_create_child_issue_separate_issues_per_error_class(hermes_home, monkeypatch):
    """Distinct root causes on the same PR must create distinct issues."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    report_dir = hermes_home / "reports"

    pr = _pr_payload(61, "feat: multi failure", "sha61")
    # fmt: off
    checks = _check_runs_payload([
        {"id": 1, "name": "tests", "conclusion": "failure", "details_url": "https://d/1"},
        {"id": 2, "name": "build", "conclusion": "failure", "details_url": "https://d/2"},
    ])
    ann_key = [{"path": "tests/x.py", "start_line": 1, "annotation_level": "failure",
                "message": "KeyError: 'missing'", "title": "t"}]
    ann_timeout = [{"path": "tests/y.py", "start_line": 1, "annotation_level": "failure",
                    "message": "Operation timed out after 120s", "title": "t"}]
    issue1 = {"html_url": "https://github.com/Lexus2016/hermes-agent-evolution/issues/500"}
    issue2 = {"html_url": "https://github.com/Lexus2016/hermes-agent-evolution/issues/501"}
    # fmt: on

    client = FakeClient([
        (200, [pr]),
        (200, checks),
        (200, ann_key),  # check 1 -> key-error
        (200, ann_timeout),  # check 2 -> timeout
        (200, {"total_count": 0, "items": []}),  # search key-error
        (201, issue1),
        (200, {"total_count": 0, "items": []}),  # search timeout
        (201, issue2),
    ])

    results = diag.diagnose_prs(dry_run=False, client=client, report_dir=report_dir)
    assert len(results) == 2
    urls = {r["error_class"]: r["child_issue_url"] for r in results}
    assert urls["key-error"] == issue1["html_url"]
    assert urls["timeout"] == issue2["html_url"]
    assert urls["key-error"] != urls["timeout"]
    posts = [(m, u) for m, u, _ in client.calls if m == "POST" and "/issues" in u]
    assert len(posts) == 2


def test_dry_run_two_failures_shared_error_creates_one_issue(hermes_home, monkeypatch):
    """Two failures sharing a root cause group into ONE issue, not two."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    report_dir = hermes_home / "reports"

    pr = _pr_payload(62, "pr: dup root cause", "sha62")
    # fmt: off
    checks = _check_runs_payload([
        {"id": 1, "name": "tests (slice 1)", "conclusion": "failure", "details_url": "https://d/1"},
        {"id": 2, "name": "tests (slice 2)", "conclusion": "failure", "details_url": "https://d/2"},
    ])
    ann = [{"path": "tests/x.py", "start_line": 1, "annotation_level": "failure",
            "message": "KeyError: 'missing'", "title": "t"}]
    # fmt: on
    client = FakeClient([
        (200, [pr]),
        (200, checks),
        (200, ann),  # check 1 -> key-error
        (200, ann),  # check 2 -> key-error
        (200, {"total_count": 0, "items": []}),  # single dedup search
    ])
    diag.diagnose_prs(dry_run=True, client=client, report_dir=report_dir)
    searches = [u for m, u, _ in client.calls if m == "GET" and "/search/issues" in u]
    assert len(searches) == 1  # one group -> one dedup search -> one issue
