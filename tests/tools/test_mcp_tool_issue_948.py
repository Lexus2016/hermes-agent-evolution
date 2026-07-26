import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from tools.mcp_tool import MCPServerTask, _format_connect_error, _resolve_stdio_command, _MCP_AVAILABLE

# Ensure the mcp module symbols exist for patching even when the SDK isn't installed
if not _MCP_AVAILABLE:
    import tools.mcp_tool as _mcp_mod
    if not hasattr(_mcp_mod, "StdioServerParameters"):
        _mcp_mod.StdioServerParameters = MagicMock
    if not hasattr(_mcp_mod, "stdio_client"):
        _mcp_mod.stdio_client = MagicMock
    if not hasattr(_mcp_mod, "ClientSession"):
        _mcp_mod.ClientSession = MagicMock


def test_resolve_stdio_command_falls_back_to_hermes_node_bin(tmp_path):
    node_bin = tmp_path / "node" / "bin"
    node_bin.mkdir(parents=True)
    npx_path = node_bin / "npx"
    npx_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    npx_path.chmod(0o755)

    with patch("tools.mcp_tool.shutil.which", return_value=None), \
         patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}, clear=False):
        command, env = _resolve_stdio_command("npx", {"PATH": "/usr/bin"})

    assert command == str(npx_path)
    assert env["PATH"].split(os.pathsep)[0] == str(node_bin)


def test_resolve_stdio_command_falls_back_to_usr_local_bin():
    """When ``npx`` isn't on the filtered PATH and isn't under ``$HERMES_HOME/node/bin``
    or ``~/.local/bin``, the resolver should still locate it at ``/usr/local/bin/npx``.

    This is the canonical install location for Node on Linux from-source builds,
    the upstream ``node:bookworm-slim`` image (which the Hermes Docker image
    copies ``node + npm + corepack`` from since #4977), and macOS Homebrew on
    Intel. Without this candidate, MCP servers run with an ``env.PATH`` that
    omits ``/usr/local/bin`` (common when users hand-author PATH for sandboxing)
    fail with ENOENT at ``execvp``.
    """
    target = os.path.join(os.sep, "usr", "local", "bin", "npx")

    # Pretend ONLY the /usr/local/bin/npx candidate exists and is executable —
    # the other candidates ($HERMES_HOME/node/bin/npx and ~/.local/bin/npx)
    # should fail isfile() and the resolver must fall through to /usr/local/bin.
    def _fake_isfile(path):
        return path == target

    def _fake_access(path, _mode):
        return path == target

    with patch("tools.mcp_tool.shutil.which", return_value=None), \
         patch("tools.mcp_tool.os.path.isfile", side_effect=_fake_isfile), \
         patch("tools.mcp_tool.os.access", side_effect=_fake_access):
        command, env = _resolve_stdio_command("npx", {"PATH": "/opt/data/bin:/usr/bin:/bin"})

    assert command == target
    # /usr/local/bin must be prepended so npx's shebang (`/usr/bin/env node`)
    # can find node in the same directory.
    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(target)


def test_resolve_stdio_command_respects_explicit_empty_path():
    seen_paths = []

    def _fake_which(_cmd, path=None):
        seen_paths.append(path)
        return None

    with patch("tools.mcp_tool.shutil.which", side_effect=_fake_which):
        command, env = _resolve_stdio_command("python", {"PATH": ""})

    assert command == "python"
    assert env["PATH"] == ""
    assert seen_paths == [""]


def test_format_connect_error_unwraps_exception_group():
    error = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [FileNotFoundError(2, "No such file or directory", "node")],
    )

    message = _format_connect_error(error)

    assert "missing executable 'node'" in message


