"""Tests for per-chat display verbosity overrides (issue #924).

Covers ``resolve_display_setting(..., chat_id=...)`` step-0 resolution, the
verbosity mode presets, the ``quiet_chats`` shorthand, the
``resolve_chat_mode`` / ``chat_delivery_suppressed`` helpers, and the strict
regression guarantee that ``chat_id=None`` leaves existing resolution
byte-for-byte identical.
"""

import pytest

from gateway.display_config import (
    resolve_display_setting,
    resolve_chat_mode,
    chat_delivery_suppressed,
    _MODE_PRESETS,
    _QUIET_PRESET,
    _VERBOSE_PRESET,
)


# ---------------------------------------------------------------------------
# Mode preset values match the issue's verbosity-modes table
# ---------------------------------------------------------------------------

class TestModePresets:
    def test_verbose_preset_values(self):
        assert _VERBOSE_PRESET["tool_progress"] == "all"
        assert _VERBOSE_PRESET["interim_assistant_messages"] is True
        assert _VERBOSE_PRESET["long_running_notifications"] is True
        assert _VERBOSE_PRESET["show_reasoning"] is True

    def test_quiet_preset_values(self):
        assert _QUIET_PRESET["tool_progress"] == "off"
        assert _QUIET_PRESET["interim_assistant_messages"] is False
        assert _QUIET_PRESET["long_running_notifications"] is False
        assert _QUIET_PRESET["busy_ack_detail"] is False
        assert _QUIET_PRESET["show_reasoning"] is False

    def test_normal_falls_through(self):
        # normal is None so the per-platform chain runs unchanged.
        assert _MODE_PRESETS["normal"] is None

    def test_silent_matches_quiet_for_settings(self):
        # silent resolves identically to quiet for per-setting display; its
        # extra delivery-suppression is expressed via chat_delivery_suppressed.
        assert _MODE_PRESETS["silent"] == _QUIET_PRESET

    def test_verbose_is_inverse_of_quiet(self):
        # Same key set, opposite values — predictable, symmetric presets.
        assert set(_VERBOSE_PRESET) == set(_QUIET_PRESET)


# ---------------------------------------------------------------------------
# Per-chat override resolution (step 0)
# ---------------------------------------------------------------------------

class TestChatOverrideResolution:
    def test_quiet_group_hides_progress_and_reasoning(self):
        cfg = {"display": {"chat_overrides": {"-100grp": {"mode": "quiet"}}}}
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="-100grp") == "off"
        assert resolve_display_setting(cfg, "telegram", "show_reasoning", chat_id="-100grp") is False
        assert resolve_display_setting(cfg, "telegram", "interim_assistant_messages", chat_id="-100grp") is False

    def test_verbose_dm_shows_everything(self):
        cfg = {"display": {"chat_overrides": {"dm1": {"mode": "verbose"}}}}
        # Telegram default tool_progress is "off"; verbose chat flips it to "all".
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="dm1") == "all"
        assert resolve_display_setting(cfg, "telegram", "show_reasoning", chat_id="dm1") is True

    def test_explicit_key_beats_mode_preset(self):
        # mode: quiet but explicit show_reasoning: true → reasoning still shows.
        cfg = {"display": {"chat_overrides": {"mix": {"mode": "quiet", "show_reasoning": True}}}}
        assert resolve_display_setting(cfg, "telegram", "show_reasoning", chat_id="mix") is True
        # Non-overridden keys still follow the quiet preset.
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="mix") == "off"

    def test_mode_normal_falls_through_to_platform_default(self):
        cfg = {"display": {"chat_overrides": {"n": {"mode": "normal"}}}}
        # Falls through unchanged: telegram platform default is "off".
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="n") == "off"
        # And discord's platform default "all" is preserved too.
        assert resolve_display_setting(cfg, "discord", "tool_progress", chat_id="n") == "all"

    def test_preset_without_this_setting_falls_through(self):
        # tool_preview_length is not in any mode preset → falls through to the
        # platform default even inside a quiet chat.
        cfg = {"display": {"chat_overrides": {"g": {"mode": "quiet"}}}}
        assert resolve_display_setting(cfg, "slack", "tool_preview_length", chat_id="g") == 40

    def test_unknown_mode_falls_through(self):
        cfg = {"display": {"chat_overrides": {"g": {"mode": "bogus"}}}}
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="g") == "off"

    def test_chat_value_normalised(self):
        # Explicit per-chat string values pass through _normalise like any other.
        cfg = {"display": {"chat_overrides": {"g": {"tool_progress": "false"}}}}
        assert resolve_display_setting(cfg, "discord", "tool_progress", chat_id="g") == "off"
        cfg2 = {"display": {"chat_overrides": {"g": {"show_reasoning": "yes"}}}}
        assert resolve_display_setting(cfg2, "telegram", "show_reasoning", chat_id="g") is True

    def test_int_chat_id_coerced_to_str(self):
        cfg = {"display": {"chat_overrides": {"12345": {"mode": "quiet"}}}}
        # Caller may pass an int chat id.
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id=12345) == "off"

    def test_int_keyed_chat_overrides_matched(self):
        # YAML/JSON may parse an unquoted numeric chat id as an int KEY.
        cfg = {"display": {"chat_overrides": {12345: {"mode": "quiet"}}}}
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="12345") == "off"
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id=12345) == "off"
        # Negative Telegram supergroup id as an int key.
        cfg2 = {"display": {"chat_overrides": {-1001234567890: {"mode": "verbose"}}}}
        assert resolve_display_setting(cfg2, "telegram", "tool_progress", chat_id="-1001234567890") == "all"

    def test_full_fallback_chain_chat_then_platform_then_tier_then_global(self):
        # A chat override for one setting doesn't leak into other settings /
        # other chats: the rest of the chain (platform → tier → global) still runs.
        cfg = {
            "display": {
                "chat_overrides": {"g": {"mode": "quiet"}},
                "platforms": {"discord": {"reasoning_style": "blockquote"}},
            }
        }
        # chat layer (quiet) for a covered key
        assert resolve_display_setting(cfg, "discord", "tool_progress", chat_id="g") == "off"
        # platform layer (explicit) for an uncovered key
        assert resolve_display_setting(cfg, "discord", "reasoning_style", chat_id="g") == "blockquote"
        # tier default for another uncovered key
        assert resolve_display_setting(cfg, "discord", "tool_preview_length", chat_id="g") == 40
        # global default for a key with no platform/tier entry
        assert resolve_display_setting(cfg, "discord", "tool_progress_grouping", chat_id="g") == "accumulate"


