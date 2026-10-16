"""One reviewed release transition, not a general allowlist or a trust bootstrap.

The first manifest and this code are candidate changes. Independent owner-directed
review of the exact tree/workflows establishes their initial trust; the pinned digest
only detects later drift. Every raw finding remains in the report. Unknown evidence,
new occurrences and changed reviewed files remain blocking. Ordinary future PRs use
the original ratchet once the release is in their base's ancestry.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from scripts.code_health import gitio
from scripts.code_health.inherited_evidence import assign_entries

BASE = "3cd9963bcce9c0aba451dadeaff5c701bca102e4"
UPSTREAM = "297552a9d646119871391d202509822cd86330df"
MANIFEST = "analysis/sync-health/v0221-manifest.json"
MANIFEST_SHA256 = "786204b408b51ef1e592eea1d7f88283f7b3670e7f3dfa23abf6ccd35d3ee820"


def blob(repo: Path, revision: str | None, path: str) -> tuple[str, bytes] | None:
    """Read regular repository data only; never follow a manifest/source symlink."""
    if not path or path.startswith("/") or ".." in Path(path).parts:
        raise RuntimeError("Invalid inherited-debt evidence path")
    if revision is None:
        target = repo / path
        if target.is_symlink() or not target.resolve().is_relative_to(repo.resolve()):
            raise RuntimeError("Inherited-debt evidence must be a regular repository file")
        if not target.is_file():
            return None
        data = target.read_bytes()
        sha = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        return sha, data
    row = gitio.git(repo, "ls-tree", revision, "--", path).strip()
    if not row:
        return None
    fields = row.split()
    if len(fields) < 3 or fields[0] not in {"100644", "100755"} or fields[1] != "blob":
        raise RuntimeError("Inherited-debt evidence must be a regular Git blob")
    sha = fields[2]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise RuntimeError("Invalid inherited-debt Git object")
    result = subprocess.run(["git", "cat-file", "blob", sha], cwd=repo, capture_output=True,
                            timeout=120, check=True)
    return sha, result.stdout


def _ancestor(repo: Path, old: str, new: str) -> bool:
    result = subprocess.run(["git", "merge-base", "--is-ancestor", old, new], cwd=repo,
                            capture_output=True, timeout=120, check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError("Cannot verify inherited-debt ancestry; fetch complete pinned history")
    return result.returncode == 0


def _policy(repo: Path, base: str, head: str | None):
    stored = blob(repo, head, MANIFEST)
    if stored is None:
        return None  # Ordinary repositories/PRs are unaffected.
    upstream = gitio.commit_or_none(repo, UPSTREAM)
    if upstream != UPSTREAM:
        raise RuntimeError("Pinned upstream object missing; inherited-debt transition cannot be verified")
    if _ancestor(repo, UPSTREAM, base):
        return None  # The normal main ratchet owns all subsequent changes.
    if base != BASE:
        raise RuntimeError("Inherited-debt transition baseline differs from the reviewed exact base")
    candidate = gitio.commit_or_none(repo, head or "HEAD")
    if candidate is None or not _ancestor(repo, UPSTREAM, candidate) or not _ancestor(repo, base, candidate):
        raise RuntimeError("Inherited-debt transition requires true pinned upstream and base ancestry")
    data = stored[1]
    if hashlib.sha256(data).hexdigest() != MANIFEST_SHA256:
        raise RuntimeError("Inherited-debt manifest drift: independent review is required")
    policy = json.loads(data)
    if policy.get("schema_version") != 1 or policy.get("base") != BASE or policy.get("upstream") != UPSTREAM:
        raise RuntimeError("Invalid pinned inherited-debt policy identity")
    report = policy.get("report", {})
    raw = blob(repo, head, report.get("path", ""))
    if raw is None or hashlib.sha256(raw[1]).hexdigest() != report.get("sha256"):
        raise RuntimeError("Original full code-health report missing or changed")
    return policy


def apply_inherited_debt(repo, base, head, findings, head_measures, measurer):
    """Attach separately reviewed dispositions; never alter raw finding severity."""
    policy = _policy(repo, base, head)
    if policy is None:
        return
    entries = policy.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("Inherited-debt entries must be an explicit reviewed list")
    source_paths: dict[str, set[str]] = {}
    for entry in entries:
        source = entry.get("source", {})
        revision = source.get("commit")
        if revision not in {BASE, UPSTREAM}:
            raise RuntimeError("Inherited-debt source is not an approved pinned parent")
        source_path = source.get("path", "")
        if blob(repo, revision, source_path) is None:
            raise RuntimeError("Pinned inherited-debt source is missing")
        source_paths.setdefault(revision, set()).add(source_path)
    sources = {rev: measurer.measure(rev, sorted(paths)) for rev, paths in source_paths.items()}
    assign_entries(entries, findings, head_measures, sources,
                   lambda rev, path: blob(repo, rev, path), head, MANIFEST_SHA256)


def disposition_counts(findings):
    counts = Counter("inherited" if f.inherited_debt else "unresolved"
                     for f in findings if f.blocking and f.allowed_reason is None)
    return counts["inherited"], counts["unresolved"]
