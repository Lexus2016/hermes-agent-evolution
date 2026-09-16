"""Per-platform display/verbosity resolver (``resolve_display_setting``).

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

# Settings configurable per-platform; other display settings are CLI-only.
_GLOBAL_DEFAULTS: dict[str, Any] = {
    "tool_progress": "all",
    "tool_progress_grouping": "accumulate",  # "accumulate" = edit one bubble; "separate" = one msg per tool
    "show_reasoning": False,
    "reasoning_style": "code",  # "code" (💭 **Reasoning:** + fence), "blockquote" ("> "), "subtext" ("-# " Discord)
    "tool_preview_length": 0,
    "streaming": None,  # None = follow top-level streaming config
    # Gateway-only assistant/status chatter; mobile platforms opt down to final-answer-first.
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
    "busy_steer_ack_enabled": True,  # busy_input_mode=steer echo; the text still lands in the run
    # Delete tool-progress / "⏳ Working" bubbles after a SUCCESSFUL final response where deletion is
    # supported (Telegram); failed runs keep them as breadcrumbs.
    "cleanup_progress": False,
    # Working-state text on text-rendering indicators (Slack assistant status): "full"/true = verb +
    # argument preview, "verb" = verb only (keeps paths out of shared channels), "off"/false = static.
    "live_status": "full",
}

# Tiers: HIGH = editing, personal/team use; MEDIUM = editing but customer-facing;
# LOW = no edit support (progress messages are permanent); MINIMAL = batch delivery.
_TIER_HIGH = {
    "tool_progress": "all", "show_reasoning": False, "tool_preview_length": 40,
    "streaming": None,  # follow global
    "interim_assistant_messages": True, "long_running_notifications": True, "busy_ack_detail": True,
}
_TIER_MEDIUM = {**_TIER_HIGH, "tool_progress": "new"}
_TIER_LOW = {
    **_TIER_HIGH, "tool_progress": "off", "streaming": False,
    "interim_assistant_messages": False, "long_running_notifications": False, "busy_ack_detail": False,
}
_TIER_MINIMAL = {**_TIER_LOW, "tool_preview_length": 0}

_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    # Mobile inbox: quiet tool_progress / busy-ack, but keep interim commentary and heartbeats so it
    # doesn't look like "typing..." for 30 minutes.
    "telegram": {**_TIER_HIGH, "tool_progress": "off", "busy_ack_detail": False},
    "discord": {**_TIER_HIGH, "reasoning_style": "subtext"},  # "-# " subtext reads as metadata
    # Slack: Bolt posts cannot be edited like CLI; "new"/"all" spam permanent lines.
    "slack": {**_TIER_MEDIUM, "tool_progress": "off", "long_running_notifications": False, "busy_ack_detail": False},
    "mattermost": _TIER_MEDIUM,
    "matrix": _TIER_MEDIUM,
    "feishu": _TIER_MEDIUM,
    "buzz": _TIER_MEDIUM,  # Nostr: edits in place but channels are shared community spaces
    "signal": _TIER_LOW,
    "whatsapp": _TIER_MEDIUM,  # Baileys bridge supports /edit
    "whatsapp_cloud": _TIER_LOW,  # adapter lacks edit_message; promote once it lands
    "photon": _TIER_LOW,  # permanent-message iMessage inboxes (no edit)
    "bluebubbles": _TIER_LOW,
    "weixin": _TIER_LOW,
    # Non-editable, but its native "stream" msgtype gives a typing animation + cumulative updates.
    "wecom": {**_TIER_LOW, "streaming": True},
    "wecom_callback": _TIER_LOW,
    "dingtalk": _TIER_LOW,
    "email": _TIER_MINIMAL,
    "sms": _TIER_MINIMAL,
    "webhook": _TIER_MINIMAL,
    "homeassistant": _TIER_MINIMAL,
    "api_server": {**_TIER_HIGH, "tool_preview_length": 0},
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
    """Resolve a display setting with per-platform override support (see module docstring for order).

    ``platform_key`` is the platform config key (``"telegram"``; see ``_platform_config_key`` in
    gateway/run.py). Returns *fallback* when nothing is configured.
    """
    display_cfg = user_config.get("display") or {}
    if not isinstance(display_cfg, dict):
        display_cfg = {}

    # 0. Per-chat override (display.chat_overrides.<chat_id> / quiet_chats).
    if chat_id is not None:
        chat_val = _resolve_chat_override(display_cfg, str(chat_id), setting)
        if chat_val is not _CHAT_UNSET:
            return _normalise(setting, chat_val)

    plat_overrides = (display_cfg.get("platforms") or {}).get(platform_key)
    if isinstance(plat_overrides, dict) and plat_overrides.get(setting) is not None:
        return _normalise(setting, plat_overrides[setting])
    if setting == "tool_progress":  # legacy display.tool_progress_overrides.<platform>
        legacy = display_cfg.get("tool_progress_overrides")
        if isinstance(legacy, dict) and legacy.get(platform_key) is not None:
            return _normalise(setting, legacy[platform_key])
    if setting != "streaming" and display_cfg.get(setting) is not None:  # display.streaming is CLI-only
        return _normalise(setting, display_cfg[setting])
    val = _PLATFORM_DEFAULTS.get(platform_key, {}).get(setting)
    if val is None:
        val = _GLOBAL_DEFAULTS.get(setting)
    return fallback if val is None else val


# --- Normalisation of YAML quirks (bare ``off`` → False in YAML 1.1, etc.) ---

_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no"}


def _norm_tristate(on: str, off: str, choices: set, extra_truthy: set = frozenset()):
    """Normaliser for bool-or-keyword settings: bools/truthy tokens → *on*, falsy → *off*, else a known choice or *on*."""
    def norm(value: Any) -> str:
        if isinstance(value, bool):
            return on if value else off
        val = str(value).strip().lower()
        if val in _FALSY:
            return off
        if val in _TRUTHY | extra_truthy:
            return on
        return val if val in choices else on
    return norm


def _norm_bool(value: Any) -> bool:
    return value.strip().lower() in _TRUTHY | {"raw", "verbose"} if isinstance(value, str) else bool(value)


def _norm_long_running(value: Any) -> Any:
    return "generic" if isinstance(value, str) and value.strip().lower() == "generic" else _norm_bool(value)


def _norm_cleanup_progress(value: Any) -> bool:
    return value.lower() in _TRUTHY if isinstance(value, str) else bool(value)


def _norm_choice(choices: tuple[str, ...]) -> Any:
    def norm(value: Any) -> str:
        val = str(value).lower()
        return val if val in choices else choices[0]

    return norm


def _norm_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_NORMALISERS: dict[str, Any] = {
    "tool_progress": _norm_tristate("all", "off", {"off", "new", "all", "verbose", "log"}),
    "show_reasoning": _norm_bool,
    "streaming": _norm_bool,
    "interim_assistant_messages": _norm_bool,
    "long_running_notifications": _norm_long_running,
    "busy_ack_detail": _norm_bool,
    "busy_steer_ack_enabled": _norm_bool,
    "thinking_progress": _norm_bool,
    "cleanup_progress": _norm_cleanup_progress,
    "live_status": _norm_tristate("full", "off", {"full", "verb", "off"}, extra_truthy={"all"}),
    "tool_progress_grouping": _norm_choice(("accumulate", "separate")),
    "reasoning_style": _norm_choice(("code", "blockquote", "subtext")),
    "tool_preview_length": _norm_int,
}


def _normalise(setting: str, value: Any) -> Any:
    """Normalise a user-supplied value for *setting*; unknown settings pass through."""
    norm = _NORMALISERS.get(setting)
    return norm(value) if norm else value
