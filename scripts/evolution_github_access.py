#!/usr/bin/env python3
"""Classify GitHub write access for the evolution pipeline.

Three states (council 2026-08-31):

* ``write`` — authenticated viewer has push|maintain|admin on the repo.
* ``denied`` — we completed a check and the account cannot push (or there
  are no credentials at all). Registrar pauses class-A jobs; does not delete.
* ``inconclusive`` — the check did not finish (no transport, network/5xx,
  rate-limit). Registrar must leave already-registered jobs untouched.

The shell wake-gate and the registrar share this module so they cannot
diverge. Stdlib only — cron copies this file into ``$HERMES_HOME/scripts``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

WRITE = "write"
DENIED = "denied"
INCONCLUSIVE = "inconclusive"

DEFAULT_REPO = "Lexus2016/hermes-agent-evolution"


def evolution_repo() -> str:
    return (os.environ.get("GITHUB_EVOLUTION_REPO") or DEFAULT_REPO).strip()


def _has_write_permissions(blob: str) -> bool:
    compact = blob.replace(" ", "")
    return any(
        marker in compact
        for marker in ('"push":true', '"maintain":true', '"admin":true')
    )


def classify_http(status: Optional[int], body: str) -> str:
    """Map an HTTP status + repo JSON body onto the three states."""
    if status is None:
        return INCONCLUSIVE
    if status in (408, 429) or status >= 500:
        return INCONCLUSIVE
    if status == 200:
        return WRITE if _has_write_permissions(body) else DENIED
    if status in (401, 403, 404):
        # Completed authz failure — never treat an error payload as write.
        return DENIED
    return INCONCLUSIVE


def _gh_api_repo(repo: str) -> tuple[Optional[int], str]:
    gh = shutil.which("gh")
    if not gh:
        return None, ""
    try:
        user = subprocess.run(
            [gh, "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if user.returncode != 0:
            # gh present but not logged in — fall through to token path.
            return None, ""
        proc = subprocess.run(
            [gh, "api", f"repos/{repo}", "--jq", ".permissions // {}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        body = proc.stdout or proc.stderr or ""
        if proc.returncode == 0:
            wrapped = json.dumps({"permissions": json.loads(body)}) if body.strip().startswith("{") else body
            # --jq already extracted permissions; wrap so _has_write sees push.
            perms_blob = body if '"push"' in body or '"maintain"' in body or '"admin"' in body else wrapped
            return 200, perms_blob
        # gh api uses exit 1 for 4xx; stderr often has the HTTP JSON.
        err = (proc.stderr or "") + (proc.stdout or "")
        if "HTTP 401" in err or '"401"' in err:
            return 401, err
        if "HTTP 403" in err or '"403"' in err:
            return 403, err
        if "HTTP 404" in err or '"404"' in err:
            return 404, err
        if "HTTP 429" in err or "rate limit" in err.lower():
            return 429, err
        return None, err
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None, ""


def _token_api_repo(repo: str) -> tuple[Optional[int], str]:
    token = (os.environ.get("GITHUB_PRIVATE_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        return None, ""
    url = f"https://api.github.com/repos/{repo}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "hermes-evolution-access",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            return int(getattr(resp, "status", 200) or 200), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return int(exc.code), body
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, ""


def _gh_has_stored_auth() -> bool:
    """True when gh has a stored login, without a network round-trip.

    ``gh api user`` is online — using it here would turn an offline
    ``hermes update`` into DENIED and pause class-A jobs (R1/R2).
    """
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    try:
        text = hosts.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    return "oauth_token" in text or "token:" in text


def has_any_credentials() -> bool:
    if (os.environ.get("GITHUB_PRIVATE_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip():
        return True
    if shutil.which("gh") is None:
        return False
    return _gh_has_stored_auth()


def classify(repo: Optional[str] = None) -> str:
    """Live classification against GitHub. Never raises."""
    repo = (repo or evolution_repo()).strip() or DEFAULT_REPO

    # Explicit env tokens first so an ambient `gh` login (often read-only)
    # cannot shadow GITHUB_PRIVATE_TOKEN / GITHUB_TOKEN from .env.
    status, body = _token_api_repo(repo)
    if status is not None:
        return classify_http(status, body)

    status, body = _gh_api_repo(repo)
    if status is not None:
        return classify_http(status, body)

    # No completed HTTP answer.
    if has_any_credentials():
        return INCONCLUSIVE
    # No credentials at all: this install cannot push.
    return DENIED


def main(argv: list[str]) -> int:
    repo = evolution_repo()
    for i, arg in enumerate(argv):
        if arg in ("--repo", "-r") and i + 1 < len(argv):
            repo = argv[i + 1]
    state = classify(repo)
    print(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
