"""Tests for the Home Assistant gateway adapter.

Tests real logic: state change formatting, event filtering pipeline,
cooldown behavior, config integration, and adapter initialization.
"""

import errno
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import (
    Platform,
    PlatformConfig,
)
from plugins.platforms.homeassistant.adapter import (
    FlapSuppressor,
    HomeAssistantAdapter,
    _parse_window_seconds,
    check_ha_requirements,
    validate_ha_config,
)


# ---------------------------------------------------------------------------
# check_ha_requirements
# ---------------------------------------------------------------------------


class TestCheckRequirements:


    @patch("plugins.platforms.homeassistant.adapter.AIOHTTP_AVAILABLE", False)
    def test_returns_false_without_aiohttp(self, monkeypatch):
        monkeypatch.setenv("HASS_TOKEN", "test-token")
        assert check_ha_requirements() is False

    def test_validate_config_accepts_platform_token(self, monkeypatch):
        monkeypatch.delenv("HASS_TOKEN", raising=False)
        config = PlatformConfig(enabled=True, token="config-token")
        assert validate_ha_config(config) is True


class TestValidateConfig:
    def test_returns_false_without_token_in_config_or_env(self, monkeypatch):
        monkeypatch.delenv("HASS_TOKEN", raising=False)
        assert validate_ha_config(PlatformConfig(enabled=True)) is False


# ---------------------------------------------------------------------------
# _format_state_change - pure function, all domain branches
# ---------------------------------------------------------------------------


class TestFormatStateChange:
    @staticmethod
    def fmt(entity_id, old_state, new_state):
        return HomeAssistantAdapter._format_state_change(entity_id, old_state, new_state)

    def test_climate_includes_temperatures(self):
        msg = self.fmt(
            "climate.thermostat",
            {"state": "off"},
            {"state": "heat", "attributes": {
                "friendly_name": "Main Thermostat",
                "current_temperature": 21.5,
                "temperature": 23,
            }},
        )
        assert "Main Thermostat" in msg
        assert "'off'" in msg and "'heat'" in msg
        assert "21.5" in msg and "23" in msg

    def test_sensor_includes_unit(self):
        msg = self.fmt(
            "sensor.temperature",
            {"state": "22.5"},
            {"state": "25.1", "attributes": {
                "friendly_name": "Living Room Temp",
                "unit_of_measurement": "C",
            }},
        )
        assert "22.5C" in msg and "25.1C" in msg
        assert "Living Room Temp" in msg







# ---------------------------------------------------------------------------
# Adapter initialization from config
# ---------------------------------------------------------------------------


class TestAdapterInit:
    def test_url_and_token_from_config_extra(self, monkeypatch):
        monkeypatch.delenv("HASS_URL", raising=False)
        monkeypatch.delenv("HASS_TOKEN", raising=False)

        config = PlatformConfig(
            enabled=True,
            token="config-token",
            extra={"url": "http://192.168.1.50:8123"},
        )
        adapter = HomeAssistantAdapter(config)
        assert adapter._hass_token == "config-token"
        assert adapter._hass_url == "http://192.168.1.50:8123"


    def test_watch_filters_parsed(self):
        config = PlatformConfig(
            enabled=True, token="***",
            extra={
                "watch_domains": ["climate", "binary_sensor"],
                "watch_entities": ["sensor.special"],
                "ignore_entities": ["sensor.uptime", "sensor.cpu"],
                "cooldown_seconds": 120,
            },
        )
        adapter = HomeAssistantAdapter(config)
        assert adapter._watch_domains == {"climate", "binary_sensor"}
        assert adapter._watch_entities == {"sensor.special"}
        assert adapter._ignore_entities == {"sensor.uptime", "sensor.cpu"}
        assert adapter._watch_all is False
        assert adapter._cooldown_seconds == 120


# ---------------------------------------------------------------------------
# Event filtering pipeline (_handle_ha_event)
#
# We mock handle_message (not our code, it's the base class pipeline) to
# capture the MessageEvent that _handle_ha_event produces.
# ---------------------------------------------------------------------------


def _make_adapter(**extra) -> HomeAssistantAdapter:
    config = PlatformConfig(enabled=True, token="tok", extra=extra)
    adapter = HomeAssistantAdapter(config)
    adapter.handle_message = AsyncMock()
    return adapter


