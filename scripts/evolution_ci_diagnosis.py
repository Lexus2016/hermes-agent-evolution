#!/usr/bin/env python3
"""CI failure auto-diagnosis for evolution PRs — issue #577 (rework).

Polls open PRs in ``Lexus2016/hermes-agent-evolution`` via the GitHub REST
API, detects failed check runs using the supported check-runs endpoint, fetches
failure messages from the annotations API (with a ``gh run view --log-failed``
fallback), classifies failures as trivial vs complex, and creates focused
child issues for complex failures.

Usage:
    python scripts/evolution_ci_diagnosis.py [--dry-run]

Exit codes:
    0 — completed (even if failures were seen; this is a monitor)
    1 — setup/config error (missing GITHUB_TOKEN, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

_REPO = "Lexus2016/hermes-agent-evolution"
_GITHUB_API = "https://api.github.com"
_DEFAULT_LIMIT = 100

_ANNOTATION_LIMIT = 100
_RUN_LOG_MAX_LINES = 200
_SNIPPET_LINES = 30

_TRIVIAL_ERROR_CLASSES = {
    "unused-import",
    "undefined-name",
    "line-too-long",
    "trailing-whitespace",
    "missing-newline",
    "extra-blank-line",
    "blank-lines",
    "format",
    "lint",
    "mypy",
    "type-mismatch",
    "missing-return",
    "incompatible-return",
    "missing-attribute",
}

# Regex -> error_class heuristic.
_TRIVIAL_PATTERNS: List[Tuple[str, str]] = [
    (r"ruff.*\b(F401|F811|F841|E501|W291|W292|E302|E303|W391)\b", "lint"),
    (r"ruff format --check", "format"),
    (r"black --check", "format"),
    (r"`([^`]+)` imported but unused", "unused-import"),
    (r"\b(\w+)\s+imported but unused", "unused-import"),
    (r"`([^`]+)` is not defined", "undefined-name"),
    (r"line too long \(\d+ > \d+\)", "line-too-long"),
    (r"trailing whitespace", "trailing-whitespace"),
    (r"no newline at end of file", "missing-newline"),
    (r"blank line at end of file", "extra-blank-line"),
    (r"expected 2 blank lines", "blank-lines"),
    (r"Argument .* to .* has incompatible type", "type-mismatch"),
    (r"Missing return statement", "missing-return"),
    (r"Incompatible return value type", "incompatible-return"),
    (r"Module .* has no attribute", "missing-attribute"),
    (r"mypy.*failed", "mypy"),
]

_COMPLEX_PATTERNS: List[Tuple[str, str]] = [
    (r"FAILED\s+(tests/\S+)::\S+\s+-\s+(\w+Error):", "pytest-error"),
    (r"FAILED\s+(tests/\S+)::\S+", "test-failure"),
    (r"ERROR\s+(tests/\S+)::\S+", "test-error"),
    (r"SyntaxError:", "syntax-error"),
    (r"IndentationError:", "indentation-error"),
    (r"TypeError:", "type-error"),
    (r"ValueError:", "value-error"),
    (r"KeyError:\s*['\"]?([^'\"\n]+)", "key-error"),
    (r"IndexError:", "index-error"),
    (r"AttributeError:\s*(.*)", "attribute-error"),
    (r"ImportError:", "import-error"),
    (r"ModuleNotFoundError:\s*([^\n]+)", "module-not-found"),
    (r"RecursionError:", "recursion-error"),
    (r"TimeoutError:", "timeout"),
    (r"ConnectionError:", "connection-error"),
    (r"PermissionError:", "permission-error"),
    (r"subprocess\.CalledProcessError", "subprocess-error"),
]

# -----------------------------------------------------------------------------
# Types
# -----------------------------------------------------------------------------


@dataclass
class PRInfo:
    number: int
    title: str
    head_sha: str
    head_branch: str
    html_url: str


@dataclass
class FailedCheck:
    check_run_id: int
    name: str
    conclusion: str
    details_url: str
    head_sha: str
    annotations: List[Dict[str, Any]]


@dataclass
class FailureDetails:
    error_class: str
    classification: str  # "trivial" or "complex"
    message: str
    snippet: str
    source: str  # "annotations" or "gh-run-log"


Client = Callable[
    [str, str, Optional[str]], Tuple[int, str]
]  # method, url, body -> (status, body)


# -----------------------------------------------------------------------------
# API client
# -----------------------------------------------------------------------------


def _make_request(
    url: str, token: str, method: str = "GET", body: Optional[str] = None
) -> Tuple[int, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hermes-evolution-ci-diagnosis",
    }
    data = body.encode("utf-8") if body else None
    if method != "GET":
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="ignore") if exc else ""
        return exc.code, body_text
    except urllib.error.URLError as exc:
        print(f"[ci-diagnosis] network error: {exc.reason}", file=sys.stderr)
        return 0, ""


def _resolve_github_token() -> str:
    """Resolve a GitHub token for the raw REST calls below.

    Prefers the ``GITHUB_TOKEN`` env var, then falls back to the gh CLI's
    file-based credential (``gh auth token``). The cron scheduler's
    anti-exfiltration env sanitizer (``_ALWAYS_STRIP_KEYS`` in
    ``tools/environments/local.py``) strips ``GITHUB_TOKEN``/``GH_TOKEN`` from
    every no_agent subprocess environment, so this no_agent cron never receives
    the env var even when it is set in ``~/.hermes/.env``. The gh CLI reads
    ``~/.config/gh`` (not the env), so it survives the sanitizer and is the
    evolution pipeline's documented primary auth mechanism.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def default_client(
    method: str, url: str, body: Optional[str] = None
) -> Tuple[int, str]:
    token = _resolve_github_token()
    if not token:
        print(
            "[ci-diagnosis] no GitHub token (set GITHUB_TOKEN or run `gh auth login`)",
            file=sys.stderr,
        )
        return 1, ""
    return _make_request(url, token, method, body)