def test_run_stdio_uses_resolved_command_and_prepended_path(tmp_path):
    node_bin = tmp_path / "node" / "bin"
    node_bin.mkdir(parents=True)
    npx_path = node_bin / "npx"
    npx_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    npx_path.chmod(0o755)

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))

    mock_stdio_cm = MagicMock()
    mock_stdio_cm.__aenter__ = AsyncMock(return_value=(object(), object()))
    mock_stdio_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    async def _test():
        with patch("tools.mcp_tool.shutil.which", return_value=None), \
             patch.dict("os.environ", {"HERMES_HOME": str(tmp_path), "PATH": "/usr/bin", "HOME": str(tmp_path)}, clear=False), \
             patch("tools.mcp_tool.StdioServerParameters") as mock_params, \
             patch("tools.mcp_tool.stdio_client", return_value=mock_stdio_cm), \
             patch("tools.mcp_tool.ClientSession", return_value=mock_session_cm):
            server = MCPServerTask("srv")
            await server.start({"command": "npx", "args": ["-y", "pkg"], "env": {"PATH": "/usr/bin"}})

            # The real (resolved) command no longer reaches StdioServerParameters
            # directly -- it's now wrapped in the parent-death watchdog
            # supervisor (tools/mcp_stdio_watchdog.py) so an ungraceful exit of
            # this process can't orphan it. Assert the resolved npx path and
            # its args still flow through correctly as the watchdog's target
            # command, preserving this test's original path-resolution intent.
            call_kwargs = mock_params.call_args.kwargs
            assert call_kwargs["command"] == sys.executable
            assert call_kwargs["args"][0].endswith("mcp_stdio_watchdog.py")
            assert "--" in call_kwargs["args"]
            sep = call_kwargs["args"].index("--")
            assert call_kwargs["args"][sep + 1:] == [str(npx_path), "-y", "pkg"]
            assert call_kwargs["env"]["PATH"].split(os.pathsep)[0] == str(node_bin)

            await server.shutdown()

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# #29184: OSV malware preflight must not block the asyncio event loop, and a
# stalled check must time out fail-open rather than freezing MCP startup.
# ---------------------------------------------------------------------------


def _stdio_mocks():
    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))
    mock_stdio_cm = MagicMock()
    mock_stdio_cm.__aenter__ = AsyncMock(return_value=(object(), object()))
    mock_stdio_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_stdio_cm, mock_session_cm


def test_run_stdio_malware_check_does_not_block_event_loop():
    """The blocking OSV check runs off the loop (asyncio.to_thread), so a
    concurrent coroutine keeps making progress while it runs."""
    import time
    mock_stdio_cm, mock_session_cm = _stdio_mocks()

    def slow_check(_command, _args):
        time.sleep(0.3)  # simulate a slow OSV HTTPS call
        return None

    ticks = {"n": 0}

    async def _ticker():
        # If the loop were blocked, these ticks would not advance during the
        # 0.3s check.
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks["n"] += 1

    async def _test():
        with patch("tools.osv_check.check_package_for_malware", side_effect=slow_check), \
             patch("tools.mcp_tool.StdioServerParameters"), \
             patch("tools.mcp_tool.stdio_client", return_value=mock_stdio_cm), \
             patch("tools.mcp_tool.ClientSession", return_value=mock_session_cm):
            server = MCPServerTask("srv")
            ticker = asyncio.create_task(_ticker())
            await server.start({"command": "npx", "args": ["-y", "pkg"]})
            ticks_during = ticks["n"]
            await ticker
            await server.shutdown()
        # The loop kept ticking DURING the 0.3s blocking check -> not blocked.
        assert ticks_during >= 3, f"event loop appeared blocked (ticks={ticks_during})"

    asyncio.run(_test())


def test_run_stdio_malware_check_times_out_fail_open():
    """A check that hangs past the timeout must NOT freeze startup: it times
    out, logs, and proceeds (fail-open) so the server still starts."""
    import time
    mock_stdio_cm, mock_session_cm = _stdio_mocks()

    def hung_check(_command, _args):
        time.sleep(0.5)  # outlasts the 0.2s timeout 2.5x; short enough not to stall teardown
        return "MALWARE"  # would block startup if awaited to completion

    async def _test():
        with patch("tools.osv_check.check_package_for_malware", side_effect=hung_check), \
             patch("tools.mcp_tool._OSV_MALWARE_CHECK_TIMEOUT_S", 0.2), \
             patch("tools.mcp_tool.StdioServerParameters"), \
             patch("tools.mcp_tool.stdio_client", return_value=mock_stdio_cm), \
             patch("tools.mcp_tool.ClientSession", return_value=mock_session_cm):
            server = MCPServerTask("srv")
            start = time.monotonic()
            await server.start({"command": "npx", "args": ["-y", "pkg"]})
            elapsed = time.monotonic() - start
            await server.shutdown()
        # Returned shortly after the 0.2s timeout (fail-open), not the 0.5s hang.
        assert elapsed < 1.0, f"startup did not fail-open promptly ({elapsed:.1f}s)"

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# #1297: pre-flight guard — a missing stdio command must fail ONCE with a
# non-retryable error, not bubble FileNotFoundError through the reconnect
# retry loop (3 retries × N revival cycles = the observed 189-failure/scan
# storm). The guard is exercised directly here; the integration (it stops the
# retry loop because MissingMcpCommandError subclasses NonMcpEndpointError,
# which the reconnect loop catches-and-exits on) is covered by inspection of
# the loop's `except NonMcpEndpointError` branch.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from tools.mcp_tool import (  # noqa: E402
    MissingMcpCommandError,
    _ensure_stdio_command_resolvable,
)


