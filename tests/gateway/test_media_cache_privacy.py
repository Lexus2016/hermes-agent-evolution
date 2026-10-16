"""Inbound media never falls back to default-mode writes after privacy failure."""

import os
import stat
from pathlib import Path

import pytest

from gateway.platforms import base, media_cache_privacy as privacy


@pytest.fixture
def cache(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("HERMES_HOME_MODE", raising=False)
    monkeypatch.delenv("HERMES_UID", raising=False)
    monkeypatch.delenv("HERMES_GID", raising=False)
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    path = home / "media"
    monkeypatch.setattr(base, "IMAGE_CACHE_DIR", path)
    return path


def _write():
    return Path(base.cache_image_from_bytes(b"\x89PNG\r\n\x1a\nprivate"))


@pytest.mark.platforms("linux", "macos")
def test_private_directory_and_file(cache):
    target = _write()
    assert target.read_bytes().endswith(b"private")
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.platforms("linux", "macos")
def test_permissive_existing_directory_is_repaired(cache):
    cache.mkdir(mode=0o755)
    target = _write()
    assert target.exists()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700


@pytest.mark.platforms("linux", "macos")
def test_failed_directory_hardening_writes_nothing(cache, monkeypatch):
    cache.mkdir(mode=0o755)
    monkeypatch.setattr(privacy.os, "fchmod", lambda *args: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(PermissionError):
        _write()
    assert list(cache.iterdir()) == []


@pytest.mark.platforms("linux", "macos")
def test_ineffective_directory_hardening_writes_nothing(cache, monkeypatch):
    cache.mkdir(mode=0o755)
    monkeypatch.setattr(privacy.os, "fchmod", lambda *args: None)
    monkeypatch.setattr("hermes_cli.config._secure_dir", lambda fd: None)
    with pytest.raises(PermissionError, match="privacy"):
        _write()
    assert list(cache.iterdir()) == []


@pytest.mark.platforms("linux", "macos")
def test_secure_file_open_failure_has_no_plain_write_fallback(cache, monkeypatch):
    original = os.open

    def deny_file(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise PermissionError("denied")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(privacy.os, "open", deny_file)
    monkeypatch.setattr(Path, "write_bytes", lambda *args: pytest.fail("insecure fallback"))
    with pytest.raises(PermissionError):
        _write()
    assert list(cache.iterdir()) == []


def test_unknown_policy_cannot_grant_sharing(cache, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: (_ for _ in ()).throw(RuntimeError("unavailable")))
    with pytest.raises(RuntimeError, match="unavailable"):
        _write()
    assert not cache.exists()


@pytest.mark.platforms("linux", "macos")
def test_explicit_managed_sharing_is_preserved(cache, monkeypatch):
    cache.mkdir(mode=0o750)
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config._secure_dir", lambda path: None)
    target = _write()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o750
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.platforms("linux", "macos")
def test_explicit_shared_mode_is_preserved(cache, monkeypatch):
    monkeypatch.setenv("HERMES_HOME_MODE", "750")
    target = _write()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o750
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.platforms("windows")
def test_non_posix_exclusive_write_is_supported(cache):
    target = _write()
    assert target.read_bytes().endswith(b"private")


@pytest.mark.platforms("linux", "macos")
def test_directory_symlink_is_rejected(cache, tmp_path):
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    cache.symlink_to(destination, target_is_directory=True)
    with pytest.raises(OSError):
        _write()
    assert list(destination.iterdir()) == []


@pytest.mark.platforms("linux", "macos")
def test_file_collision_does_not_overwrite(cache):
    base._secure_media_cache_dir(cache)
    target = cache / "existing"
    target.write_bytes(b"preserved")
    with pytest.raises(FileExistsError):
        privacy.write_cache_file(target, b"private")
    assert target.read_bytes() == b"preserved"


@pytest.mark.platforms("linux", "macos")
def test_file_symlink_does_not_follow_target(cache, tmp_path):
    base._secure_media_cache_dir(cache)
    target = tmp_path / "target"
    target.write_bytes(b"preserved")
    link = cache / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        privacy.write_cache_file(link, b"private")
    assert target.read_bytes() == b"preserved"


@pytest.mark.platforms("linux", "macos")
def test_insecure_effective_file_mode_blocks_payload(cache, monkeypatch):
    original = os.open

    def insecure_file(path, flags, *args, **kwargs):
        fd = original(path, flags, *args, **kwargs)
        if flags & os.O_CREAT:
            os.fchmod(fd, 0o644)
        return fd

    monkeypatch.setattr(privacy.os, "open", insecure_file)
    with pytest.raises(PermissionError, match="file privacy"):
        _write()
    files = list(cache.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b""


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("mode", ["invalid", "-1", "10000"])
def test_invalid_mode_cannot_grant_sharing(cache, monkeypatch, mode):
    monkeypatch.setenv("HERMES_HOME_MODE", mode)
    target = _write()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.platforms("linux", "macos")
def test_explicit_operator_uid_gid_remap_is_verified(cache, monkeypatch):
    from types import SimpleNamespace

    original = os.fstat
    assigned = {}
    monkeypatch.setattr("hermes_constants._resolve_hermes_uid_gid", lambda: (12345, 23456))

    def assign(fd):
        assigned[fd] = (12345, 23456)

    def observed(fd):
        info = original(fd)
        uid, gid = assigned.get(fd, (info.st_uid, info.st_gid))
        return SimpleNamespace(st_mode=info.st_mode, st_uid=uid, st_gid=gid)

    monkeypatch.setattr("hermes_cli.config._secure_dir", assign)
    monkeypatch.setattr("hermes_constants._chown_to_hermes_uid", assign)
    monkeypatch.setattr(privacy.os, "fstat", observed)
    assert _write().read_bytes().endswith(b"private")
    assert assigned


@pytest.mark.platforms("linux", "macos")
def test_unexpected_owner_is_refused_before_payload(cache, monkeypatch):
    monkeypatch.setattr("hermes_constants._resolve_hermes_uid_gid", lambda: (os.geteuid() + 1000, None))
    monkeypatch.setattr("hermes_cli.config._secure_dir", lambda fd: None)
    with pytest.raises(PermissionError, match="ownership"):
        _write()
    assert list(cache.iterdir()) == []