def _get(client: Client, url: str) -> Optional[Any]:
    status, body = client("GET", url)
    if status != 200 or not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# -----------------------------------------------------------------------------
# State / deduplication
# -----------------------------------------------------------------------------


def _state_dir() -> Path:
    return get_hermes_home() / "evolution" / "ci_diagnosis"


def _recorded_issues_path() -> Path:
    path = _state_dir() / "recorded_issues.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_recorded() -> Dict[str, str]:
    path = _recorded_issues_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _record_issue(key: str, issue_url: str, state_path: Optional[Path] = None) -> None:
    path = state_path or _recorded_issues_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    recorded = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                recorded = {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError):
            pass
    recorded[key] = issue_url
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(recorded, fh, indent=2, sort_keys=True)
    tmp.replace(path)


# -----------------------------------------------------------------------------
# PR / check-run fetching
# -----------------------------------------------------------------------------


def fetch_open_prs(
    client: Client,
    repo: str = _REPO,
    limit: int = _DEFAULT_LIMIT,
) -> List[PRInfo]:
    url = f"{_GITHUB_API}/repos/{repo}/pulls?state=open&per_page={limit}"
    data = _get(client, url)
    if not isinstance(data, list):
        return []
    prs: List[PRInfo] = []
    for item in data:
        head = item.get("head") or {}
        sha = (head.get("sha") or "").strip()
        branch = (head.get("ref") or "").strip()
        if not sha:
            continue
        prs.append(
            PRInfo(
                number=int(item.get("number", 0)),
                title=(item.get("title") or "").strip(),
                head_sha=sha,
                head_branch=branch,
                html_url=(item.get("html_url") or "").strip(),
            )
        )
    return prs


