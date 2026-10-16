"""HTTP streaming keeps synthetic media private before atomic publication."""

import asyncio
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import image_source, vision_tools

PAYLOAD = b"synthetic private image"
URL = "https://example.com/synthetic.png"


class Response:
    headers = {}
    url = URL

    def __init__(self):
        self.chunks = 0
        self.failure = None

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self):
        self.chunks += 1
        yield PAYLOAD
        if self.failure:
            raise self.failure

    def stream(self, *args, **kwargs):
        return self


@pytest.fixture
def stream_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_UID", raising=False)
    monkeypatch.delenv("HERMES_GID", raising=False)
    response = Response()
    monkeypatch.setattr("tools.url_safety.create_ssrf_safe_async_client", lambda **kw: response)
    monkeypatch.setattr(vision_tools, "check_website_access", lambda url: None)
    monkeypatch.setattr(vision_tools.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    destination = tmp_path / "image.img"
    destination.write_bytes(b"ORIGINAL")
    return response, destination, tmp_path / ".image.img.fixed.tmp"


@pytest.mark.platforms("linux", "macos")
def test_image_source_preserves_private_temporary_mode(stream_env, monkeypatch):
    import utils

    _, destination, _ = stream_env
    original_temp = tempfile.NamedTemporaryFile
    original_replace = utils.atomic_replace
    modes = []
    parent_mode = stat.S_IMODE(destination.parent.stat().st_mode)

    def allocate(*args, **kwargs):
        return original_temp(*args, dir=destination.parent, **kwargs)

    def replace(source, target):
        modes.append(stat.S_IMODE(source.stat().st_mode))
        original_replace(source, target)
        modes.append(stat.S_IMODE(target.stat().st_mode))

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", allocate)
    monkeypatch.setattr(utils, "atomic_replace", replace)
    assert asyncio.run(image_source._download_to_bytes(URL)) == PAYLOAD
    assert modes == [0o600, 0o600]
    assert stat.S_IMODE(destination.parent.stat().st_mode) == parent_mode


@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.platforms("linux", "macos")
def test_collision_never_overwrites_or_deletes_foreign_temp(stream_env, symlink):
    response, destination, temporary = stream_env
    victim = destination.parent / "victim"
    victim.write_bytes(b"VICTIM")
    if symlink:
        temporary.symlink_to(victim)
    else:
        temporary.write_bytes(b"COLLISION")
    with pytest.raises(FileExistsError):
        asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert response.chunks == 0
    assert destination.read_bytes() == b"ORIGINAL"
    assert victim.read_bytes() == b"VICTIM"
    assert temporary.is_symlink() if symlink else temporary.read_bytes() == b"COLLISION"


@pytest.mark.parametrize("failure", [RuntimeError("synthetic stream failure"), asyncio.CancelledError()])
def test_failed_stream_preserves_destination_and_removes_owned_temp(stream_env, failure):
    response, destination, temporary = stream_env
    response.failure = failure
    with pytest.raises(type(failure)):
        asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert destination.read_bytes() == b"ORIGINAL"
    assert not temporary.exists()


def test_oversize_preserves_destination_and_cleans_partial(stream_env):
    _, destination, temporary = stream_env
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(vision_tools._download_media(
            URL, destination, 1, media_label="Image", accept="image/*", max_bytes=1,
            timeout=1, retry_all=False,
        ))
    assert destination.read_bytes() == b"ORIGINAL"
    assert not temporary.exists()


@pytest.mark.platforms("linux", "macos")
def test_verification_failure_precedes_first_chunk(stream_env, monkeypatch):
    from hermes_platform import media_cache_privacy

    response, destination, temporary = stream_env

    def refuse(info):
        raise PermissionError("synthetic ownership refusal")

    monkeypatch.setattr(media_cache_privacy, "_verify_owner", refuse)
    with pytest.raises(PermissionError):
        asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert response.chunks == 0
    assert destination.read_bytes() == b"ORIGINAL"
    assert not temporary.exists()


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("invalid", ["mode", "type"])
def test_invalid_descriptor_stops_before_payload(stream_env, monkeypatch, invalid):
    from hermes_platform import media_cache_privacy

    response, destination, temporary = stream_env
    original = os.fstat

    def invalid_info(fd):
        info = original(fd)
        mode = stat.S_IFDIR | 0o600 if invalid == "type" else stat.S_IFREG | 0o644
        return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_uid=info.st_uid,
                               st_gid=info.st_gid, st_mode=mode)

    monkeypatch.setattr(media_cache_privacy.os, "fstat", invalid_info)
    with pytest.raises(PermissionError):
        asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert response.chunks == 0
    assert destination.read_bytes() == b"ORIGINAL"
    assert not temporary.exists()


def test_cleanup_leaves_replaced_temporary_untouched(stream_env, monkeypatch):
    import utils

    _, destination, temporary = stream_env
    displaced = temporary.with_suffix(".owned")

    def replace_then_fail(source, target):
        source.rename(displaced)
        source.write_bytes(b"FOREIGN REPLACEMENT")
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(utils, "atomic_replace", replace_then_fail)
    with pytest.raises(OSError):
        asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert temporary.read_bytes() == b"FOREIGN REPLACEMENT"
    assert displaced.read_bytes() == PAYLOAD
    assert destination.read_bytes() == b"ORIGINAL"


