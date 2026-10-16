"""Synthetic capture and vision callers never persist bytes after privacy failures."""

import asyncio
import base64
import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from tools.computer_use import tool as cu
from tools.computer_use.backend import CaptureResult, UIElement
from tools import vision_tools as vision


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_HOME_MODE", "HERMES_UID", "HERMES_GID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    root = tmp_path / "cache-root"
    for sub in ("cache/images", "cache/computer_use", "cache/vision", "cache/video"):
        (root / sub).mkdir(parents=True, mode=0o755)
    monkeypatch.setattr("hermes_constants.get_hermes_dir", lambda new, old: root / new)
    monkeypatch.setattr(vision, "get_hermes_dir", lambda new, old: root / new)
    monkeypatch.setattr(cu, "_should_route_through_aux_vision", lambda: False)
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (30, 60, 90)).save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    cap = CaptureResult(mode="som", width=100, height=100, png_b64=encoded,
                        elements=[UIElement(1, "button", "synthetic label")])
    provider = AsyncMock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(vision, "async_call_llm", provider)
    monkeypatch.setattr(vision, "_load_auxiliary_client", lambda: None)
    monkeypatch.setattr(vision, "_aux_call_kwargs", lambda *args, **kwargs: {})
    return SimpleNamespace(root=root, cap=cap, url="data:image/png;base64," + encoded, provider=provider)


def _inject_failure(monkeypatch, failure):
    if failure == "directory":
        def denied(_fd):
            raise PermissionError("synthetic privacy failure")
        monkeypatch.setattr("hermes_cli.config._secure_dir", denied)
    elif failure == "ineffective_directory":
        monkeypatch.setattr("hermes_cli.config._secure_dir", lambda fd: None)
        monkeypatch.setattr(os, "fchmod", lambda *args: None)
    else:
        original = os.open
        def denied_file(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                raise PermissionError("synthetic private open failure")
            return original(path, flags, *args, **kwargs)
        monkeypatch.setattr(os, "open", denied_file)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failure", ["directory", "ineffective_directory", "open"])
@pytest.mark.parametrize("surface", ["capture", "native", "auxiliary"])
def test_public_media_flow_refuses_persistence_after_privacy_failure(media, monkeypatch, failure, surface):
    _inject_failure(monkeypatch, failure)
    if surface == "capture":
        result = cu._capture_response(media.cap, max_elements=0)
        assert result["_multimodal"]  # Inline capture survives an optional cache failure.
        assert "screenshot_path" not in result["meta"]
        assert "elements_file" not in result["meta"]
    else:
        call = (vision._vision_analyze_native(media.url, "synthetic") if surface == "native"
                else vision.vision_analyze_tool(media.url, "synthetic"))
        result = asyncio.run(call)
        assert isinstance(result, str), result
        assert json.loads(result).get("success") is False
    media.provider.assert_not_called()
    assert not [p for p in media.root.rglob("*") if p.is_file()]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failure", ["directory", "ineffective_directory", "open"])
def test_backend_video_privacy_failure_precedes_payload(media, monkeypatch, failure):
    from tools import image_source
    monkeypatch.setattr(image_source, "_is_local_terminal_backend", lambda: False)
    monkeypatch.setattr(image_source, "resolve_image_source", AsyncMock(
        return_value=image_source.ResolvedImage(b"synthetic video", "video/mp4", "container")))
    _inject_failure(monkeypatch, failure)
    with pytest.raises(PermissionError):
        asyncio.run(vision._materialize_video("synthetic.mp4", "synthetic-task", []))
    assert not [p for p in media.root.rglob("*") if p.is_file()]


@pytest.mark.platforms("posix")
def test_capture_image_and_element_spill_are_private(media):
    result = cu._capture_response(media.cap, max_elements=0)
    for name in ("screenshot_path", "elements_file"):
        path = Path(result["meta"][name])
        assert path.stat().st_uid == os.geteuid()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert path.read_bytes()


@pytest.mark.platforms("posix")
def test_capture_managed_directory_policy_is_preserved(media, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config._secure_dir", lambda fd: None)
    result = cu._capture_response(media.cap, max_elements=0)
    for name in ("screenshot_path", "elements_file"):
        path = Path(result["meta"][name])
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.platforms("posix")
def test_capture_cache_symlink_never_writes_destination(media, tmp_path):
    cache = media.root / "cache/images"
    cache.rmdir()
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    cache.symlink_to(destination, target_is_directory=True)
    result = cu._capture_response(media.cap)
    assert "screenshot_path" not in result["meta"]
    assert not list(destination.iterdir())


@pytest.mark.platforms("windows")
def test_capture_non_posix_writes_supported_without_mode_claim(media):
    result = cu._capture_response(media.cap, max_elements=0)
    assert Path(result["meta"]["screenshot_path"]).read_bytes()
    assert Path(result["meta"]["elements_file"]).read_bytes()