def test_preflight_raises_for_missing_bare_command():
    """A bare command name not on PATH → MissingMcpCommandError (non-retryable),
    naming the command and giving an install hint. This is the turbo-memory-mcp
    case from #1297."""
    with patch("tools.mcp_tool.shutil.which", return_value=None):
        with pytest.raises(MissingMcpCommandError) as exc_info:
            _ensure_stdio_command_resolvable(
                "tqmemory", "turbo-memory-mcp", {"PATH": "/usr/bin"}
            )
    msg = str(exc_info.value)
    assert "turbo-memory-mcp" in msg
    assert "not found on PATH" in msg
    # Actionable install hint must be present.
    assert "install" in msg.lower()


def test_preflight_raises_for_nonexistent_absolute_path():
    """An absolute path that does not exist → MissingMcpCommandError with a
    'path does not exist' message (distinct from the PATH case)."""
    with patch("tools.mcp_tool.os.path.isfile", return_value=False):
        with pytest.raises(MissingMcpCommandError) as exc_info:
            _ensure_stdio_command_resolvable(
                "srv", "/opt/does-not-exist/bin", {"PATH": "/usr/bin"}
            )
    assert "does not exist" in str(exc_info.value)


def test_preflight_raises_for_non_executable_path(tmp_path):
    """An existing-but-not-executable file → MissingMcpCommandError (chmod
    hint). Prevents a confusing PermissionError retry storm."""
    f = tmp_path / "server"
    f.write_text("#!/bin/sh\n", encoding="utf-8")
    f.chmod(0o644)  # not executable
    with patch("tools.mcp_tool.os.access", return_value=False):
        with pytest.raises(MissingMcpCommandError) as exc_info:
            _ensure_stdio_command_resolvable("srv", str(f), {"PATH": "/usr/bin"})
    assert "not executable" in str(exc_info.value)


def test_preflight_raises_for_empty_command():
    with pytest.raises(MissingMcpCommandError) as exc_info:
        _ensure_stdio_command_resolvable("srv", "", {"PATH": "/usr/bin"})
    assert "empty" in str(exc_info.value).lower()


def test_preflight_passes_for_bare_command_on_path():
    """A bare command found via shutil.which → no exception (server proceeds
    to spawn). The common healthy case must not be blocked."""
    with patch(
        "tools.mcp_tool.shutil.which",
        return_value="/usr/local/bin/turbo-memory-mcp",
    ):
        # Should not raise.
        _ensure_stdio_command_resolvable(
            "tqmemory", "turbo-memory-mcp", {"PATH": "/usr/local/bin"}
        )


def test_preflight_passes_for_existing_executable_absolute_path(tmp_path):
    """An absolute path pointing at a real executable file → no exception."""
    f = tmp_path / "server"
    f.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    f.chmod(0o755)
    _ensure_stdio_command_resolvable("srv", str(f), {"PATH": "/usr/bin"})


def test_preflight_missing_command_is_non_retryable_subclass():
    """MissingMcpCommandError MUST subclass NonMcpEndpointError so the
    reconnect loop's `except NonMcpEndpointError` branch exits cleanly instead
    of retrying. This is the invariant that converts the retry storm into one
    clean failure — if it ever breaks, #1297 regresses."""
    from tools.mcp_tool import NonMcpEndpointError

    assert issubclass(MissingMcpCommandError, NonMcpEndpointError)
    # And NonMcpEndpointError is itself a ConnectionError (the reconnect loop's
    # broad-catch semantics) — verify the chain end-to-end.
    assert issubclass(MissingMcpCommandError, ConnectionError)


def test_preflight_uses_subprocess_env_path_not_process_path():
    """The pre-flight must resolve against the SUBPROCESS env's PATH (what the
    child will actually see), not the Hermes process's PATH. A binary present
    in the subprocess PATH but not the process PATH must pass; a binary absent
    from the subprocess PATH must fail even if the process PATH has it."""
    # Subprocess PATH includes /opt/special where the binary lives; the fake
    # which() returns a hit only for that path → should pass.
    def _which(cmd, path=None):
        if path and "/opt/special" in path:
            return "/opt/special/" + cmd
        return None

    with patch("tools.mcp_tool.shutil.which", side_effect=_which):
        # Passes with the subprocess PATH that includes /opt/special.
        _ensure_stdio_command_resolvable(
            "srv", "special-bin", {"PATH": "/opt/special:/usr/bin"}
        )
        # Fails when the subprocess PATH omits /opt/special, even though the
        # process PATH (not consulted) might have it.
        with pytest.raises(MissingMcpCommandError):
            _ensure_stdio_command_resolvable(
                "srv", "special-bin", {"PATH": "/usr/bin"}
            )
