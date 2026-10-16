"""Feishu text injection bounds actual bytes read without blocking inbound event handling."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.feishu.adapter import FeishuAdapter, _MAX_TEXT_INJECT_BYTES


@pytest.fixture
def adapter():
    return FeishuAdapter(PlatformConfig())


def _intercept_read(source, monkeypatch, on_read, *, on_open=None):
    original_open = Path.open

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, *args, **kwargs):
            value = self.stream.read(*args, **kwargs)
            on_read(value)
            return value

    def intercept(path, *args, **kwargs):
        if path != source:
            return original_open(path, *args, **kwargs)
        if on_open is not None:
            on_open(original_open)
        return Reader(original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", intercept)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, _MAX_TEXT_INJECT_BYTES, _MAX_TEXT_INJECT_BYTES + 1])
async def test_actual_byte_boundary_preserves_empty_documents(adapter, tmp_path, size):
    source = tmp_path / "notes.txt"
    source.write_bytes(b"x" * size)
    result = await adapter._maybe_extract_text_document(str(source), "text/plain")
    expected = "" if size > _MAX_TEXT_INJECT_BYTES else "[Content of notes.txt]:\n" + "x" * size
    assert result == expected


@pytest.mark.asyncio
async def test_file_growth_after_stat_cannot_bypass_read_bound(adapter, tmp_path, monkeypatch):
    source = tmp_path / "notes.txt"
    source.write_bytes(b"small")
    consumed = []

    def grow_before_open(original_open):
        # The old implementation has already accepted the small stat size at this point.
        with original_open(source, "ab") as stream:
            stream.write(b"x" * (_MAX_TEXT_INJECT_BYTES * 2))

    _intercept_read(source, monkeypatch, lambda value: consumed.append(len(value)), on_open=grow_before_open)
    result = await adapter._maybe_extract_text_document(str(source), "text/plain")
    assert result == ""
    assert sum(consumed) <= _MAX_TEXT_INJECT_BYTES + 1


@pytest.mark.asyncio
async def test_bom_utf8_and_universal_newlines_are_preserved(adapter, tmp_path):
    source = tmp_path / "notes.md"
    source.write_bytes(b"\xef\xbb\xbf" + "café\r\nsecond\rthird\nfourth".encode())
    result = await adapter._maybe_extract_text_document(str(source), "text/markdown")
    assert result == "[Content of notes.md]:\ncafé\nsecond\nthird\nfourth"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,media_type,accepted", [
    ("notes.txt", "text/csv", True),
    ("notes.md", "text/x-custom", True),
    ("notes.csv", "text/plain", True),
    ("notes.csv", "text/markdown", True),
    ("notes.csv", "text/csv", False),
    ("notes.txt", "application/octet-stream", False),
])
async def test_mime_and_extension_contract(adapter, tmp_path, filename, media_type, accepted):
    source = tmp_path / filename
    source.write_bytes(b"content")
    result = await adapter._maybe_extract_text_document(str(source), media_type)
    assert result == (f"[Content of {filename}]:\ncontent" if accepted else "")


@pytest.mark.asyncio
async def test_malformed_and_missing_files_keep_empty_fallback(adapter, tmp_path, caplog):
    source = tmp_path / "notes.txt"
    source.write_bytes(b"invalid\xffutf8")
    assert await adapter._maybe_extract_text_document(str(source), "text/plain") == ""
    assert await adapter._maybe_extract_text_document(str(tmp_path / "missing.txt"), "text/plain") == ""
    assert await adapter._maybe_extract_text_document("", "text/plain") == ""
    assert "Failed to inject text document content" in caplog.text


@pytest.mark.asyncio
async def test_blocked_read_keeps_inbound_loop_responsive(adapter, tmp_path, monkeypatch):
    source = tmp_path / "notes.txt"
    source.write_bytes(b"content")
    loop = asyncio.get_running_loop()
    started, release, responsive = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def pause_read(_value):
        started.set()
        if not release.wait(10):
            raise TimeoutError("test observer did not release text read")

    def observe_loop():
        try:
            if started.wait(5):
                loop.call_soon_threadsafe(responsive.set)
                observed.append(responsive.wait(5))
        finally:
            release.set()

    _intercept_read(source, monkeypatch, pause_read)
    observer = threading.Thread(target=observe_loop, daemon=True)
    observer.start()
    try:
        result = await adapter._maybe_extract_text_document(str(source), "text/plain")
    finally:
        release.set()
        await asyncio.to_thread(observer.join, 5)
    assert observed == [True], "text file read prevented inbound loop callbacks"
    assert result == "[Content of notes.txt]:\ncontent"


@pytest.mark.asyncio
async def test_cancelled_read_has_no_late_document_publication(adapter, tmp_path, monkeypatch):
    source = tmp_path / "notes.txt"
    source.write_bytes(b"content")
    loop = asyncio.get_running_loop()
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    display_calls = []

    def pause_read(_value):
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(10):
                raise TimeoutError("test did not release cancelled text read")
        finally:
            loop.call_soon_threadsafe(finished.set)

    def display_name(path):
        display_calls.append(path)
        return "notes.txt"

    _intercept_read(source, monkeypatch, pause_read)
    monkeypatch.setattr(adapter, "_display_name_from_cached_path", display_name)
    task = asyncio.create_task(adapter._maybe_extract_text_document(str(source), "text/plain"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), 5)
    assert display_calls == []
    assert await adapter._maybe_extract_text_document(str(source), "text/plain") == "[Content of notes.txt]:\ncontent"
    assert display_calls == [str(source)]
