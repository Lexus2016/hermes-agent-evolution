"""A failed watcher launch must remain retryable at shutdown's fallback seam."""

import asyncio
import errno
from pathlib import Path
import subprocess
import time
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", [None, "spawn", "environment"])
async def test_shutdown_retries_only_an_unspawned_restart_watcher(
    first_failure, isolated_home, monkeypatch,
):
    from gateway import run as gateway_run
    from gateway.run_shutdown import GatewayShutdownMixin

    runner = GatewayShutdownMixin()
    runner.adapters = {}
    runner._restart_task_started = False
    runner._detached_restart_helper_started = False
    runner._restart_drain_timeout = 0
    runner._await_active_work_before_restart = AsyncMock(return_value=True)
    runner._finalize_shutdown_agents = AsyncMock()
    context = runner._StopContext(deferred_count=lambda: 0, started_at=time.monotonic())

    async def finalize_stop(**_kwargs):
        # Exercise the real stop phase containing the second launch opportunity.
        # Do not execute the unrelated process-kill or real adapter teardown phases.
        await runner._stop_finalize_agents_and_adapters(context)

    runner.stop = AsyncMock(side_effect=finalize_stop)
    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["synthetic-hermes"])
    watcher_environment = {"HOME": str(isolated_home.parent), "HERMES_HOME": str(isolated_home)}
    environment = Mock(return_value=watcher_environment)
    spawn = Mock(return_value=object())
    if first_failure == "spawn":
        spawn.side_effect = [OSError(errno.EAGAIN, "synthetic launch failure"), object()]
    elif first_failure == "environment":
        environment.side_effect = [OSError(errno.EIO, "synthetic environment failure"), watcher_environment]
    monkeypatch.setattr(GatewayShutdownMixin, "_restart_watcher_env", staticmethod(environment))
    monkeypatch.setattr(subprocess, "Popen", spawn)

    assert runner.request_restart(detached=True, via_service=False) is True
    await runner._restart_task

    runner.stop.assert_awaited_once_with(restart=True, detached_restart=True, service_restart=False)
    runner._finalize_shutdown_agents.assert_awaited_once_with({})
    assert environment.call_count == (1 if first_failure is None else 2)
    assert spawn.call_count == (2 if first_failure == "spawn" else 1)
    assert runner._detached_restart_helper_started is True
    assert spawn.call_args.kwargs["env"] == watcher_environment
    assert spawn.call_args.kwargs["start_new_session"] is True

    # A confirmed successful launch must still suppress every later opportunity.
    calls_after_success = spawn.call_count
    await runner._launch_detached_restart_command()
    assert spawn.call_count == calls_after_success


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_concurrent_and_reentrant_launches_do_not_duplicate_success(
    fail_first, isolated_home, monkeypatch,
):
    from gateway import run as gateway_run
    from gateway.run_shutdown import GatewayShutdownMixin

    runner = GatewayShutdownMixin()
    runner._detached_restart_helper_started = False
    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["synthetic-hermes"])
    monkeypatch.setattr(GatewayShutdownMixin, "_restart_watcher_env", staticmethod(dict))
    attempts = []

    def reentrant_spawn(*args, **kwargs):
        attempts.append((args, kwargs))
        nested = runner._launch_detached_restart_command()
        try:
            # A synchronous callback re-enters while the first spawn is unsettled.
            with pytest.raises(StopIteration):
                nested.send(None)
        finally:
            nested.close()
        if fail_first and len(attempts) == 1:
            raise OSError(errno.EAGAIN, "synthetic first launch failure")
        return object()

    monkeypatch.setattr(subprocess, "Popen", reentrant_spawn)
    results = await asyncio.gather(
        runner._launch_detached_restart_command(),
        runner._launch_detached_restart_command(),
        runner._launch_detached_restart_command(),
        return_exceptions=True,
    )

    assert len(attempts) == (2 if fail_first else 1)
    assert sum(isinstance(result, OSError) for result in results) == int(fail_first)
    assert runner._detached_restart_helper_started is True
    assert runner._detached_restart_helper_starting is False


@pytest.mark.parametrize("failed_spawns", [0, 1, 2])
def test_windows_watcher_reports_synthetic_spawn_outcome(
    failed_spawns, isolated_home, monkeypatch, caplog,
):
    """Check helper return/dispatch data with fake process APIs, not Windows OS behavior."""
    from gateway.run_shutdown import GatewayShutdownMixin
    from hermes_cli import _launchers, _subprocess_compat

    environment = {"HERMES_HOME": str(isolated_home)}
    monkeypatch.setattr(GatewayShutdownMixin, "_restart_watcher_env", staticmethod(lambda: environment))
    command = Mock(return_value=["synthetic-python", "synthetic-watcher"])
    monkeypatch.setattr(_launchers, "runtime_command", command)
    monkeypatch.setattr(_subprocess_compat, "windows_detach_popen_kwargs", lambda: {"creationflags": 11})
    monkeypatch.setattr(_subprocess_compat, "windows_detach_flags_without_breakaway", lambda: 22)
    outcomes = [OSError(errno.EACCES, "private-synthetic-error-detail") for _ in range(failed_spawns)]
    outcomes.append(object())
    spawn = Mock(side_effect=outcomes)
    monkeypatch.setattr(subprocess, "Popen", spawn)

    launched = GatewayShutdownMixin._spawn_windows_restart_watcher(["hermes path", "--flag"], 123, 5.0)

    assert launched is (failed_spawns < 2)
    assert spawn.call_count == min(failed_spawns + 1, 2)
    assert command.call_args.args[1] == ["123", "5.0", "hermes path", "--flag", "gateway", "restart"]
    assert spawn.call_args_list[0].kwargs["creationflags"] == 11
    if failed_spawns:
        assert spawn.call_args_list[1].kwargs["creationflags"] == 22
    assert "private-synthetic-error-detail" not in caplog.text
    assert ("was not started" in caplog.text) is (failed_spawns == 2)


@pytest.mark.platforms("windows")
@pytest.mark.asyncio
async def test_windows_failed_helper_does_not_latch_started(isolated_home, monkeypatch):
    """Native-host dispatch test; watcher creation stays synthetic."""
    from gateway import run as gateway_run
    from gateway.run_shutdown import GatewayShutdownMixin

    runner = GatewayShutdownMixin()
    runner._detached_restart_helper_started = False
    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["synthetic-hermes"])
    helper = Mock(side_effect=[False, True])
    monkeypatch.setattr(GatewayShutdownMixin, "_spawn_windows_restart_watcher", staticmethod(helper))

    await runner._launch_detached_restart_command()
    assert runner._detached_restart_helper_started is False
    await runner._launch_detached_restart_command()
    assert runner._detached_restart_helper_started is True
    await runner._launch_detached_restart_command()
    assert helper.call_count == 2
