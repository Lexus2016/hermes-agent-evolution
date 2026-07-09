"""Tests for memory tool file lock timeout (#829).

Verifies that _file_lock uses non-blocking acquisition with bounded retry
and raises TimeoutError instead of hanging indefinitely when the lock
can't be acquired.
"""

import json
import os
import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_file_lock_acquires_normally(tmp_path):
    """A lock with no contention should be acquired immediately."""
    from tools.memory_tool import MemoryStore

    lock_file = tmp_path / "test.lock"
    with MemoryStore._file_lock(lock_file):
        # If we get here, the lock was acquired.
        assert True


def test_file_lock_releases_after_context(tmp_path):
    """After the context manager exits, the lock should be released."""
    from tools.memory_tool import MemoryStore

    lock_file = tmp_path / "test.lock"
    with MemoryStore._file_lock(lock_file):
        pass
    # Should be able to acquire again immediately.
    with MemoryStore._file_lock(lock_file):
        pass


def test_file_lock_timeout_raises_timeout_error(tmp_path):
    """When the lock is held by another thread, a TimeoutError is raised."""
    import fcntl
    if fcntl is None:
        pytest.skip("fcntl not available on this platform")

    from tools.memory_tool import MemoryStore

    lock_file = tmp_path / "contended.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # Acquire the lock in a separate thread and hold it.
    held_fd = open(lock_file, "a+", encoding="utf-8")
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        # Use a short timeout so the test doesn't take long.
        with pytest.raises(TimeoutError) as exc_info:
            # We need to call _acquire_fcntl_lock directly with a short timeout.
            fd2 = open(lock_file, "a+", encoding="utf-8")
            try:
                MemoryStore._acquire_fcntl_lock(fd2, timeout=0.5)
            finally:
                fd2.close()

        assert "memory file lock" in str(exc_info.value).lower() or "lock" in str(exc_info.value).lower()
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        held_fd.close()


def test_file_lock_no_fcntl_no_msvcrt_yields_directly(tmp_path):
    """When fcntl and msvcrt are both None, the lock is a no-op."""
    from tools.memory_tool import MemoryStore

    lock_file = tmp_path / "test.lock"
    # Patch both to None — the context manager should yield without blocking.
    with patch("tools.memory_tool.fcntl", None), \
         patch("tools.memory_tool.msvcrt", None):
        with MemoryStore._file_lock(lock_file):
            assert True


def test_acquire_fcntl_lock_with_contention_retries(tmp_path):
    """_acquire_fcntl_lock retries with backoff before giving up."""
    import fcntl
    if fcntl is None:
        pytest.skip("fcntl not available on this platform")

    from tools.memory_tool import MemoryStore

    lock_file = tmp_path / "retry.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # Hold the lock in another thread, release it after a short delay.
    held_fd = open(lock_file, "a+", encoding="utf-8")
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release_after_delay():
        time.sleep(0.3)
        fcntl.flock(held_fd, fcntl.LOCK_UN)

    releaser = threading.Thread(target=release_after_delay, daemon=True)
    releaser.start()

    try:
        # Should succeed after the lock is released (~0.3s).
        fd2 = open(lock_file, "a+", encoding="utf-8")
        try:
            MemoryStore._acquire_fcntl_lock(fd2, timeout=5.0)
            # If we get here, the retry worked.
            fcntl.flock(fd2, fcntl.LOCK_UN)
        finally:
            fd2.close()
    finally:
        releaser.join(timeout=2)
        try:
            fcntl.flock(held_fd, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        held_fd.close()


def test_lock_timeout_constant_exists():
    """The _LOCK_TIMEOUT_SECONDS class constant exists and is reasonable."""
    from tools.memory_tool import MemoryStore
    assert hasattr(MemoryStore, "_LOCK_TIMEOUT_SECONDS")
    assert MemoryStore._LOCK_TIMEOUT_SECONDS > 0
    assert MemoryStore._LOCK_TIMEOUT_SECONDS <= 60  # sanity: not minutes


def test_memory_save_and_load_under_lock(tmp_path):
    """Memory operations work correctly under the file lock."""
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.add("memory", "test entry one")
    store.save_to_disk("memory")

    store2 = MemoryStore()
    store2._reload_target("memory")
    entries = store2._entries_for("memory")
    assert "test entry one" in entries


if __name__ == "__main__":
    pytest.main([__file__, "-v"])