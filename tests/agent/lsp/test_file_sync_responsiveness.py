"""Document I/O must yield without reordering or publishing abandoned file snapshots."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.lsp.client import LSPClient, file_uri
from agent.lsp.protocol import LSPProtocolError


def _live_client(tmp_path, monkeypatch):
    client = LSPClient(server_id="file-sync", workspace_root=str(tmp_path), command=[])
    client._state = "running"
    client._proc = SimpleNamespace(returncode=None, stdin=SimpleNamespace(is_closing=lambda: False))
    client._reader_task = SimpleNamespace(done=lambda: False)
    sent = []

    async def record(method, params):
        sent.append((method, params))

    monkeypatch.setattr(client, "_send_notification", record)
    return client, sent


async def _loop_checkpoint():
    ready = asyncio.Event()
    asyncio.get_running_loop().call_soon(ready.set)
    await ready.wait()


def _pause_first_read(source, monkeypatch):
    """Control read completion separately from the real-thread responsiveness contract."""
    started, release = asyncio.Event(), asyncio.Event()
    original_to_thread = asyncio.to_thread

    async def offload(func, *args, **kwargs):
        if getattr(func, "__self__", None) == source and func.__name__ == "read_text":
            value = func(*args, **kwargs)
            if not started.is_set():
                started.set()
                await release.wait()
            return value
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", offload)
    return started, release


@pytest.mark.asyncio
async def test_blocked_file_read_keeps_loop_responsive(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_bytes(b"\xef\xbb\xbfprint('ready')\n")
    client, sent = _live_client(tmp_path, monkeypatch)
    loop = asyncio.get_running_loop()
    started, release, responsive = threading.Event(), threading.Event(), threading.Event()
    observed = []
    original_read = Path.read_text

    def blocked_read(path, *args, **kwargs):
        value = original_read(path, *args, **kwargs)
        if path == source:
            started.set()
            if not release.wait(10):
                raise TimeoutError("test observer did not release file read")
        return value

    def observe_loop():
        try:
            if started.wait(5):
                loop.call_soon_threadsafe(responsive.set)
                observed.append(responsive.wait(5))
        finally:
            release.set()

    monkeypatch.setattr(Path, "read_text", blocked_read)
    observer = threading.Thread(target=observe_loop, daemon=True)
    observer.start()
    try:
        assert await client.open_file(str(source), language_id="python") == 0
    finally:
        release.set()
        await asyncio.to_thread(observer.join, 5)
    assert observed == [True], "file I/O prevented the event loop from handling a callback"
    assert sent[-1] == ("textDocument/didOpen", {"textDocument": {
        "uri": file_uri(str(source)), "languageId": "python", "version": 0, "text": "print('ready')\n",
    }})


@pytest.mark.asyncio
async def test_concurrent_opens_preserve_snapshot_and_version_order(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("old\n", encoding="utf-8")
    client, sent = _live_client(tmp_path, monkeypatch)
    started, release = _pause_first_read(source, monkeypatch)
    first = asyncio.create_task(client.open_file(str(source)))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        source.write_text("new\n", encoding="utf-8")
        second = asyncio.create_task(client.open_file(str(source)))
        await _loop_checkpoint()
        release.set()
        assert await first == 0
        assert await second == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
    documents = [(method, params) for method, params in sent if method.startswith("textDocument/")]
    assert documents[0][0] == "textDocument/didOpen"
    assert documents[0][1]["textDocument"]["text"] == "old\n"
    assert documents[1] == ("textDocument/didChange", {
        "textDocument": {"uri": file_uri(str(source)), "version": 1}, "contentChanges": [{"text": "new\n"}],
    })


@pytest.mark.asyncio
async def test_cancelled_read_never_publishes_late_result(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("abandoned\n", encoding="utf-8")
    client, sent = _live_client(tmp_path, monkeypatch)
    loop = asyncio.get_running_loop()
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    original_read = Path.read_text

    def blocked_read(path, *args, **kwargs):
        value = original_read(path, *args, **kwargs)
        if path == source:
            loop.call_soon_threadsafe(started.set)
            try:
                if not release.wait(10):
                    raise TimeoutError("test did not release cancelled file read")
            finally:
                loop.call_soon_threadsafe(finished.set)
        return value

    monkeypatch.setattr(Path, "read_text", blocked_read)
    task = asyncio.create_task(client.open_file(str(source)))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not sent
        assert str(source) not in client._docs
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), 5)
    assert not sent
    assert await client.open_file(str(source)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["stopping", "stopped", "restarted"])
async def test_read_cannot_publish_after_process_transition(tmp_path, monkeypatch, transition):
    source = tmp_path / "source.py"
    source.write_text("old process snapshot\n", encoding="utf-8")
    client, sent = _live_client(tmp_path, monkeypatch)
    started, release = _pause_first_read(source, monkeypatch)
    task = asyncio.create_task(client.open_file(str(source)))
    try:
        await asyncio.wait_for(started.wait(), 5)
        if transition == "stopping":
            client._stopping = True
        elif transition == "stopped":
            client._state = "stopped"
        else:
            client._proc = SimpleNamespace(returncode=None, stdin=SimpleNamespace(is_closing=lambda: False))
        release.set()
        with pytest.raises(LSPProtocolError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not sent
    assert str(source) not in client._docs


@pytest.mark.asyncio
async def test_save_and_eviction_wait_for_pending_open(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.lsp.client.MAX_TRACKED_FILES", 1)
    source, other = tmp_path / "source.py", tmp_path / "other.py"
    source.write_text("first\n", encoding="utf-8")
    other.write_text("second\n", encoding="utf-8")
    client, sent = _live_client(tmp_path, monkeypatch)
    started, release = _pause_first_read(source, monkeypatch)
    first = asyncio.create_task(client.open_file(str(source)))
    pending = [first]
    try:
        await asyncio.wait_for(started.wait(), 5)
        pending.extend([asyncio.create_task(client.save_file(str(source))),
                        asyncio.create_task(client.open_file(str(other)))])
        await _loop_checkpoint()
        assert not sent
        release.set()
        await asyncio.gather(*pending)
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
    documents = [(method, params["textDocument"]["uri"]) for method, params in sent
                 if method.startswith("textDocument/")]
    assert documents == [("textDocument/didOpen", file_uri(str(source))),
                         ("textDocument/didSave", file_uri(str(source))),
                         ("textDocument/didOpen", file_uri(str(other))),
                         ("textDocument/didClose", file_uri(str(source)))]
    assert str(source) not in client._docs
    assert client._docs[str(other)].text == "second\n"


@pytest.mark.asyncio
async def test_process_replacement_during_watched_file_send_drops_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("old process snapshot\n", encoding="utf-8")
    client, sent = _live_client(tmp_path, monkeypatch)

    async def replace_process(method, params):
        sent.append((method, params))
        client._proc = SimpleNamespace(returncode=None, stdin=SimpleNamespace(is_closing=lambda: False))

    monkeypatch.setattr(client, "_send_notification", replace_process)
    with pytest.raises(LSPProtocolError):
        await client.open_file(str(source))
    assert [method for method, _ in sent] == ["workspace/didChangeWatchedFiles"]
    assert str(source) not in client._docs