def _make_event(entity_id, old_state, new_state, old_attrs=None, new_attrs=None):
    return {
        "data": {
            "entity_id": entity_id,
            "old_state": {"state": old_state, "attributes": old_attrs or {}},
            "new_state": {"state": new_state, "attributes": new_attrs or {"friendly_name": entity_id}},
        }
    }


class TestEventFilteringPipeline:
    @pytest.mark.asyncio
    async def test_ignored_entity_not_forwarded(self):
        adapter = _make_adapter(watch_all=True, ignore_entities=["sensor.uptime"])
        await adapter._handle_ha_event(_make_event("sensor.uptime", "100", "101"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unwatched_domain_not_forwarded(self):
        adapter = _make_adapter(watch_domains=["climate"])
        await adapter._handle_ha_event(_make_event("light.bedroom", "off", "on"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_watched_domain_forwarded(self):
        adapter = _make_adapter(watch_domains=["climate"], cooldown_seconds=0)
        await adapter._handle_ha_event(
            _make_event("climate.thermostat", "off", "heat",
                        new_attrs={"friendly_name": "Thermostat", "current_temperature": 20, "temperature": 22})
        )
        adapter.handle_message.assert_called_once()

        # Verify the actual MessageEvent text content
        msg_event = adapter.handle_message.call_args[0][0]
        assert "Thermostat" in msg_event.text
        assert "heat" in msg_event.text
        assert msg_event.source.platform == Platform.HOMEASSISTANT
        assert msg_event.source.chat_id == "ha_events"


# ---------------------------------------------------------------------------
# Cooldown behavior
# ---------------------------------------------------------------------------


class TestCooldown:

    @pytest.mark.asyncio
    async def test_cooldown_expires(self):
        adapter = _make_adapter(watch_all=True, cooldown_seconds=1)

        event = _make_event("sensor.temp", "20", "21",
                            new_attrs={"friendly_name": "Temp"})
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

        # Simulate time passing beyond cooldown
        adapter._last_event_time["sensor.temp"] = time.time() - 2

        event2 = _make_event("sensor.temp", "21", "22",
                             new_attrs={"friendly_name": "Temp"})
        await adapter._handle_ha_event(event2)
        assert adapter.handle_message.call_count == 2


# ---------------------------------------------------------------------------
# Flap suppression (issue #71): dedup repeated (entity, state) toggles
# before they cost an LLM round-trip
# ---------------------------------------------------------------------------


class TestFlapSuppression:
    """Known-flap events are suppressed at the integration layer; novel
    state changes always pass through."""

    @staticmethod
    def _clock(monkeypatch, start=1000.0):
        """Freeze ``time.time`` in the adapter module on a controllable clock."""
        state = {"now": start}
        monkeypatch.setattr(
            "plugins.platforms.homeassistant.adapter.time.time",
            lambda: state["now"],
        )
        return state

    @staticmethod
    def _adapter(**extra) -> HomeAssistantAdapter:
        # cooldown_seconds=0 so only the flap suppressor gates the event.
        return _make_adapter(watch_all=True, cooldown_seconds=0, **extra)

    @pytest.mark.asyncio
    async def test_identical_flap_within_window_suppressed(self, monkeypatch):
        clock = self._clock(monkeypatch)
        adapter = self._adapter(flap_suppression_seconds=60)
        event = _make_event(
            "device_tracker.person",
            "home",
            "away",
            new_attrs={"friendly_name": "Person"},
        )

        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

        # Same entity toggling to the same state again inside the window.
        clock["now"] += 10
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_state_change_within_window_passes_through(self, monkeypatch):
        clock = self._clock(monkeypatch)
        adapter = self._adapter(flap_suppression_seconds=60)
        away_event = _make_event(
            "device_tracker.person",
            "home",
            "away",
            new_attrs={"friendly_name": "Person"},
        )
        home_event = _make_event(
            "device_tracker.person",
            "away",
            "home",
            new_attrs={"friendly_name": "Person"},
        )

        await adapter._handle_ha_event(away_event)
        assert adapter.handle_message.call_count == 1

        # A genuine toggle back is a different target state: must pass.
        clock["now"] += 5
        await adapter._handle_ha_event(home_event)
        assert adapter.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_window_expiry_allows_re_notification(self, monkeypatch):
        clock = self._clock(monkeypatch)
        adapter = self._adapter(flap_suppression_seconds=60)
        event = _make_event(
            "device_tracker.person",
            "home",
            "away",
            new_attrs={"friendly_name": "Person"},
        )

        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

        # After the window expires the identical flap is novel again.
        clock["now"] += 61
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_different_entities_independent(self, monkeypatch):
        clock = self._clock(monkeypatch)
        adapter = self._adapter(flap_suppression_seconds=60)

        await adapter._handle_ha_event(
            _make_event(
                "device_tracker.person_a",
                "home",
                "away",
                new_attrs={"friendly_name": "Person A"},
            )
        )
        assert adapter.handle_message.call_count == 1

        # Same state, different entity: independent key, must pass.
        clock["now"] += 5
        await adapter._handle_ha_event(
            _make_event(
                "device_tracker.person_b",
                "home",
                "away",
                new_attrs={"friendly_name": "Person B"},
            )
        )
        assert adapter.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_config_driven_window(self, monkeypatch):
        clock = self._clock(monkeypatch)
        adapter = self._adapter(flap_suppression_seconds=30)
        assert adapter._flap_suppressor.window_seconds == 30.0
        event = _make_event(
            "binary_sensor.motion",
            "off",
            "on",
            new_attrs={"friendly_name": "Motion"},
        )

        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

        # Inside the configured 30s window: suppressed.
        clock["now"] += 20
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

        # Past the configured window: re-notified.
        clock["now"] += 11
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_non_int_window_falls_back_to_default(self, monkeypatch):
        clock = self._clock(monkeypatch)
        adapter = self._adapter(flap_suppression_seconds="not-a-number")
        # Unparseable config falls back to the 60s default...
        assert adapter._flap_suppressor.window_seconds == 60.0

        event = _make_event(
            "binary_sensor.motion",
            "off",
            "on",
            new_attrs={"friendly_name": "Motion"},
        )
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

        # ...so a repeat inside 60s is still suppressed.
        clock["now"] += 10
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_zero_window_disables_suppression(self, monkeypatch):
        clock = self._clock(monkeypatch)
        adapter = self._adapter(flap_suppression_seconds=0)
        event = _make_event(
            "binary_sensor.motion",
            "off",
            "on",
            new_attrs={"friendly_name": "Motion"},
        )

        await adapter._handle_ha_event(event)
        clock["now"] += 1
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 2


class TestFlapSuppressorUnit:
    """Direct unit tests for the FlapSuppressor tracker (explicit ``now``)."""

    def test_should_suppress_keyed_on_entity_and_state(self):
        suppressor = FlapSuppressor(window_seconds=60)
        assert suppressor.should_suppress("sensor.a", "on", now=1000.0) is False
        # Same (entity, state) inside the window.
        assert suppressor.should_suppress("sensor.a", "on", now=1010.0) is True
        # Same entity, different state: novel.
        assert suppressor.should_suppress("sensor.a", "off", now=1010.0) is False
        # Same state, different entity: novel.
        assert suppressor.should_suppress("sensor.b", "on", now=1010.0) is False
        # Window expiry: identical flap is novel again.
        assert suppressor.should_suppress("sensor.a", "on", now=1070.0) is False

    def test_suppressed_events_do_not_extend_window(self):
        suppressor = FlapSuppressor(window_seconds=60)
        assert suppressor.should_suppress("sensor.a", "on", now=1000.0) is False
        assert suppressor.should_suppress("sensor.a", "on", now=1010.0) is True
        assert suppressor.should_suppress("sensor.a", "on", now=1020.0) is True
        # Still anchored on the forwarded occurrence at t=1000.
        assert suppressor.should_suppress("sensor.a", "on", now=1061.0) is False

    def test_zero_window_never_suppresses(self):
        suppressor = FlapSuppressor(window_seconds=0)
        assert suppressor.should_suppress("sensor.a", "on", now=1.0) is False
        assert suppressor.should_suppress("sensor.a", "on", now=1.0) is False

    def test_prune_keeps_tracker_bounded(self):
        suppressor = FlapSuppressor(window_seconds=60)
        for i in range(100):
            suppressor.should_suppress(f"sensor.e{i}", "on", now=1000.0 + i * 0.1)
        # Advance far past the window: everything expires on the next call.
        suppressor.should_suppress("sensor.fresh", "on", now=2000.0)
        assert all(ts >= 2000.0 - 60.0 for ts in suppressor._last_seen.values())


class TestParseWindowSeconds:
    def test_accepts_int_float_and_numeric_string(self):
        assert _parse_window_seconds(30) == 30.0
        assert _parse_window_seconds(30.5) == 30.5
        assert _parse_window_seconds("45") == 45.0

    def test_unparseable_falls_back_to_default(self):
        assert _parse_window_seconds("nope") == 60.0
        assert _parse_window_seconds(None) == 60.0
        assert _parse_window_seconds([]) == 60.0
        # bool is an int subclass; a YAML `true` must not read as 1s.
        assert _parse_window_seconds(True) == 60.0
        assert _parse_window_seconds(False) == 60.0
        assert _parse_window_seconds(float("inf")) == 60.0

    def test_zero_and_negative_disable(self):
        assert _parse_window_seconds(0) == 0.0
        assert _parse_window_seconds(-5) == -5.0
        suppressor = FlapSuppressor(_parse_window_seconds(0))
        assert suppressor.window_seconds == 0.0


# ---------------------------------------------------------------------------
# Config integration (env overrides, round-trip)
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    def test_env_override_creates_ha_platform(self, monkeypatch):
        monkeypatch.setenv("HASS_TOKEN", "env-token")
        monkeypatch.setenv("HASS_URL", "http://10.0.0.5:8123")
        # Clear other platform tokens
        for v in ["TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN"]:
            monkeypatch.delenv(v, raising=False)

        from gateway.config import load_gateway_config
        config = load_gateway_config()

        assert Platform.HOMEASSISTANT in config.platforms
        ha = config.platforms[Platform.HOMEASSISTANT]
        assert ha.enabled is True
        assert ha.token == "env-token"
        assert ha.extra["url"] == "http://10.0.0.5:8123"


# ---------------------------------------------------------------------------
# send() via REST API
# ---------------------------------------------------------------------------


class TestSendViaRestApi:
    """send() uses REST API (not WebSocket) to avoid race conditions."""

    @staticmethod
    def _mock_aiohttp_session(response_status=200, response_text="OK"):
        """Build a mock aiohttp session + response for async-with patterns.

        aiohttp.ClientSession() is a sync constructor whose return value
        is used as ``async with session:``.  ``session.post(...)`` returns a
        context-manager (not a coroutine), so both layers use MagicMock for
        the call and AsyncMock only for ``__aenter__`` / ``__aexit__``.
        """
        mock_response = MagicMock()
        mock_response.status = response_status
        mock_response.text = AsyncMock(return_value=response_text)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        return mock_session

    @pytest.mark.asyncio
    async def test_send_success(self):
        adapter = _make_adapter()
        mock_session = self._mock_aiohttp_session(200)

        with patch("plugins.platforms.homeassistant.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events", "Test notification")

        assert result.success is True
        # Verify the REST API was called with correct payload
        call_args = mock_session.post.call_args
        assert "/api/services/persistent_notification/create" in call_args[0][0]
        assert call_args[1]["json"]["title"] == "Hermes Agent"
        assert call_args[1]["json"]["message"] == "Test notification"
        assert "Bearer tok" in call_args[1]["headers"]["Authorization"]


# ---------------------------------------------------------------------------
# Toolset integration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket URL construction
# ---------------------------------------------------------------------------




class TestLocalNetworkConnectHint:
    def test_ehostunreach_outside_launchd_is_a_plain_unreachable_host(self, monkeypatch):
        """The same errno from a Terminal-run gateway (or another OS) must not be blamed on macOS."""
        from plugins.platforms.homeassistant.adapter import _connect_error_detail

        monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
        monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
        err = OSError(errno.EHOSTUNREACH, "No route to host")
        assert _connect_error_detail(err) == str(err)
        assert _connect_error_detail(RuntimeError("auth failed")) == "auth failed"

    @pytest.mark.macos_only
    def test_ehostunreach_under_launchd_names_the_remedy(self, monkeypatch):
        """Only the launchd-supervised gateway can be denied by Local Network Privacy (#71206)."""
        from plugins.platforms.homeassistant.adapter import _connect_error_detail

        monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
        err = OSError(errno.EHOSTUNREACH, "No route to host")
        detail = _connect_error_detail(err)
        assert detail.startswith(str(err))
        assert len(detail) > len(str(err))  # a remedy hint is appended
