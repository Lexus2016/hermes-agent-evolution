"""Tests for the Honcho dialectic circuit breaker."""

import time
from unittest.mock import MagicMock

import pytest

from plugins.memory.honcho.session import HonchoSession, HonchoSessionManager


class TestDialecticCircuitBreaker:
    """Circuit breaker prevents burning Honcho API credits during outages."""

    @staticmethod
    def _make_manager() -> HonchoSessionManager:
        cfg = MagicMock()
        cfg.write_frequency = "async"
        cfg.dialectic_reasoning_level = "low"
        cfg.dialectic_dynamic = True
        cfg.dialectic_max_chars = 600
        cfg.dialectic_max_input_chars = 10000
        cfg.user_observe_me = True
        cfg.user_observe_others = True
        cfg.ai_observe_me = True
        cfg.ai_observe_others = True
        cfg.message_max_chars = 25000
        mgr = HonchoSessionManager(config=cfg)
        # Fast thresholds for tests
        mgr._CIRCUIT_BREAKER_THRESHOLD = 3
        mgr._CIRCUIT_BREAKER_COOLDOWN_SECONDS = 10.0
        return mgr

    @staticmethod
    def _make_session(mgr: HonchoSessionManager, key: str = "test") -> HonchoSession:
        session = HonchoSession(
            key=key,
            user_peer_id="user-peer",
            assistant_peer_id="ai-peer",
            honcho_session_id="session-id",
        )
        mgr._cache[key] = session
        return session

    def test_available_by_default(self):
        mgr = self._make_manager()
        assert mgr.dialectic_query_available() is True

    def test_failure_increments_counter(self):
        mgr = self._make_manager()
        self._make_session(mgr)
        mgr._get_or_create_peer = MagicMock(
            side_effect=Exception("Honcho backend unreachable")
        )
        for _ in range(2):
            assert mgr.dialectic_query("test", "hello") == ""
        assert mgr._consecutive_dialectic_failures == 2
        assert mgr.dialectic_query_available() is True

    def test_breaker_trips_after_threshold(self):
        mgr = self._make_manager()
        self._make_session(mgr)
        mgr._get_or_create_peer = MagicMock(
            side_effect=Exception("Honcho backend unreachable")
        )
        for _ in range(3):
            mgr.dialectic_query("test", "hello")
        assert mgr._consecutive_dialectic_failures == 3
        assert mgr._dialectic_tripped_at is not None
        assert mgr.dialectic_query_available() is False

    def test_breaker_blocks_calls_while_open(self):
        mgr = self._make_manager()
        self._make_session(mgr)
        mgr._consecutive_dialectic_failures = 3
        mgr._dialectic_tripped_at = time.monotonic()
        peer_mock = MagicMock()
        peer_mock.chat.return_value = "should not run"
        mgr._get_or_create_peer = MagicMock(return_value=peer_mock)

        result = mgr.dialectic_query("test", "hello")
        assert result == ""
        peer_mock.chat.assert_not_called()

    def test_success_after_failure_resets_window(self):
        mgr = self._make_manager()
        self._make_session(mgr)
        peer_mock = MagicMock()
        peer_mock.chat.return_value = "healthy result"
        mgr._get_or_create_peer = MagicMock(return_value=peer_mock)

        # Simulate prior failure state
        mgr._consecutive_dialectic_failures = 2
        mgr.dialectic_query("test", "hello")
        assert mgr._consecutive_dialectic_failures == 0
        assert mgr._dialectic_tripped_at is None

    def test_half_open_probe_resets_on_success(self):
        mgr = self._make_manager()
        self._make_session(mgr)
        peer_mock = MagicMock()
        peer_mock.chat.return_value = "probe succeeded"
        mgr._get_or_create_peer = MagicMock(return_value=peer_mock)

        mgr._consecutive_dialectic_failures = 3
        mgr._dialectic_tripped_at = time.monotonic() - 15.0
        assert mgr.dialectic_query_available() is True  # half-open

        result = mgr.dialectic_query("test", "hello")
        assert result == "probe succeeded"
        assert mgr._consecutive_dialectic_failures == 0
        assert mgr._dialectic_tripped_at is None

    def test_half_open_probe_re_trips_on_failure(self):
        mgr = self._make_manager()
        self._make_session(mgr)
        mgr._get_or_create_peer = MagicMock(side_effect=Exception("still down"))

        mgr._consecutive_dialectic_failures = 3
        old_tripped = time.monotonic() - 15.0
        mgr._dialectic_tripped_at = old_tripped
        assert mgr.dialectic_query_available() is True  # half-open

        mgr.dialectic_query("test", "hello")
        # A new failure should keep the breaker open and refresh the trip timestamp.
        assert mgr._consecutive_dialectic_failures >= 3
        assert mgr._dialectic_tripped_at is not None
        assert mgr._dialectic_tripped_at >= old_tripped
        assert mgr.dialectic_query_available() is False

    def test_empty_result_does_not_increment_failure(self):
        mgr = self._make_manager()
        self._make_session(mgr)
        peer_mock = MagicMock()
        peer_mock.chat.return_value = ""
        mgr._get_or_create_peer = MagicMock(return_value=peer_mock)

        for _ in range(5):
            mgr.dialectic_query("test", "hello")
        assert mgr._consecutive_dialectic_failures == 0
        assert mgr.dialectic_query_available() is True