# ---------------------------------------------------------------------------
# quiet_chats shorthand
# ---------------------------------------------------------------------------

class TestQuietChatsShorthand:
    def test_membership_implies_quiet(self):
        cfg = {"display": {"quiet_chats": ["-100q", "-200q"]}}
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="-100q") == "off"
        assert resolve_display_setting(cfg, "telegram", "interim_assistant_messages", chat_id="-200q") is False

    def test_non_member_unaffected(self):
        cfg = {"display": {"quiet_chats": ["-100q"]}}
        # discord default is "all"; a non-listed chat keeps it.
        assert resolve_display_setting(cfg, "discord", "tool_progress", chat_id="other") == "all"

    def test_explicit_mode_beats_quiet_chats(self):
        cfg = {
            "display": {
                "quiet_chats": ["dm"],
                "chat_overrides": {"dm": {"mode": "verbose"}},
            }
        }
        # chat_overrides.mode wins over quiet_chats membership.
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="dm") == "all"

    def test_int_members_coerced(self):
        cfg = {"display": {"quiet_chats": [12345]}}
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id="12345") == "off"


# ---------------------------------------------------------------------------
# resolve_chat_mode / chat_delivery_suppressed helpers
# ---------------------------------------------------------------------------

class TestChatModeHelpers:
    def test_resolve_chat_mode(self):
        cfg = {
            "display": {
                "chat_overrides": {"g": {"mode": "quiet"}, "v": {"mode": "VERBOSE"}},
                "quiet_chats": ["q"],
            }
        }
        assert resolve_chat_mode(cfg, "g") == "quiet"
        assert resolve_chat_mode(cfg, "v") == "verbose"  # case-insensitive
        assert resolve_chat_mode(cfg, "q") == "quiet"  # via quiet_chats
        assert resolve_chat_mode(cfg, "unknown") is None
        assert resolve_chat_mode(cfg, None) is None

    def test_chat_delivery_suppressed(self):
        cfg = {"display": {"chat_overrides": {"s": {"mode": "silent"}, "g": {"mode": "quiet"}}}}
        assert chat_delivery_suppressed(cfg, "s") is True
        # quiet is NOT silent — quiet still delivers (final answer only).
        assert chat_delivery_suppressed(cfg, "g") is False
        assert chat_delivery_suppressed(cfg, "unknown") is False
        assert chat_delivery_suppressed(cfg, None) is False

    def test_helpers_tolerate_missing_display(self):
        assert resolve_chat_mode({}, "g") is None
        assert chat_delivery_suppressed({}, "g") is False


# ---------------------------------------------------------------------------
# Regression guard: chat_id=None is byte-for-byte identical to before
# ---------------------------------------------------------------------------

class TestChatIdNoneRegression:
    # (config, platform, setting) cases spanning every resolution layer.
    CASES = [
        ({}, "telegram", "tool_progress"),                       # platform default
        ({}, "discord", "tool_progress"),                        # tier default
        ({}, "unknown_platform", "tool_progress"),               # global default
        ({}, "telegram", "streaming"),                           # None sentinel
        ({}, "slack", "tool_preview_length"),                    # int
        ({"display": {"tool_progress": "new"}}, "telegram", "tool_progress"),  # global user
        ({"display": {"platforms": {"telegram": {"tool_progress": "verbose"}}}}, "telegram", "tool_progress"),  # platform override
        ({"display": {"tool_progress_overrides": {"signal": "off"}}}, "signal", "tool_progress"),  # legacy
        ({"display": {"reasoning_style": "subtext"}}, "telegram", "reasoning_style"),
    ]

    @pytest.mark.parametrize("cfg,platform,setting", CASES)
    def test_none_matches_no_arg(self, cfg, platform, setting):
        # Passing chat_id=None must equal omitting it entirely.
        assert (
            resolve_display_setting(cfg, platform, setting)
            == resolve_display_setting(cfg, platform, setting, chat_id=None)
        )

    def test_chat_overrides_present_but_no_chat_id_is_inert(self):
        # Even with chat_overrides configured, omitting chat_id must ignore them.
        cfg = {"display": {"chat_overrides": {"g": {"mode": "verbose"}}, "quiet_chats": ["g"]}}
        # telegram default stays "off" — the per-chat layer never runs.
        assert resolve_display_setting(cfg, "telegram", "tool_progress") == "off"
        assert resolve_display_setting(cfg, "telegram", "show_reasoning") is False
