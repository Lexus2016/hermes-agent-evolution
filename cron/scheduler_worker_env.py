"""Cron: import path of the restart-safe external worker.

The worker is spawned as ``sys.executable -m cron.scheduler``. Its entry module is
``cron.scheduler``, not ``hermes_cli.main``, so nothing bootstraps the gateway's checkout
onto its ``sys.path``; historically it imported ``cron`` only through the implicit ``-m``
cwd entry. That entry is gone under ``PYTHONSAFEPATH`` and useless when the venv's
editable install maps a moved/deleted checkout -- the worker then dies with
"No module named 'cron'" before its ownership ack (#112729, hypothesised cause).

The shared subprocess sanitizer strips Hermes-owned PYTHONPATH entries because user
children must not see our tree. This child IS Hermes, so the pin is applied *after* the
env is built, on the sanitized env -- the sanitizer's other decisions (dropped runtime
site-packages, dropped venv markers) stand.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sysconfig
import tarfile
from pathlib import Path

logger = logging.getLogger("cron.scheduler")
_PRISTINE_POPEN = subprocess.Popen


def _installed_purelib() -> Path | None:
    try:
        return Path(sysconfig.get_paths()["purelib"]).resolve()
    except (KeyError, OSError):
        return None


def resolve_worker_tree(repo_root: Path | None = None) -> Path:
    """Resolve the tree path the restart-safe external cron worker imports from.

    In development checkouts, this points to a clean pinned snapshot of HEAD rather than
    the live working directory, ensuring concurrent edits or conflict markers in the
    development tree do not break cron worker dispatch before ownership acknowledgement.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    override = os.environ.get("HERMES_CRON_WORKER_TREE")
    if override:
        override_path = Path(override).resolve()
        if override_path.is_dir():
            return override_path

    if _installed_purelib() == repo_root.resolve():
        return repo_root

    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return repo_root

    try:
        rev_proc = _PRISTINE_POPEN(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        head_out, _ = rev_proc.communicate(timeout=5)
        if rev_proc.returncode != 0:
            return repo_root
        head_sha = head_out.decode("utf-8", errors="replace").strip()
        if len(head_sha) < 7:
            return repo_root

        from hermes_constants import get_hermes_home

        snapshot_base = get_hermes_home() / "cron" / "snapshots"
        snapshot_dir = snapshot_base / head_sha
        marker_file = snapshot_dir / ".complete"

        if marker_file.exists():
            return snapshot_dir

        snapshot_base.mkdir(parents=True, exist_ok=True)
        tmp_dir = snapshot_base / f"tmp-{head_sha}-{os.getpid()}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        archive_proc = _PRISTINE_POPEN(
            ["git", "archive", "--format=tar", head_sha],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if archive_proc.stdout is not None:
            with tarfile.open(fileobj=archive_proc.stdout, mode="r|*") as tar:
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(tmp_dir, filter="data")
                else:
                    tar.extractall(tmp_dir)
        archive_proc.wait(timeout=15)

        if archive_proc.returncode == 0 and (tmp_dir / "cron").is_dir():
            (tmp_dir / ".complete").write_text(head_sha, encoding="utf-8")
            if snapshot_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            else:
                try:
                    os.replace(tmp_dir, snapshot_dir)
                except OSError:
                    if not snapshot_dir.exists():
                        raise
            for entry in snapshot_base.iterdir():
                if entry.is_dir() and entry.name != head_sha and not entry.name.startswith("tmp-"):
                    shutil.rmtree(entry, ignore_errors=True)
            return snapshot_dir
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return repo_root
    except Exception as e:
        logger.debug("Could not resolve pinned snapshot for cron worker: %s", e)
        return repo_root


def pin_hermes_tree_on_pythonpath(worker_env: dict, repo_root: Path) -> dict:
    """Prepend ``repo_root`` to the worker env's own PYTHONPATH (never ``os.environ``'s).

    Skipped when ``repo_root`` is the interpreter's ``purelib``: under a wheel / pipx /
    uv-tool install ``cron/`` lives in site-packages itself, which is already importable,
    and pinning it would move site-packages ahead of the stdlib on ``sys.path``.
    """
    root = str(repo_root)
    if _installed_purelib() == Path(root).resolve():
        return worker_env
    existing = [e for e in worker_env.get("PYTHONPATH", "").split(os.pathsep) if e]
    worker_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([root, *existing]))
    return worker_env
