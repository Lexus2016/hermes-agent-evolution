"""Per-platform display/verbosity configuration resolver.

Provides ``resolve_display_setting()`` — the single entry-point for reading
display settings with platform-specific overrides and sensible defaults.

Resolution order (first non-None wins):
    0. ``display.chat_overrides.<chat_id>`` / ``display.quiet_chats``
                                               — per-chat verbosity override
                                               (only when a ``chat_id`` is passed)
    1. ``display.platforms.<platform>.<key>``  — explicit per-platform user override
    2. ``display.<key>``                       — global user setting
    3. ``_PLATFORM_DEFAULTS[<platform>][<key>]``  — built-in sensible default
    4. ``_GLOBAL_DEFAULTS[<key>]``              — built-in global default

Per-chat overrides (step 0) let a single platform mix verbosity per chat/topic:
a customer-facing group can run ``mode: quiet`` (final answer only) while a DM
runs ``mode: verbose`` (full tool/reasoning detail).  A chat entry may set a
shorthand ``mode`` preset and/or individual setting keys (explicit keys win over
the preset).  ``display.quiet_chats`` is a convenience list of chat ids that
implies ``mode: quiet``.  Step 0 is inert unless the caller passes a ``chat_id``
— every existing call site omits it, so their resolution is byte-for-byte
unchanged.

Exception: ``display.streaming`` is CLI-only.  Gateway streaming follows the
top-level ``streaming`` config unless ``display.platforms.<platform>.streaming``
sets an explicit per-platform override.

Backward compatibility: ``display.tool_progress_overrides`` is still read as a
fallback for ``tool_progress`` when no ``display.platforms`` entry exists.  A
config migration (version bump) automatically moves the old format into the new
``display.platforms`` structure.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Overrideable display settings and their global defaults
# ---------------------------------------------------------------------------
# These are the settings that can be configured per-platform.
# Other display settings (compact, personality, skin, etc.) are CLI-only
# and don't participate in per-platform resolution.

_GLOBAL_DEFAULTS: dict[str, Any] = {
    "tool_progress": "all",
    "tool_progress_grouping": "accumulate",  # "accumulate" = edit one bubble; "separate" = one msg per tool
    "show_reasoning": False,
    # How a reasoning/thinking summary is rendered when show_reasoning is on.
    #   "code"      -> 💭 **Reasoning:** + fenced code block (legacy default)
    #   "blockquote"-> each line prefixed with "> "
    #   "subtext"   -> each line prefixed with "-# " (Discord small grey subtext)
    # Discord defaults to "subtext"; everywhere else defaults to "code".
    "reasoning_style": "code",
    "tool_preview_length": 0,
    "streaming": None,  # None = follow top-level streaming config
    # Gateway-only assistant/status chatter controls. These default on for
    # back-compat, but mobile platforms can opt down to final-answer-first.
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
    # Whether busy_input_mode=steer sends a visible "Steered into current run"
    # acknowledgment after successfully injecting the user's mid-turn message.
    # Disable when the platform should steer silently (the text still lands in
    # the active run; only the confirmation echo is suppressed).
    "busy_steer_ack_enabled": True,
    # When true, delete tool-progress / "⏳ Working — N min" / status bubbles
    # after the final response lands on platforms that support message
    # deletion (e.g. Telegram). Off by default — progress is still shown
    # live, just cleaned up after success so the chat doesn't fill up with
    # stale breadcrumbs. Failed runs leave bubbles in place as breadcrumbs.
    "cleanup_progress": False,
    # Live working-state status on platforms whose typing indicator renders
    # text (Slack's assistant status line). Values:
    #   "full" / true  -> verb + argument preview ("is running pytest…")
    #   "verb"         -> verb only ("is running…") — keeps file paths and
    #                     commands out of shared channels
    #   "off" / false  -> static text (typing_status_text or "is thinking...")
    # Independent of tool_progress: works even when progress bubbles are off
    # (Slack's default), and costs no extra API calls — the existing typing
    # refresh cadence just renders different text.
    "live_status": "full",
}

# ---------------------------------------------------------------------------
# Sensible per-platform defaults — tiered by platform capability
# ---------------------------------------------------------------------------
# Tier 1 (high): Supports message editing, typically personal/team use
# Tier 2 (medium): Supports editing but often workspace/customer-facing
# Tier 3 (low): No edit support — each progress msg is permanent
# Tier 4 (minimal): Batch/non-interactive delivery

_TIER_HIGH = {
    "tool_progress": "all",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": None,  # follow global
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
}

_TIER_MEDIUM = {
    "tool_progress": "new",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": None,
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
}

_TIER_LOW = {
    "tool_progress": "off",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": False,
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
}

_TIER_MINIMAL = {
    "tool_progress": "off",
    "show_reasoning": False,
    "tool_preview_length": 0,
    "streaming": False,
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
}

_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    # Tier 1 — full edit support, personal/team use
    # Telegram is usually a mobile inbox: keep tool_progress quiet and skip
    # the verbose busy-ack iteration counter, but DO surface real mid-turn
    # assistant commentary (interim_assistant_messages) and DO send periodic
    # heartbeats (long_running_notifications) so the user has signal between
    # turn start and final answer. Otherwise it looks like "typing..." for
    # 30 minutes with nothing happening. Opt in to verbose iteration detail
    # via display.platforms.telegram.busy_ack_detail / tool_progress.
    "telegram":    {
        **_TIER_HIGH,
        "tool_progress": "off",
        "busy_ack_detail": False,
    },
    # Discord has a native "subtext" primitive (-# small grey text) that reads
    # as metadata rather than content, so reasoning summaries default to it
    # here instead of the fenced code block used elsewhere.
    "discord":     {**_TIER_HIGH, "reasoning_style": "subtext"},

    # Tier 2 — edit support, often customer/workspace channels
    # Slack: tool_progress off by default — Bolt posts cannot be edited like CLI;
    # "new"/"all" spam permanent lines in channels (hermes-agent#14663).
    "slack":           {**_TIER_MEDIUM, "tool_progress": "off"},
    "mattermost":      _TIER_MEDIUM,
    "matrix":          _TIER_MEDIUM,
    "feishu":          _TIER_MEDIUM,

    # Tier 3 — no edit support, progress messages are permanent
    "signal":          _TIER_LOW,
    "whatsapp":        _TIER_MEDIUM,  # Baileys bridge supports /edit
    # WhatsApp Cloud API: Meta added message editing in 2023 but the
    # Hermes Cloud adapter doesn't implement edit_message yet, so we
    # stay on TIER_LOW (tool_progress off) to avoid spamming each
    # status update as a separate message. Promote to TIER_MEDIUM once
    # Cloud's edit_message lands.
    "whatsapp_cloud":  _TIER_LOW,
    "bluebubbles":     _TIER_LOW,
    "weixin":          _TIER_LOW,
    "wecom":           _TIER_LOW,
    "wecom_callback":  _TIER_LOW,
    "dingtalk":        _TIER_LOW,

    # Tier 4 — batch or non-interactive delivery
    "email":           _TIER_MINIMAL,
    "sms":             _TIER_MINIMAL,
    "webhook":         _TIER_MINIMAL,
    "homeassistant":   _TIER_MINIMAL,
    "api_server":      {**_TIER_HIGH, "tool_preview_length": 0},
}

# Canonical set of per-platform overrideable keys (for validation).
OVERRIDEABLE_KEYS = frozenset(_GLOBAL_DEFAULTS.keys())


# ---------------------------------------------------------------------------
# Per-chat verbosity modes (shorthand presets)
# ---------------------------------------------------------------------------
# A chat entry (``display.chat_overrides.<chat_id>``) can set ``mode: <name>``
# as a shorthand for a bundle of individual display settings.  The table below
# is the source of truth for the modes documented in the SKILL/issue:
#
#   Mode     | tool_progress | interim | long_running | busy_ack | reasoning
#   ---------|:-------------:|:-------:|:------------:|:--------:|:--------:
#   verbose  |     all       |  True   |    True      |  True    |  True
#   normal   | (fall through to the existing per-platform chain — no override)
#   quiet    |     off       |  False  |    False     |  False   |  False
#   silent   |     off       |  False  |    False     |  False   |  False
#
# ``silent`` resolves identically to ``quiet`` for per-setting display; its
# EXTRA meaning — suppress delivery entirely — is expressed via
# ``chat_delivery_suppressed()``, NOT a pseudo-setting key.  Keeping delivery
# suppression out of the preset dict avoids leaking a fake "_suppress_delivery"
# key into the normal setting-resolution path.
_VERBOSE_PRESET: dict[str, Any] = {
    "tool_progress": "all",
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
    "show_reasoning": True,
}

_QUIET_PRESET: dict[str, Any] = {
    "tool_progress": "off",
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
    "show_reasoning": False,
}

# silent == quiet for per-setting resolution (delivery suppression is separate).
_SILENT_PRESET: dict[str, Any] = dict(_QUIET_PRESET)

_MODE_PRESETS: dict[str, dict[str, Any] | None] = {
    "verbose": _VERBOSE_PRESET,
    "normal": None,  # None → fall through to the existing per-platform chain
    "quiet": _QUIET_PRESET,
    "silent": _SILENT_PRESET,
}

# Sentinel: distinguishes "per-chat layer produced no value for this setting"
# (fall through to the platform chain) from a legitimately resolved ``None``.
_CHAT_UNSET = object()


def _chat_override_entry(display_cfg: dict, chat_id: str) -> Any:
    """Look up ``chat_overrides.<chat_id>`` tolerating non-string keys.

    YAML/JSON often parse numeric chat ids as ints (e.g. ``12345`` or
    ``-1001234567890``), so a plain ``dict.get(str_chat_id)`` would silently
    miss them.  ``chat_id`` is always a str here; match on the stringified key.
    """
    overrides = display_cfg.get("chat_overrides")
    if not isinstance(overrides, dict):
        return None
    entry = overrides.get(chat_id)
    if entry is None:
        for key, value in overrides.items():
            if str(key) == chat_id:
                return value
    return entry


def _chat_mode_for(display_cfg: dict, chat_id: str) -> str | None:
    """Resolve the effective verbosity *mode* for a chat, or None.

    Priority: an explicit ``chat_overrides.<chat_id>.mode`` beats the
    ``quiet_chats`` shorthand.  Returns a canonical lowercase mode name
    (``verbose``/``normal``/``quiet``/``silent``) or None when the chat has no
    mode configured.
    """
    chat_cfg = _chat_override_entry(display_cfg, chat_id)
    if isinstance(chat_cfg, dict):
        mode = chat_cfg.get("mode")
        if isinstance(mode, str):
            mode_norm = mode.strip().lower()
            if mode_norm in _MODE_PRESETS:
                return mode_norm
    quiet_chats = display_cfg.get("quiet_chats")
    if isinstance(quiet_chats, (list, tuple, set)):
        if chat_id in {str(c) for c in quiet_chats}:
            return "quiet"
    return None


def _resolve_chat_override(display_cfg: dict, chat_id: str, setting: str) -> Any:
    """Return a per-chat value for *setting*, or ``_CHAT_UNSET`` to fall through.

    An explicit individual key on the chat entry beats the mode preset (e.g.
    ``mode: quiet`` with an explicit ``show_reasoning: true`` still shows
    reasoning in that chat).  ``mode: normal`` (or any preset that does not
    define *setting*) yields ``_CHAT_UNSET`` so the existing platform chain runs
    unchanged.
    """
    chat_cfg = _chat_override_entry(display_cfg, chat_id)
    if isinstance(chat_cfg, dict) and setting != "mode" and setting in chat_cfg:
        return chat_cfg[setting]
    mode = _chat_mode_for(display_cfg, chat_id)
    if mode:
        preset = _MODE_PRESETS.get(mode)
        if preset is not None and setting in preset:
            return preset[setting]
    return _CHAT_UNSET


def resolve_chat_mode(user_config: dict, chat_id: str | None) -> str | None:
    """Public helper: the resolved verbosity mode for a chat, or None.

    Used by the cron delivery path (``cron/scheduler.py``) to map a delivery
    target's per-chat mode onto a ``delivery_verbosity`` level.
    """
    if chat_id is None:
        return None
    display_cfg = user_config.get("display") or {}
    if not isinstance(display_cfg, dict):
        return None
    return _chat_mode_for(display_cfg, str(chat_id))


def chat_delivery_suppressed(user_config: dict, chat_id: str | None) -> bool:
    """True when the chat's resolved mode is ``silent`` (suppress delivery)."""
    return resolve_chat_mode(user_config, chat_id) == "silent"


