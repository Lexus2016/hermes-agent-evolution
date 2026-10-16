"""Both main-native inbound media writers establish privacy before payload writes."""
import stat
from pathlib import Path

import pytest

from gateway.platforms import base


@pytest.fixture
def cache(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".hermes").mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("HERMES_HOME_MODE", "HERMES_UID", "HERMES_GID"):
        monkeypatch.delenv(name, raising=False)
    # Initialize the ordinary home before testing cache-only mode policy; config
    # initialization otherwise applies deliberately malformed modes to the home itself.
    from hermes_cli.config import ensure_hermes_home
    ensure_hermes_home()
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    path = home / "media"
    for name in ("IMAGE_CACHE_DIR", "DOCUMENT_CACHE_DIR"):
        monkeypatch.setattr(base, name, path)
    return path


def _write(kind):
    if kind == "document":
        return Path(base.cache_document_from_bytes(b"private document", "report.txt"))
    return Path(base.cache_image_from_bytes(b"\x89PNG\r\n\x1a\nprivate image"))


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["image", "document"])
def test_private_directory_and_file(cache, kind):
    cache.mkdir(mode=0o755)
    target = _write(kind)
    assert b"private" in target.read_bytes()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def _positive_policy(cache, monkeypatch, kind, policy):
    import os
    from types import SimpleNamespace
    from gateway.platforms import media_cache_privacy as privacy

    cache.mkdir(mode=0o750)
    expected_mode = 0o700
    if policy == "managed":
        monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
        monkeypatch.setattr("hermes_cli.config._secure_dir", lambda fd: None)
        expected_mode = 0o750
    elif policy == "shared":
        monkeypatch.setenv("HERMES_HOME_MODE", "750")
        expected_mode = 0o750
    elif policy in {"invalid", "negative", "out_of_range"}:
        monkeypatch.setenv("HERMES_HOME_MODE", {"invalid": "invalid", "negative": "-1",
                                              "out_of_range": "10000"}[policy])
    elif policy == "docker_uid_gid":
        monkeypatch.setenv("HERMES_CONTAINER", "1")
        monkeypatch.setenv("HERMES_UID", "12345")
        monkeypatch.setenv("HERMES_GID", "23456")
        original = os.fstat
        assigned = {}
        def assign(fd):
            assigned[fd] = (12345, 23456)
        def observed(fd):
            info = original(fd)
            uid, gid = assigned.get(fd, (info.st_uid, info.st_gid))
            return SimpleNamespace(st_mode=info.st_mode, st_uid=uid, st_gid=gid)
        # Exercise the canonical config parser, but never change a real account's ownership.
        monkeypatch.setattr("hermes_cli.config._secure_dir", assign)
        monkeypatch.setattr("hermes_constants._chown_to_hermes_uid", assign)
        monkeypatch.setattr(privacy.os, "fstat", observed)
    old_umask = os.umask(0)
    try:
        target = _write(kind)
    finally:
        os.umask(old_umask)
    assert b"private" in target.read_bytes()
    assert stat.S_IMODE(cache.stat().st_mode) == expected_mode
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


_POLICIES = ["private", "managed", "shared", "invalid", "negative", "out_of_range", "docker_uid_gid"]


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["image", "document"])
@pytest.mark.parametrize("policy", _POLICIES)
def test_explicit_policy_preserves_private_payload_contract(cache, monkeypatch, kind, policy):
    _positive_policy(cache, monkeypatch, kind, policy)


@pytest.mark.macos_only
@pytest.mark.parametrize("kind", ["image", "document"])
@pytest.mark.parametrize("policy", _POLICIES)
def test_explicit_policy_preserves_private_payload_contract_macos(cache, monkeypatch, kind, policy):
    _positive_policy(cache, monkeypatch, kind, policy)


def _refuse_payload(cache, monkeypatch, kind, failure):
    import os
    from types import SimpleNamespace
    from gateway.platforms import media_cache_privacy as privacy

    preserved = None
    if failure == "directory_symlink":
        destination = cache.parent / "elsewhere"
        destination.mkdir()
        cache.symlink_to(destination, target_is_directory=True)
    else:
        cache.mkdir(mode=0o755)
    if failure == "directory_error":
        def denied(*args):
            raise PermissionError("synthetic denied")
        monkeypatch.setattr(privacy.os, "fchmod", denied)
    elif failure == "directory_ineffective":
        monkeypatch.setattr(privacy.os, "fchmod", lambda *args: None)
        monkeypatch.setattr("hermes_cli.config._secure_dir", lambda fd: None)
    elif failure == "unknown_policy":
        def unavailable():
            raise RuntimeError("synthetic unavailable")
        monkeypatch.setattr("hermes_cli.config.is_managed", unavailable)
    elif failure == "directory_owner":
        monkeypatch.setattr("hermes_constants._resolve_hermes_uid_gid", lambda: (os.geteuid() + 1000, None))
        monkeypatch.setattr("hermes_cli.config._secure_dir", lambda fd: None)
    elif failure == "file_owner":
        original_stat = os.fstat
        def wrong_owner(fd):
            info = original_stat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_gid=info.st_gid,
                                   st_uid=info.st_uid + (1000 if stat.S_ISREG(info.st_mode) else 0))
        monkeypatch.setattr(privacy.os, "fstat", wrong_owner)
    elif failure in {"file_open", "file_mode"}:
        original_open = os.open
        def faulty_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and failure == "file_open":
                raise PermissionError("synthetic denied")
            fd = original_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                os.fchmod(fd, 0o644)
            return fd
        monkeypatch.setattr(privacy.os, "open", faulty_open)
        monkeypatch.setattr(Path, "write_bytes", lambda *args: pytest.fail("insecure fallback"))
    elif failure in {"file_collision", "file_symlink"}:
        monkeypatch.setattr(base.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
        name = "img_aaaaaaaaaaaa.jpg" if kind == "image" else "doc_aaaaaaaaaaaa_report.txt"
        target = cache / name
        if failure == "file_collision":
            preserved = target
        else:
            preserved = cache.parent / "preserved"
            target.symlink_to(preserved)
        preserved.write_bytes(b"preserved")
    with pytest.raises((OSError, RuntimeError, ValueError)):
        _write(kind)
    if preserved is not None:
        assert preserved.read_bytes() == b"preserved"
    else:
        # A refused descriptor may leave an empty file, never private payload bytes.
        assert all(path.read_bytes() == b"" for path in cache.iterdir())


_FAILURES = ["directory_error", "directory_ineffective", "unknown_policy", "directory_owner",
             "file_owner", "file_open", "file_mode", "directory_symlink", "file_collision", "file_symlink"]


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["image", "document"])
@pytest.mark.parametrize("failure", _FAILURES)
def test_privacy_failure_never_writes_payload(cache, monkeypatch, kind, failure):
    _refuse_payload(cache, monkeypatch, kind, failure)


@pytest.mark.macos_only
@pytest.mark.parametrize("kind", ["image", "document"])
@pytest.mark.parametrize("failure", _FAILURES)
def test_privacy_failure_never_writes_payload_macos(cache, monkeypatch, kind, failure):
    _refuse_payload(cache, monkeypatch, kind, failure)


@pytest.mark.windows_only
@pytest.mark.parametrize("kind", ["image", "document"])
def test_non_posix_exclusive_write_is_supported(cache, monkeypatch, kind):
    from types import SimpleNamespace
    monkeypatch.setattr(base.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    target = _write(kind)
    assert b"private" in target.read_bytes()
    with pytest.raises(FileExistsError):
        _write(kind)
    assert b"private" in target.read_bytes()
