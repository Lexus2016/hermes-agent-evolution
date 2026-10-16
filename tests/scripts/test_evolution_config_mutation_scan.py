"""Tests for the config mutation baseline+diff runner (issue #89 slice A).

The runner is the ``no_agent`` cron entry point
(``evolution-config-mutation-scan``, 03:15 daily) — these tests exercise its
real ``main()`` against temp trees, covering the snapshot, the diff primitive,
baseline init/auto-init, exclude behavior and the report/exit-code contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import evolution_config_mutation_scan as runner


def _tree(root: Path) -> None:
    """Build a small config/skill/hook tree. Parent dirs are created."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("key: value\n", encoding="utf-8")
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# Demo skill\n", encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "helper.py").write_text("print('x')\n", encoding="utf-8")
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "volatile.json").write_text("{}\n", encoding="utf-8")
    (root / "auth.lock").write_text("", encoding="utf-8")


def _scanroot(tmp_path: Path) -> Path:
    """A dedicated subtree to scan; baseline/report live OUTSIDE it so the
    artifacts themselves are never reported as mutations."""
    root = tmp_path / "scanroot"
    _tree(root)
    return root


def _out(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    return out / "baseline.json", out / "report.json"


def test_snapshot_hashes_and_excludes(tmp_path: Path) -> None:
    snap = runner.snapshot_tree(_scanroot(tmp_path))
    # config + skill + script recorded
    assert "config.yaml" in snap
    assert "skills/demo/SKILL.md" in snap
    assert "scripts/helper.py" in snap
    # volatile subtree and lock file excluded
    assert "cache/volatile.json" not in snap
    assert "auth.lock" not in snap
    assert snap["config.yaml"]["sha256"]
    assert snap["config.yaml"]["size"] == len("key: value\n")
    assert snap["config.yaml"]["mtime_ns"] > 0


def test_detect_added_modified_removed(tmp_path: Path) -> None:
    root = _scanroot(tmp_path)
    baseline = runner.snapshot_tree(root)
    (root / "config.yaml").write_text("key: CHANGED\n", encoding="utf-8")
    (root / "skills" / "demo" / "SKILL.md").unlink()
    (root / "new_hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    current = runner.snapshot_tree(root)
    mut = runner.detect_mutations(baseline, current)
    assert mut["added"] == ["new_hook.sh"]
    assert mut["removed"] == ["skills/demo/SKILL.md"]
    assert len(mut["modified"]) == 1
    assert mut["modified"][0]["path"] == "config.yaml"
    assert mut["modified"][0]["before_sha256"] != mut["modified"][0]["after_sha256"]


def test_init_writes_baseline(tmp_path: Path) -> None:
    root = _scanroot(tmp_path)
    baseline, _ = _out(tmp_path)
    code = runner.main(["prog", "--init", "--root", str(root), "--baseline", str(baseline)])
    assert code == 0
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert data["roots"] == [str(root)]
    assert "scanroot/config.yaml" in data["files"]
    assert data["files"]["scanroot/config.yaml"]["sha256"]


def test_scan_auto_initializes_when_baseline_missing(tmp_path: Path) -> None:
    root = _scanroot(tmp_path)
    baseline, report = _out(tmp_path)
    code = runner.main(
        ["prog", "--root", str(root), "--baseline", str(baseline), "--report", str(report)]
    )
    assert code == 0
    assert baseline.is_file()  # bootstrapped itself
    assert not report.exists()  # no diff on the init run


def test_scan_reports_mutations(tmp_path: Path) -> None:
    root = _scanroot(tmp_path)
    baseline, report = _out(tmp_path)
    args = ["prog", "--root", str(root), "--baseline", str(baseline), "--report", str(report)]
    assert runner.main([*args, "--init"]) == 0
    (root / "config.yaml").write_text("key: mutated\n", encoding="utf-8")
    (root / "sneaky.json").write_text('{"hook": true}\n', encoding="utf-8")
    assert runner.main(args) == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["changed_total"] == 2
    assert data["added"] == ["scanroot/sneaky.json"]
    assert data["modified"][0]["path"] == "scanroot/config.yaml"
    assert data["files_baselined"] == 3


def test_fail_on_changes_exit_code(tmp_path: Path) -> None:
    root = _scanroot(tmp_path)
    baseline, report = _out(tmp_path)
    args = [
        "prog",
        "--root",
        str(root),
        "--baseline",
        str(baseline),
        "--report",
        str(report),
        "--fail-on-changes",
    ]
    assert runner.main([*args, "--init"]) == 0
    (root / "config.yaml").write_text("key: mutated\n", encoding="utf-8")
    assert runner.main(args) == 2


def test_project_root_prefixing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    (home / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    (proj / "config.yaml").write_text("b: 2\n", encoding="utf-8")
    snap = runner.snapshot_roots([home, proj])
    assert "home/config.yaml" in snap
    assert "proj/config.yaml" in snap
    assert snap["home/config.yaml"]["sha256"] != snap["proj/config.yaml"]["sha256"]


def test_missing_root_is_clean_noop(tmp_path: Path) -> None:
    baseline, report = _out(tmp_path)
    args = [
        "prog",
        "--root",
        str(tmp_path / "nonexistent"),
        "--baseline",
        str(baseline),
        "--report",
        str(report),
    ]
    assert runner.main([*args, "--init"]) == 0
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert data["files"] == {}
