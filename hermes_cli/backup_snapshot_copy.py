"""WAL-safe bounded quick-snapshot capture phases; facade names remain patchable."""
from pathlib import Path
from typing import Optional

def _vacuum_copy_db(src: Path, dst: Path) -> bool:
    """Copy a SQLite DB via ``VACUUM INTO``, producing a compacted copy (#2489).

    Unlike the backup API (which copies freelist pages verbatim and therefore
    preserves a bloated on-disk size), ``VACUUM INTO`` drops free pages, so a
    ``state.db`` inflated by deleted/updated rows is reined in as it is
    snapshotted. WAL-safe and consistent (the engine reads the WAL for a read-only
    source). Falls back to :func:`_safe_copy_db` when the build lacks
    ``VACUUM INTO`` (SQLite < 3.27) or the copy fails.
    """
    from hermes_cli.backup import (
        _safe_copy_db, logger, sqlite3,
    )
    conn = None
    try:
        conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
        conn.execute("VACUUM INTO ?", (str(dst),))
        return True
    except Exception as exc:
        logger.warning("SQLite VACUUM INTO copy failed for %s: %s", src, exc)
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        return _safe_copy_db(src, dst)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass



def _snapshot_db(
    src: Path, dst: Path, rel_name: str, max_file_size: Optional[int]
) -> str:
    """Snapshot a SQLite DB, compacting free pages only when over the cap (#2489).

    Under the cap (or with no cap) the proven WAL-safe :func:`_safe_copy_db` path
    is used unchanged. Over the cap, a bloated ``state.db`` (free pages from
    deleted/updated rows) is compacted via :func:`_vacuum_copy_db` so it is no
    longer silently skipped just for exceeding the soft cap; the cap then applies
    to the COMPACTED copy. A DB far over the cap is skipped without a costly
    VACUUM (``_COMPACT_ATTEMPT_MAX_RATIO`` bound).

    Returns ``"ok"``, ``"failed"`` (locked/corrupted), or ``"oversized"``
    (compacted copy still exceeds the cap and was removed).
    """
    from hermes_cli.backup import (
        _COMPACT_ATTEMPT_MAX_RATIO, _safe_copy_db, _vacuum_copy_db, logger,
    )
    if max_file_size is None:
        return "ok" if _safe_copy_db(src, dst) else "failed"

    try:
        src_size = src.stat().st_size
    except OSError:
        src_size = None
    if src_size is None or src_size <= max_file_size:
        return "ok" if _safe_copy_db(src, dst) else "failed"
    if src_size > max_file_size * _COMPACT_ATTEMPT_MAX_RATIO:
        logger.warning(
            "Quick snapshot skipped %s: %d bytes far exceeds %d byte limit "
            "(no VACUUM attempt)",
            rel_name,
            src_size,
            max_file_size,
        )
        return "oversized"

    # Over the cap but within the compaction bound: VACUUM-compact, then re-check.
    if not _vacuum_copy_db(src, dst):
        return "failed"
    try:
        size = dst.stat().st_size
    except OSError:
        return "ok"
    if size <= max_file_size:
        return "ok"
    try:
        dst.unlink(missing_ok=True)
    except OSError:
        pass
    logger.warning(
        "Quick snapshot skipped %s: compacted copy %d bytes exceeds %d byte limit",
        rel_name,
        size,
        max_file_size,
    )
    return "oversized"



