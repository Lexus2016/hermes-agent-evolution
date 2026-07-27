import asyncio
import os
import stat
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp_tool import (
    MCPServerTask,
    _format_connect_error,
    _resolve_stdio_command,
    _MCP_AVAILABLE,
)

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
# Pre-flight command resolver tests (#1297)
# ---------------------------------------------------------------------------


class TestEnsureStdioCommandResolvable:
    """Cover the pre-flight guard added for #1297.

    ``_ensure_stdio_command_resolvable`` runs in ``_run_stdio`` right after
    ``_resolve_stdio_command`` and raises a NON-retryable
    :class:`MissingMcpCommandError` (subclass of
    :class:`NonMcpEndpointError`) when the configured command binary is
    absent, collapsing the 189-failure/scan retry storm for missing binaries
    like ``turbo-memory-mcp``.
    """

    def test_missing_bare_command_raises(self):
        """A bare name not on the SUBPROCESS PATH must raise."""
        from tools.mcp_tool import (
            MissingMcpCommandError,
            _ensure_stdio_command_resolvable,
        )

        env = {"PATH": "/nonexistent-dir-948"}
        with pytest.raises(MissingMcpCommandError, match="turbo-memory-mcp"):
            _ensure_stdio_command_resolvable("turbo-memory-mcp", env)

    def test_bad_absolute_path_raises(self, tmp_path):
        """An absolute path that doesn't exist must raise."""
        from tools.mcp_tool import (
            MissingMcpCommandError,
            _ensure_stdio_command_resolvable,
        )

        missing = tmp_path / "definitely-not-here-948"
        env = {"PATH": os.environ.get("PATH", "")}
        with pytest.raises(MissingMcpCommandError, match="does not exist"):
            _ensure_stdio_command_resolvable(str(missing), env)

    def test_non_executable_file_raises(self, tmp_path):
        """A regular (non-executable) file at an absolute path must raise."""
        from tools.mcp_tool import (
            MissingMcpCommandError,
            _ensure_stdio_command_resolvable,
        )

        non_exec = tmp_path / "not-executable-948"
        non_exec.write_text("#!/bin/sh\n")
        non_exec.chmod(0o644)
        assert not os.access(str(non_exec), os.X_OK)
        env = {"PATH": os.environ.get("PATH", "")}
        with pytest.raises(MissingMcpCommandError, match="not executable"):
            _ensure_stdio_command_resolvable(str(non_exec), env)

    def test_empty_command_raises(self):
        """An empty/whitespace command is a programming error -> ValueError."""
        from tools.mcp_tool import _ensure_stdio_command_resolvable

        with pytest.raises(ValueError):
            _ensure_stdio_command_resolvable("", {"PATH": "/usr/bin"})
        with pytest.raises(ValueError):
            _ensure_stdio_command_resolvable("   ", {"PATH": "/usr/bin"})

    def test_healthy_pass_through_no_exception(self):
        """A resolvable bare command (on PATH) must pass through cleanly."""
        from tools.mcp_tool import _ensure_stdio_command_resolvable

        env = {"PATH": os.environ.get("PATH", "")}
        # Should NOT raise.
        _ensure_stdio_command_resolvable("python3", env)

    def test_executable_absolute_path_passes(self, tmp_path):
        """An executable file at an absolute path must pass through cleanly."""
        from tools.mcp_tool import _ensure_stdio_command_resolvable

        exec_file = tmp_path / "my-mcp-server-948"
        exec_file.write_text("#!/bin/sh\n")
        exec_file.chmod(
            stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        )
        env = {"PATH": os.environ.get("PATH", "")}
        # Should NOT raise.
        _ensure_stdio_command_resolvable(str(exec_file), env)

    def test_missing_command_error_subclasses_non_mcp_endpoint_error(self):
        """MissingMcpCommandError must be caught as NonMcpEndpointError so the
        reconnect loop treats it as a clean, non-retryable exit."""
        from tools.mcp_tool import (
            MissingMcpCommandError,
            NonMcpEndpointError,
            _ensure_stdio_command_resolvable,
        )

        assert issubclass(MissingMcpCommandError, NonMcpEndpointError)
        env = {"PATH": "/nonexistent-dir-948"}
        with pytest.raises(NonMcpEndpointError):
            _ensure_stdio_command_resolvable("no-such-binary-948", env)
        with pytest.raises(ConnectionError):
            _ensure_stdio_command_resolvable("no-such-binary-948", env)
