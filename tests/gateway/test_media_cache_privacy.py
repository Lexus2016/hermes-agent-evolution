"""Both main-native inbound media writers establish privacy before payload writes."""
import os
import stat
from pathlib import Path

import pytest

from gateway.platforms import base, media_cache_privacy as privacy


@pytest.fixture
def cache(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".hermes").mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("HERMES_HOME_MODE", "HERMES_UID", "HERMES_GID", "HERMES_CONTAINER", "HERMES_SKIP_CHMOD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("hermes_constants._detect_container", lambda: False)
    # Initialize the ordinary home before testing cache-only mode policy; config
    # initialization otherwise applies deliberately malformed modes to the home itself.
    from hermes_cli.config import ensure_hermes_home
    ensure_hermes_home()
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    path = home / "media"
    for name in ("IMAGE_CACHE_DIR", "DOCUMENT_CACHE_DIR"):
        monkeypatch.setattr(base, name, path)
    return path


def _write(kind="image"):
    if kind == "document":
        return Path(base.cache_document_from_bytes(b"private document", "report.txt"))
    return Path(base.cache_image_from_bytes(b"\x89PNG\r\n\x1a\nprivate"))


@pytest.mark.platforms("linux", "macos")
def test_new_private_directory_and_file(cache):
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
def test_non_posix_new_private_file_is_supported(cache):
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


@pytest.mark.platforms("linux")
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
    elif policy.startswith(("container_", "skip_chmod_", "detected_")):
        signal, mode = policy.rsplit("_", 1)
        if signal == "detected":
            monkeypatch.setattr("hermes_constants._detect_container", lambda: True)
        else:
            name = "HERMES_CONTAINER" if signal == "container" else "HERMES_SKIP_CHMOD"
            monkeypatch.setenv(name, "1")
        if mode == "preserved":
            expected_mode = 0o750
        else:
            monkeypatch.setenv("HERMES_HOME_MODE", "700" if mode == "private" else "invalid")
    elif policy == "docker_uid_gid":
        from gateway.platforms import media_cache_privacy as privacy
        # Explicit private mode keeps UID/GID verification active inside containers.
        monkeypatch.setenv("HERMES_HOME_MODE", "700")
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
_POLICIES += [f"{signal}_{mode}" for signal in ("container", "skip_chmod", "detected")
              for mode in ("preserved", "private", "invalid")]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("kind", ["image", "document"])
@pytest.mark.parametrize("policy", _POLICIES)
def test_explicit_policy_preserves_private_payload_contract(cache, monkeypatch, kind, policy):
    _positive_policy(cache, monkeypatch, kind, policy)


@pytest.mark.platforms("macos")
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
    elif failure in {"unknown_policy", "unknown_container_policy"}:
        def unavailable():
            raise RuntimeError("synthetic unavailable")
        target = ("hermes_cli.config.is_managed" if failure == "unknown_policy"
                  else "hermes_constants._container_or_chmod_skipped")
        monkeypatch.setattr(target, unavailable)
    elif failure == "directory_owner":
        monkeypatch.setattr("hermes_constants._resolve_hermes_uid_gid", lambda: (os.geteuid() + 1000, None))  # windows-footgun: ok — native POSIX matrix only
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


_FAILURES = ["directory_error", "directory_ineffective", "unknown_policy", "unknown_container_policy", "directory_owner",
             "file_owner", "file_open", "file_mode", "directory_symlink", "file_collision", "file_symlink"]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("kind", ["image", "document"])
@pytest.mark.parametrize("failure", _FAILURES)
def test_privacy_failure_never_writes_payload(cache, monkeypatch, kind, failure):
    _refuse_payload(cache, monkeypatch, kind, failure)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("kind", ["image", "document"])
@pytest.mark.parametrize("failure", _FAILURES)
def test_privacy_failure_never_writes_payload_macos(cache, monkeypatch, kind, failure):
    _refuse_payload(cache, monkeypatch, kind, failure)


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("kind", ["image", "document"])
def test_non_posix_exclusive_write_is_supported(cache, monkeypatch, kind):
    from types import SimpleNamespace
    monkeypatch.setattr(base.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    target = _write(kind)
    assert b"private" in target.read_bytes()
    with pytest.raises(FileExistsError):
        _write(kind)
    assert b"private" in target.read_bytes()
