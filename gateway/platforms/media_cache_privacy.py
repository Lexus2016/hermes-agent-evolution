"""Permission-checked media cache creation, before any private payload is written."""

import os
import stat
from pathlib import Path


def _posix_permissions() -> bool:
    return os.name == "posix"


def _shared_policy() -> bool:
    # A failed policy lookup must never be interpreted as permission to share.
    from hermes_cli.config import is_managed

    if is_managed():
        return True
    configured = os.environ.get("HERMES_HOME_MODE", "").strip()
    try:
        mode = int(configured, 8) if configured else 0
        return 0 <= mode <= 0o7777 and bool(mode & 0o077)
    except ValueError:
        return False


def _verify_owner(info) -> None:
    from hermes_constants import _resolve_hermes_uid_gid

    uid, gid = _resolve_hermes_uid_gid()
    expected_uid = uid if uid is not None and uid >= 0 else os.geteuid()  # windows-footgun: ok — POSIX-only callers
    if info.st_uid != expected_uid or (gid is not None and gid >= 0 and info.st_gid != gid):
        raise PermissionError("Media cache ownership could not be established")


def _verify_private_directory(fd: int) -> None:
    info = os.fstat(fd)
    _verify_owner(info)
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise PermissionError("Media cache directory privacy could not be established")


def _open_directory(cache_dir: Path) -> int:
    return os.open(cache_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def secure_media_cache_dir(cache_dir: Path) -> None:
    """Honor explicit sharing; otherwise require an owner-private POSIX directory."""
    shared = _shared_policy()
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o777 if shared else 0o700)
    if not _posix_permissions():
        return
    fd = _open_directory(cache_dir)
    try:
        from hermes_cli.config import _secure_dir

        # The canonical helper accepts an fd: chmod/chown cannot follow a replaced path.
        _secure_dir(fd)
        if not shared:
            # Restrict the inode even when container policy skips chmod or mode is invalid.
            os.fchmod(fd, 0o700)
            _verify_private_directory(fd)
    finally:
        os.close(fd)


def write_cache_file(filepath: Path, data: bytes) -> None:
    """Create an exclusive private file; permission failures never retry insecurely."""
    shared = _shared_policy()
    if not _posix_permissions():
        # POSIX modes are advisory on Windows; exclusive creation still avoids overwrite.
        with filepath.open("xb") as handle:
            handle.write(data)
        return
    directory_fd = _open_directory(filepath.parent)
    try:
        if not shared:
            _verify_private_directory(directory_fd)
        fd = os.open(filepath.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as handle:
            from hermes_constants import _chown_to_hermes_uid

            _chown_to_hermes_uid(handle.fileno())
            info = os.fstat(handle.fileno())
            if not shared:
                _verify_owner(info)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise PermissionError("Media cache file privacy could not be established")
            handle.write(data)
    finally:
        os.close(directory_fd)