def fetch_failed_check_runs(
    client: Client,
    head_sha: str,
    repo: str = _REPO,
) -> List[FailedCheck]:
    url = f"{_GITHUB_API}/repos/{repo}/commits/{head_sha}/check-runs?per_page={_ANNOTATION_LIMIT}"
    data = _get(client, url)
    if not isinstance(data, dict):
        return []
    checks = data.get("check_runs", [])
    failed: List[FailedCheck] = []
    for cr in checks:
        conclusion = cr.get("conclusion")
        if conclusion != "failure":
            continue
        check_run_id = cr.get("id", 0)
        annotations = fetch_check_run_annotations(client, check_run_id, repo)
        failed.append(
            FailedCheck(
                check_run_id=check_run_id,
                name=cr.get("name", "unknown"),
                conclusion=conclusion,
                details_url=cr.get("details_url", ""),
                head_sha=head_sha,
                annotations=annotations,
            )
        )
    return failed


def fetch_check_run_annotations(
    client: Client,
    check_run_id: int,
    repo: str = _REPO,
) -> List[Dict[str, Any]]:
    url = f"{_GITHUB_API}/repos/{repo}/check-runs/{check_run_id}/annotations?per_page={_ANNOTATION_LIMIT}"
    data = _get(client, url)
    if not isinstance(data, list):
        return []
    return data


def extract_run_id_from_details_url(details_url: str) -> Optional[int]:
    match = re.search(r"/runs/(\d+)/", details_url)
    if match:
        return int(match.group(1))
    match = re.search(r"/runs/(\d+)$", details_url)
    if match:
        return int(match.group(1))
    return None


