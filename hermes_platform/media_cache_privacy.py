"""Permission-checked media cache creation, before any private payload is written."""

import logging
import os
import stat
from contextlib import contextmanager
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
    expected_uid = uid if uid is not None and uid >= 0 else os.geteuid()
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


def _remove_owned_temporary(filepath: Path, identity: tuple[int, int]) -> None:
    """Leave replacements alone; this is an identity check, not an atomic unlink guarantee."""
    try:
        info = filepath.lstat()
    except FileNotFoundError:
        return
    if (info.st_dev, info.st_ino) == identity:
        filepath.unlink()


def _finish_private_temporary(filepath, identity, close, failed: bool) -> None:
    """Preserve the primary stream failure across close and owned-path cleanup."""
    primary = failed
    try:
        try:
            close()
        except OSError as exc:
            primary = True
            if not failed:
                raise
            logging.getLogger(__name__).warning(
                "Media temporary close incomplete (%s)", type(exc).__name__,
            )
    finally:
        if identity is not None:
            try:
                _remove_owned_temporary(filepath, identity)
            except OSError as exc:
                if not primary:
                    raise
                logging.getLogger(__name__).warning(
                    "Media temporary cleanup incomplete (%s)", type(exc).__name__,
                )


@contextmanager
def private_temporary_file(filepath: Path):
    """Yield an exclusive private stream without modifying its caller-owned parent.

    POSIX ownership and mode are checked before yielding. On other hosts only
    exclusive creation is guaranteed; native ACL privacy is outside this contract.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if _posix_permissions():
        flags |= os.O_NOFOLLOW
    fd = os.open(filepath, flags, 0o600)
    identity = None
    failed = False
    def close():
        os.close(fd)
    try:
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        handle = os.fdopen(fd, "wb")
        close = handle.close
        if _posix_permissions():
            from hermes_constants import _chown_to_hermes_uid

            _chown_to_hermes_uid(handle.fileno())
            info = os.fstat(handle.fileno())
            _verify_owner(info)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise PermissionError("Media stream file privacy could not be established")
        yield handle
    except BaseException:
        failed = True
        raise
    finally:
        _finish_private_temporary(filepath, identity, close, failed)


def publish_private_temporary_file(handle, temporary: Path, destination: Path) -> None:
    """Publish only the captured stream inode; lstat→rename is not an atomic identity guard."""
    from utils import atomic_replace

    opened = os.fstat(handle.fileno())
    handle.close()  # Windows requires closing before replacement.
    current = temporary.lstat()
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or not stat.S_ISREG(current.st_mode):
        raise PermissionError("Media stream temporary ownership changed before publication")
    atomic_replace(temporary, destination)
