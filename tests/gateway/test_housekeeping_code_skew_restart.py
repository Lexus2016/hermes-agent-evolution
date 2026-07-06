"""Deploy-gap self-heal: the gateway housekeeping loop detects a checkout that
moved under the long-lived process (git pull / hermes update / evolution deploy)
and, on a service-managed host, requests a graceful restart so the new code
actually goes live instead of the process running stale ``sys.modules`` forever.

See ``gateway.run._start_gateway_housekeeping`` and ``gateway.code_skew``.
"""
import threading
from unittest.mock import MagicMock, patch

import gateway.run as run


def _make_runner(running_agents=None):
    runner = MagicMock()
    # Empty dict = no interactive turn in flight (the default happy path).
    runner._running_agents = {} if running_agents is None else running_agents
    return runner


def _drive(runner, skew_seq, systemd_ok=True, call_soon=None):
    """Run the housekeeping loop until it stops.

    ``skew_seq`` is a list of values ``detect_code_skew`` returns on each call;
    the loop is stopped once the list is exhausted (or a restart is scheduled).
    ``call_soon`` overrides the fake loop's ``call_soon_threadsafe``.
    """
    stop = threading.Event()
    seq = list(skew_seq)

    def fake_skew():
        if seq:
            val = seq.pop(0)
        else:
            val = None
        if not seq:
            # Last scripted skew value — let the loop end after this tick.
            stop.set()
        return val

    class FakeLoop:
        def call_soon_threadsafe(self, fn, *args):
            if call_soon is not None:
                call_soon(fn, *args)
            else:
                fn(*args)
            stop.set()

    with patch("gateway.code_skew.detect_code_skew", side_effect=fake_skew), patch(
        "hermes_cli.gateway.supports_systemd_services", return_value=systemd_ok
    ):
        run._start_gateway_housekeeping(
            stop, adapters=None, loop=FakeLoop(), interval=0, runner=runner
        )


def test_code_skew_on_service_host_requests_graceful_restart():
    runner = _make_runner()
    _drive(runner, [("aaa1111111", "bbb2222222")], systemd_ok=True)
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


def test_code_skew_without_systemd_does_not_restart():
    """A non-service-managed host (e.g. a foreground dev run) must not
    auto-restart — it only logs; the user restarts manually."""
    runner = _make_runner()
    _drive(runner, [("aaa1111111", "bbb2222222")], systemd_ok=False)
    runner.request_restart.assert_not_called()


def test_no_code_skew_does_not_restart():
    runner = _make_runner()
    _drive(runner, [None], systemd_ok=True)
    runner.request_restart.assert_not_called()


def test_restart_deferred_while_interactive_turn_in_flight():
    """When an interactive agent turn is mid-flight, defer the restart (do not
    race its drain) — request_restart is NOT called this tick."""
    runner = _make_runner(running_agents={"session-1": object()})
    _drive(runner, [("aaa1111111", "bbb2222222")], systemd_ok=True)
    runner.request_restart.assert_not_called()


def test_scheduling_failure_does_not_latch_and_retries():
    """Regression (panel review): the latch must be set ONLY after the restart
    is successfully scheduled. If call_soon_threadsafe raises on the first skew
    check, the loop must retry on the next check and eventually restart —
    a premature latch would silently give up forever."""
    runner = _make_runner()
    attempts = {"n": 0}

    def flaky_call_soon(fn, *args):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("event loop is closing")
        fn(*args)  # second attempt succeeds

    # Two skew detections: first schedule raises, second succeeds.
    _drive(
        runner,
        [("aaa1111111", "bbb2222222"), ("aaa1111111", "bbb2222222")],
        systemd_ok=True,
        call_soon=flaky_call_soon,
    )
    assert attempts["n"] == 2
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


def test_no_runner_is_a_noop():
    """Back-compat: called without a runner, the skew check never fires — the
    detector is not even reached (the guard short-circuits first)."""
    stop = threading.Event()

    class OneShotStop:
        """Deterministic stop: reports set() after a couple of iterations so the
        loop terminates without spinning or timers."""

        def __init__(self):
            self._n = 0

        def is_set(self):
            self._n += 1
            return self._n > 3

        def wait(self, timeout=None):
            return self.is_set()

        def set(self):
            self._n = 999

    with patch("gateway.code_skew.detect_code_skew") as det:
        run._start_gateway_housekeeping(
            OneShotStop(), adapters=None, loop=MagicMock(), interval=0, runner=None
        )
    det.assert_not_called()