def resolve_display_setting(
    user_config: dict,
    platform_key: str,
    setting: str,
    fallback: Any = None,
    chat_id: str | None = None,
) -> Any:
    """Resolve a display setting with per-platform override support.

    Parameters
    ----------
    user_config : dict
        The full parsed config.yaml dict.
    platform_key : str
        Platform config key (e.g. ``"telegram"``, ``"slack"``).  Use
        ``_platform_config_key(source.platform)`` from gateway/run.py.
    setting : str
        Display setting name (e.g. ``"tool_progress"``, ``"show_reasoning"``).
    fallback : Any
        Fallback value when the setting isn't found anywhere.
    chat_id : str | None
        When set, the per-chat override layer (step 0) runs first: a resolved
        ``chat_overrides.<chat_id>`` key / mode-preset value (or a
        ``quiet_chats`` membership) short-circuits the platform chain.  When
        None (every existing call site), step 0 is skipped and resolution is
        identical to before.

    Returns
    -------
    The resolved value, or *fallback* if nothing is configured.
    """
    display_cfg = user_config.get("display") or {}
    if not isinstance(display_cfg, dict):
        display_cfg = {}

    # 0. Per-chat override (display.chat_overrides.<chat_id> / quiet_chats).
    # Inert unless a chat_id is supplied — preserves existing behaviour exactly
    # for every caller that omits it.  ``mode: normal`` and presets that do not
    # define this setting fall through to the platform chain UNCHANGED.
    if chat_id is not None:
        chat_val = _resolve_chat_override(display_cfg, str(chat_id), setting)
        if chat_val is not _CHAT_UNSET:
            return _normalise(setting, chat_val)

    # 1. Explicit per-platform override (display.platforms.<platform>.<key>)
    platforms = display_cfg.get("platforms") or {}
    plat_overrides = platforms.get(platform_key)
    if isinstance(plat_overrides, dict):
        val = plat_overrides.get(setting)
        if val is not None:
            return _normalise(setting, val)

    # 1b. Backward compat: display.tool_progress_overrides.<platform>
    if setting == "tool_progress":
        legacy = display_cfg.get("tool_progress_overrides")
        if isinstance(legacy, dict):
            val = legacy.get(platform_key)
            if val is not None:
                return _normalise(setting, val)

    # 2. Global user setting (display.<key>).  Skip display.streaming because
    # that key controls only CLI terminal streaming; gateway token streaming is
    # governed by the top-level streaming config plus per-platform overrides.
    if setting != "streaming":
        val = display_cfg.get(setting)
        if val is not None:
            return _normalise(setting, val)

    # 3. Built-in platform default
    plat_defaults = _PLATFORM_DEFAULTS.get(platform_key)
    if plat_defaults:
        val = plat_defaults.get(setting)
        if val is not None:
            return val

    # 4. Built-in global default
    val = _GLOBAL_DEFAULTS.get(setting)
    if val is not None:
        return val

    return fallback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(setting: str, value: Any) -> Any:
    """Normalise YAML quirks (bare ``off`` → False in YAML 1.1)."""
    if setting == "tool_progress":
        if value is False:
            return "off"
        if value is True:
            return "all"
        val = str(value).strip().lower()
        if val in {"false", "0", "no"}:
            return "off"
        if val in {"true", "1", "yes", "on"}:
            return "all"
        return val if val in {"off", "new", "all", "verbose", "log"} else "all"
    if setting in {
        "show_reasoning",
        "streaming",
        "interim_assistant_messages",
        "long_running_notifications",
        "busy_ack_detail",
        "busy_steer_ack_enabled",
        "thinking_progress",
    }:
        if isinstance(value, str):
            val = value.strip().lower()
            if val == "generic" and setting == "long_running_notifications":
                return "generic"
            return val in {"true", "1", "yes", "on", "raw", "verbose"}
        return bool(value)
    if setting == "cleanup_progress":
        if isinstance(value, str):
            return value.lower() in {"true", "1", "yes", "on"}
        return bool(value)
    if setting == "live_status":
        # Tri-state: "full" (verb + preview), "verb" (verb only), "off".
        if value is True:
            return "full"
        if value is False:
            return "off"
        val = str(value).strip().lower()
        if val in {"true", "1", "yes", "on", "all"}:
            return "full"
        if val in {"false", "0", "no"}:
            return "off"
        return val if val in {"full", "verb", "off"} else "full"
    if setting == "tool_progress_grouping":
        val = str(value).lower()
        return val if val in ("accumulate", "separate") else "accumulate"
    if setting == "reasoning_style":
        val = str(value).lower()
        return val if val in ("code", "blockquote", "subtext") else "code"
    if setting == "tool_preview_length":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return value