@pytest.mark.platforms("linux", "macos")
def test_configured_ownership_and_shared_parent_still_private(stream_env, monkeypatch):
    _, destination, _ = stream_env
    monkeypatch.setenv("HERMES_UID", str(os.geteuid()))
    monkeypatch.setenv("HERMES_GID", str(os.getegid()))
    monkeypatch.setenv("HERMES_HOME_MODE", "0755")
    before = destination.parent.stat()
    asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    info = destination.stat()
    assert info.st_uid == os.geteuid()
    assert info.st_gid == os.getegid()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert destination.parent.stat().st_mode == before.st_mode
    assert destination.read_bytes() == PAYLOAD


def test_retry_after_stream_failure_uses_clean_temporary(stream_env, monkeypatch):
    import httpx

    response, destination, temporary = stream_env
    response.failure = httpx.ReadTimeout("synthetic timeout")
    waits = []

    async def retry_wait(seconds):
        assert not temporary.exists()
        assert destination.read_bytes() == b"ORIGINAL"
        waits.append(seconds)
        response.failure = None

    monkeypatch.setattr(vision_tools.asyncio, "sleep", retry_wait)
    asyncio.run(vision_tools._download_image(URL, destination, max_retries=2))
    assert waits == [2]
    assert response.chunks == 2
    assert destination.read_bytes() == PAYLOAD
    assert not temporary.exists()


@pytest.mark.platforms("windows")
def test_native_windows_exclusive_stream_success(stream_env):
    _, destination, temporary = stream_env
    asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert destination.read_bytes() == PAYLOAD
    assert not temporary.exists()


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("symlink", [False, True])
def test_substitution_during_stream_refuses_publication(stream_env, monkeypatch, symlink):
    response, destination, temporary = stream_env
    displaced = temporary.with_suffix(".owned")
    victim = destination.parent / "victim"
    victim.write_bytes(b"VICTIM")

    async def substitute():
        yield PAYLOAD
        temporary.rename(displaced)
        if symlink:
            temporary.symlink_to(victim)
        else:
            temporary.write_bytes(b"REPLACEMENT")

    monkeypatch.setattr(response, "aiter_bytes", substitute)
    with pytest.raises(PermissionError, match="ownership changed"):
        asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert destination.read_bytes() == b"ORIGINAL"
    assert displaced.read_bytes() == PAYLOAD
    assert victim.read_bytes() == b"VICTIM"
    assert temporary.is_symlink() if symlink else temporary.read_bytes() == b"REPLACEMENT"


def test_cleanup_failure_preserves_cancellation_and_never_retries(stream_env, monkeypatch, caplog):
    response, destination, temporary = stream_env
    response.failure = asyncio.CancelledError()
    original = Path.lstat

    def deny_cleanup(path, *args, **kwargs):
        if path == temporary:
            raise PermissionError("private synthetic cleanup detail")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", deny_cleanup)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(vision_tools._download_media(
            URL, destination, 3, media_label="Video", accept="video/*", max_bytes=1000,
            timeout=1, retry_all=True,
        ))
    assert response.chunks == 1
    assert destination.read_bytes() == b"ORIGINAL"
    assert temporary.read_bytes() == PAYLOAD
    assert "cleanup incomplete (PermissionError)" in caplog.text
    assert "private synthetic cleanup detail" not in caplog.text


def test_close_failure_preserves_stream_cancellation(stream_env, monkeypatch, caplog):
    from hermes_platform import media_cache_privacy

    response, destination, temporary = stream_env
    response.failure = asyncio.CancelledError()
    original = os.fdopen

    class FailingClose:
        def __init__(self, fd, mode):
            self.handle = original(fd, mode)

        def fileno(self):
            return self.handle.fileno()

        def write(self, data):
            return self.handle.write(data)

        def close(self):
            self.handle.close()
            raise OSError("private synthetic close detail")

    monkeypatch.setattr(media_cache_privacy.os, "fdopen", FailingClose)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert destination.read_bytes() == b"ORIGINAL"
    assert not temporary.exists()
    assert "close incomplete (OSError)" in caplog.text
    assert "private synthetic close detail" not in caplog.text


def test_raw_close_failure_preserves_descriptor_validation_error(stream_env, monkeypatch, caplog):
    from hermes_platform import media_cache_privacy

    response, destination, temporary = stream_env
    original_close = os.close

    def invalid_descriptor(fd):
        raise PermissionError("synthetic descriptor validation")

    def failed_close(fd):
        original_close(fd)
        raise OSError("private synthetic raw close detail")

    monkeypatch.setattr(media_cache_privacy.os, "fstat", invalid_descriptor)
    monkeypatch.setattr(media_cache_privacy.os, "close", failed_close)
    with pytest.raises(PermissionError, match="descriptor validation"):
        asyncio.run(vision_tools._download_image(URL, destination, max_retries=1))
    assert response.chunks == 0
    assert destination.read_bytes() == b"ORIGINAL"
    assert temporary.read_bytes() == b""  # No proven inode identity: leave the empty residue.
    assert "close incomplete (OSError)" in caplog.text
    assert "private synthetic raw close detail" not in caplog.text