def fetch_actions_run_log(
    run_id: int,
    repo: str = _REPO,
) -> Optional[str]:
    if not shutil.which("gh"):
        return None
    try:
        proc = subprocess.run(
            ["gh", "run", "view", str(run_id), "--repo", repo, "--log-failed"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        proc = subprocess.run(
            ["gh", "run", "view", str(run_id), "--repo", repo, "--log"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except subprocess.TimeoutExpired:
        print("[ci-diagnosis] gh run view timed out", file=sys.stderr)
    return None


# -----------------------------------------------------------------------------
# Failure extraction and classification
# -----------------------------------------------------------------------------


def _extract_snippet(
    text: str, offset: int, context_lines: int = _SNIPPET_LINES
) -> str:
    lines = text.splitlines()
    line_index = max(0, text[:offset].count("\n"))
    start = max(0, line_index - context_lines)
    end = min(len(lines), line_index + context_lines + 1)
    return "\n".join(lines[start:end])


def _format_annotations(annotations: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for ann in annotations:
        path = ann.get("path", "")
        line = ann.get("start_line", ann.get("line"))
        message = ann.get("message", "").strip()
        title = ann.get("title", "").strip()
        level = ann.get("annotation_level", "")
        if not message and not title:
            continue
        loc = f"{path}:{line}" if path and line else path or ""
        header = f"{level} at {loc}".strip()
        if title and title != message:
            parts.append(f"{header}\n{title}: {message}")
        else:
            parts.append(f"{header}\n{message}")
    return "\n\n".join(parts)


def classify_failure(error_class: str) -> str:
    if error_class in _TRIVIAL_ERROR_CLASSES:
        return "trivial"
    return "complex"


def extract_failure(
    check: FailedCheck,
    runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
) -> FailureDetails:
    # 1. Try annotations first (no zip, no gh CLI dependency).
    if check.annotations:
        annotations_text = _format_annotations(check.annotations)
        error_class, message = _extract_from_text(annotations_text)
        return FailureDetails(
            error_class=error_class,
            classification=classify_failure(error_class),
            message=message,
            snippet=_extract_snippet(
                annotations_text, annotations_text.find(message) if message else 0
            ),
            source="annotations",
        )

    # 2. Fallback to gh run view --log-failed.
    run_id = extract_run_id_from_details_url(check.details_url)
    log_text: Optional[str] = None
    if run_id is not None:
        log_text = fetch_actions_run_log(run_id)
    if not log_text:
        return FailureDetails(
            error_class="unknown",
            classification="complex",
            message="Could not retrieve failure details (no annotations and no gh log).",
            snippet="",
            source="none",
        )

    error_class, message = _extract_from_text(log_text)
    offset = log_text.find(message) if message else 0
    snippet = (
        "\n".join(log_text.splitlines()[:_RUN_LOG_MAX_LINES])
        if offset == 0
        else _extract_snippet(log_text, offset)
    )
    return FailureDetails(
        error_class=error_class,
        classification=classify_failure(error_class),
        message=message,
        snippet=snippet,
        source="gh-run-log",
    )


def _extract_from_text(text: str) -> Tuple[str, str]:
    for pattern, error_class in _TRIVIAL_PATTERNS:
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        if match:
            return error_class, match.group(0).strip()
    for pattern, error_class in _COMPLEX_PATTERNS:
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        if match:
            return error_class, match.group(0).strip()
    return "unknown", "Unrecognized failure pattern"


# -----------------------------------------------------------------------------
# Child issue creation
# -----------------------------------------------------------------------------


def _issue_key(pr_number: int, check_name: str) -> str:
    return f"{_REPO}#{pr_number}/{check_name}"


def _find_existing_issue(
    client: Client,
    pr_number: int,
    check_name: str,
    repo: str = _REPO,
) -> Optional[str]:
    recorded = _load_recorded()
    key = _issue_key(pr_number, check_name)
    if key in recorded:
        return recorded[key]
    # Search open issues referencing the PR number in the title/body.
    query = f'is:issue is:open repo:{repo} "CI failure on PR #{pr_number}"'
    encoded = urllib.parse.quote(query)
    url = f"{_GITHUB_API}/search/issues?q={encoded}"
    data = _get(client, url)
    if isinstance(data, dict) and data.get("total_count", 0) > 0:
        items = data.get("items", [])
        if items:
            url = str(items[0].get("html_url", ""))
            if url:
                _record_issue(key, url)
                return url
    return None


def create_child_issue(
    client: Client,
    pr: PRInfo,
    check: FailedCheck,
    failure: FailureDetails,
    dry_run: bool = False,
    recorded_state_path: Optional[Path] = None,
) -> Optional[str]:
    key = _issue_key(pr.number, check.name)
    existing = _find_existing_issue(client, pr.number, check.name)
    if existing:
        print(f"[ci-diagnosis] issue already exists for PR #{pr.number}: {existing}")
        return existing

    title = f"CI failure on PR #{pr.number}: {failure.error_class}"
    body_lines = [
        f"## Auto-detected CI failure",
        "",
        f"**PR**: #{pr.number} — {pr.title}",
        f"**PR URL**: {pr.html_url}",
        f"**Failing check**: `{check.name}`",
        f"**Check run URL**: {check.details_url}",
        f"**Head SHA**: `{pr.head_sha}`",
        f"**Classification**: `{failure.classification}`",
        f"**Source**: `{failure.source}`",
        f"**Detected error class**: `{failure.error_class}`",
        "",
        f"### Error message",
        "",
        f"```\n{failure.message}\n```",
        "",
        f"### Log/annotation excerpt",
        "",
        f"```\n{failure.snippet[:2000]}\n```",
        "",
        "---",
        "_Generated by evolution-ci-diagnosis (issue #577)_",
    ]
    body = "\n".join(body_lines)

    if dry_run:
        print(f"[ci-diagnosis] dry-run: would create issue '{title}'")
        return "dry-run-issue-url"

    url = f"{_GITHUB_API}/repos/{_REPO}/issues"
    payload = json.dumps({"title": title, "body": body, "labels": ["fix"]})
    status, response = client("POST", url, payload)
    if status not in {201, 200}:
        print(
            f"[ci-diagnosis] failed to create issue: {status} {response}",
            file=sys.stderr,
        )
        return None
    try:
        issue_url = json.loads(response).get("html_url", "")
    except json.JSONDecodeError:
        issue_url = ""
    if issue_url:
        _record_issue(key, issue_url, recorded_state_path)
    return issue_url


# -----------------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------------


def _report_dir() -> Path:
    return get_hermes_home() / "evolution" / "ci-diagnosis"


def save_diagnosis_report(
    results: List[Dict[str, Any]],
    report_dir: Optional[Path] = None,
) -> Path:
    target = report_dir or _report_dir()
    target.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    report_path = target / f"diagnosis-{date_str}.json"
    summary = {
        "total_prs": len(results),
        "success": sum(1 for r in results if r.get("conclusion") == "success"),
        "failed": sum(1 for r in results if r.get("conclusion") == "failure"),
        "in_progress": sum(
            1 for r in results if r.get("conclusion") in {None, "in_progress"}
        ),
        "trivial": sum(1 for r in results if r.get("classification") == "trivial"),
        "complex": sum(1 for r in results if r.get("classification") == "complex"),
        "child_issues_created": sum(1 for r in results if r.get("child_issue_url")),
    }
    data = {
        "run_time": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "results": results,
    }
    report_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[ci-diagnosis] report saved: {report_path}")
    return report_path


# -----------------------------------------------------------------------------
# Main diagnose loop
# -----------------------------------------------------------------------------


def diagnose_prs(
    dry_run: bool = False,
    client: Optional[Client] = None,
    recorded_state_path: Optional[Path] = None,
    report_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    if _resolve_github_token() == "":
        print(
            "[ci-diagnosis] no GitHub token available "
            "(set GITHUB_TOKEN or authenticate the gh CLI with `gh auth login`)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    api = client or default_client

    prs = fetch_open_prs(api)
    print(f"[ci-diagnosis] found {len(prs)} open PR(s)")

    results: List[Dict[str, Any]] = []
    for pr in prs:
        failed_checks = fetch_failed_check_runs(api, pr.head_sha)
        if not failed_checks:
            # We still report the PR as healthy/failure-unknown.
            # For simplicity, mark success when no failed check-runs exist.
            results.append({
                "pr_number": pr.number,
                "pr_title": pr.title,
                "pr_url": pr.html_url,
                "head_sha": pr.head_sha,
                "conclusion": "success",
                "classification": None,
                "error_class": None,
                "message": None,
                "child_issue_url": None,
            })
            print(f"  PR #{pr.number}: no failed checks")
            continue

        for check in failed_checks:
            failure = extract_failure(check)
            child_issue_url: Optional[str] = None
            if failure.classification == "complex":
                child_issue_url = create_child_issue(
                    api,
                    pr,
                    check,
                    failure,
                    dry_run=dry_run,
                    recorded_state_path=recorded_state_path,
                )
            results.append({
                "pr_number": pr.number,
                "pr_title": pr.title,
                "pr_url": pr.html_url,
                "head_sha": pr.head_sha,
                "check_name": check.name,
                "check_run_id": check.check_run_id,
                "conclusion": "failure",
                "classification": failure.classification,
                "error_class": failure.error_class,
                "message": failure.message,
                "child_issue_url": child_issue_url,
            })
            print(
                f"  PR #{pr.number} check '{check.name}': {failure.classification} ({failure.error_class})"
            )

    save_diagnosis_report(results, report_dir=report_dir)
    return results


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="CI failure auto-diagnosis for evolution PRs (#577)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report only; do not create issues"
    )
    parser.add_argument(
        "--limit", type=int, default=_DEFAULT_LIMIT, help="Maximum PRs to scan"
    )
    args = parser.parse_args(argv[1:])

    try:
        diagnose_prs(dry_run=args.dry_run)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