def _create_quick_snapshot_locked(
    label: Optional[str] = None,
    hermes_home: Optional[Path] = None,
    keep: Optional[int] = None,
    max_file_size: Optional[int] = None,
) -> Optional[str]:
    """Create a quick state snapshot of critical files.

    Copies STATE_FILES to a timestamped directory under state-snapshots/.
    Auto-prunes old snapshots beyond the keep limit.

    Args:
        max_file_size: When set, individual files larger than this many bytes
            are skipped (with a printed warning) instead of copied. Used by
            the pre-update safety snapshot so a multi-GB ``state.db`` can
            never stall ``hermes update`` or silently eat disk — the small
            pairing/cron/config files the snapshot exists to protect are
            always captured. ``None`` (default) copies everything, which
            preserves manual ``/snapshot`` and ``hermes backup --quick``
            behavior.

    Returns:
        Snapshot ID (timestamp-based), or None if no files found.
    """
    from hermes_cli.backup import (
        Path, UTC, _QUICK_DEFAULT_KEEP, _format_size, _prune_quick_snapshots,
        _quick_snapshot_root, _snapshot_recovery_hint, datetime, get_hermes_home,
        json, logger, os, shutil,
    )
    home = hermes_home or get_hermes_home()
    root = _quick_snapshot_root(home)

    def _too_large(path: Path, rel_name: str) -> bool:
        """True (and warn) when ``path`` exceeds the max_file_size cap."""
        if max_file_size is None:
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size <= max_file_size:
            return False
        print(
            f"  ⚠ Snapshot: skipping {rel_name} "
            f"({_format_size(size)} exceeds {_format_size(max_file_size)} limit)"
        )
        logger.warning(
            "Quick snapshot skipped %s: %d bytes exceeds %d byte limit",
            rel_name,
            size,
            max_file_size,
        )
        return True

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base_snap_id = f"{ts}-{label}" if label else ts
    snap_id = base_snap_id
    suffix = 2
    while (root / snap_id).exists():
        snap_id = f"{base_snap_id}-{suffix}"
        suffix += 1
    snap_dir = root / snap_id
    staging_dir = root / f".{snap_id}.{os.getpid()}.partial"
    shutil.rmtree(staging_dir, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(root, 0o700)
    staging_dir.mkdir(mode=0o700, exist_ok=False)
    logger.info("quick snapshot phase=copy status=started id=%s", snap_id)

    manifest, failed_dbs, oversized_skipped = _capture_files(
        home, staging_dir, max_file_size, _too_large)

    if failed_dbs:
        # Critical: update path used to log-and-continue with exit 0, so a
        # missing state.db backup looked like a successful pre-update snapshot
        # (#68474). Surface this on stdout where operators actually look.
        print(
            "  ⚠ CRITICAL: could not snapshot DB file(s): "
            + ", ".join(failed_dbs)
        )
        print(
            f"  ⚠ If sessions disappear after the update, check {root}. {_snapshot_recovery_hint()}"
        )
        logger.error(
            "Quick snapshot failed to capture DB file(s): %s",
            ", ".join(failed_dbs),
        )

    if not manifest:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if failed_dbs:
            # Distinguish "nothing to snapshot" from "state.db present but unreadable"
            print(
                "  ⚠ Snapshot aborted: no files captured "
                f"(failed DBs: {', '.join(failed_dbs)})"
            )
        return None

    # Write manifest
    meta = {
        "id": snap_id,
        "timestamp": ts,
        "label": label,
        "file_count": len(manifest),
        "total_size": sum(manifest.values()),
        "files": manifest,
        "failed_dbs": failed_dbs,
        "oversized_skipped": oversized_skipped,
    }
    with open(staging_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    # Make the staged quick snapshot owner-only before it is published. The
    # staging directory is private from creation, so copied source modes can
    # be normalized safely before the final atomic rename exposes the
    # snapshot. Permission failures are intentionally fatal: publishing a
    # readable recovery bundle is worse than reporting a failed snapshot.
    if os.name != "nt":
        os.chmod(root, 0o700)
        os.chmod(staging_dir, 0o700)
        for path in staging_dir.rglob("*"):
            if path.is_dir():
                os.chmod(path, 0o700)
            elif path.is_file():
                os.chmod(path, 0o600)

    os.replace(staging_dir, snap_dir)

    # Auto-prune. Defaults preserve historical manual /snapshot behavior; callers
    # with known high-churn safety snapshots (for example pre-update) can pass a
    # smaller keep value so large state.db copies do not accumulate indefinitely.
    # #68805 review: skip pruning when a present DB failed to capture OR was
    # skipped for size — either way the snapshot is incomplete and the older
    # snapshot may contain the only recoverable database.
    incomplete = failed_dbs or oversized_skipped
    if not incomplete:
        _prune_quick_snapshots(root, keep=_QUICK_DEFAULT_KEEP if keep is None else keep)
    else:
        if oversized_skipped:
            print(
                "  ⚠ Skipping snapshot prune: DB file(s) skipped for size: "
                + ", ".join(oversized_skipped)
            )
            logger.warning(
                "Quick snapshot skipped oversized DB file(s): %s",
                ", ".join(oversized_skipped),
            )
        logger.warning(
            "Skipping snapshot prune because %d DB(s) failed to capture "
            "and/or %d were oversized — preserving older snapshots as "
            "recovery source",
            len(failed_dbs), len(oversized_skipped),
        )

    logger.info(
        "quick snapshot phase=copy status=complete id=%s files=%d bytes=%d",
        snap_id,
        len(manifest),
        sum(manifest.values()),
    )
    return snap_id



def _capture_files(home, staging_dir, max_file_size, _too_large):
    from hermes_cli.backup import _QUICK_STATE_FILES, _snapshot_db, is_zeroed_sqlite_file, logger, shutil
    manifest: dict[str, int] = {}  # rel_path -> file size
    failed_dbs: list[str] = []  # present *.db that could not be snapshotted
    # #68805: track protected DB files skipped for size — they are snapshot
    # incompleteness just like a failed copy, so pruning must be suppressed
    # to preserve the older complete snapshot that may contain the only
    # recoverable database.
    oversized_skipped: list[str] = []

    for rel in _QUICK_STATE_FILES:
        src = home / rel
        if not src.exists():
            continue

        if src.is_dir():
            _capture_directory(src, home, staging_dir, max_file_size, _too_large,
                               manifest, failed_dbs, oversized_skipped)
            continue

        if not src.is_file():
            continue

        if src.suffix != ".db" and _too_large(src, rel):
            if src.suffix == ".db":
                oversized_skipped.append(rel)
            continue

        dst = staging_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            if src.suffix == ".db":
                outcome = _snapshot_db(src, dst, rel, max_file_size)
                if outcome == "oversized":
                    oversized_skipped.append(rel)
                    continue
                if outcome == "failed":
                    failed_dbs.append(rel)
                    print(
                        f"  ⚠ Snapshot: SQLite safe copy FAILED for {rel} "
                        f"— file may be locked or corrupted"
                    )
                    if is_zeroed_sqlite_file(src):
                        print(
                            f"  ⚠ Snapshot: {rel} looks ZEROED "
                            f"(no SQLite header; {src.stat().st_size} bytes)"
                        )
                    continue
            else:
                shutil.copy2(src, dst)
            manifest[rel] = dst.stat().st_size
        except (OSError, PermissionError) as exc:
            logger.warning("Could not snapshot %s: %s", rel, exc)

    return manifest, failed_dbs, oversized_skipped


def _capture_directory(src, home, staging_dir, max_file_size, _too_large,
                       manifest, failed_dbs, oversized_skipped):
    from hermes_cli.backup import _snapshot_db, is_zeroed_sqlite_file, logger, shutil
    # Walk the directory and record each file individually in the
    # manifest so restore can treat them uniformly.  Empty dirs are
    # skipped (nothing to snapshot).
    for sub in src.rglob("*"):
        if not sub.is_file():
            continue
        sub_rel = sub.relative_to(home).as_posix()
        # Skip heavy, regenerable per-board subtrees (scratch
        # workspaces and task attachments can be large); we only need
        # the board databases + their metadata to restore a board.
        if "/workspaces/" in f"/{sub_rel}/" or "/attachments/" in f"/{sub_rel}/":
            continue
        if sub.suffix != ".db" and _too_large(sub, sub_rel):
            if sub.suffix == ".db":
                oversized_skipped.append(sub_rel)
            continue
        dst = staging_dir / sub_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Route SQLite DBs through the WAL-safe backup() path so a
            # board DB with an open WAL (the gateway may hold it at
            # snapshot time) is captured consistently.
            if sub.suffix == ".db":
                outcome = _snapshot_db(sub, dst, sub_rel, max_file_size)
                if outcome == "oversized":
                    oversized_skipped.append(sub_rel)
                    continue
                if outcome == "failed":
                    failed_dbs.append(sub_rel)
                    print(
                        f"  ⚠ Snapshot: SQLite safe copy FAILED for {sub_rel} "
                        f"— file may be locked or corrupted"
                    )
                    if is_zeroed_sqlite_file(sub):
                        print(
                            f"  ⚠ Snapshot: {sub_rel} looks ZEROED "
                            f"(no SQLite header; {sub.stat().st_size} bytes of NULs?)"
                        )
                    continue
            else:
                shutil.copy2(sub, dst)
            manifest[sub_rel] = dst.stat().st_size
        except (OSError, PermissionError) as exc:
            logger.warning("Could not snapshot %s: %s", sub_rel, exc)
