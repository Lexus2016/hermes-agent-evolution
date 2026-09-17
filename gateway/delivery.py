"""Delivery routing for cron job outputs and agent responses, by target: explicit ("telegram:123456789"),
platform home channel ("telegram"), origin (back to where the job was created), or local (files)."""

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

from hermes_cli.config import get_hermes_home

from .config import Platform, GatewayConfig, PlatformConfig
from .session import SessionSource
from .dead_targets import DeadTargetRegistry, classify_dead_error

logger = logging.getLogger(__name__)

# Cap before gateway-level truncation of cron output for non-chunking platform delivery. Telegram's hard
# API limit is 4096; the headroom covers the "full output saved to …" footer. Adapters that split long
# messages natively (splits_long_messages) bypass this entirely.
MAX_PLATFORM_OUTPUT = 4000
# Matches strings that are *only* a "silence" narration with optional markdown wrappers (*(silent)*,
# _silent_, 🔇, a bare ".", "…"). Anchored so messages that merely *contain* "silent" never match.
_SILENCE_NARRATION = re.compile(
    r'^[\s*_~`]*\(?\s*(silent|silence|no\s+response|no\s+reply)\s*\.?\)?[\s*_~`]*$'
    r'|^[\s*_~`]*[\U0001F507\.\u2026]+[\s*_~`]*$',
    re.IGNORECASE,
)
_THREAD_ROUTING_KEYS = ("thread_id", "message_thread_id", "direct_messages_topic_id", "telegram_direct_messages_topic_id")


def _is_silence_narration(content: Optional[str]) -> bool:
    """True when ``content`` is *only* a silence-narration token (length-guarded)."""
    stripped = content.strip() if content else ""
    return bool(stripped) and len(stripped) <= 64 and bool(_SILENCE_NARRATION.match(stripped))


@dataclass(frozen=True)
class DeliveryTransport:
    """Resolved live transport for one logical delivery platform."""
    adapter: Any
    config: Optional[PlatformConfig]
    transport_platform: Platform

    @property
    def is_relay(self) -> bool:
        return self.transport_platform == Platform.RELAY

    async def send(self, logical_platform: Platform, chat_id: str, content: str,
                   metadata: Optional[Dict[str, Any]]) -> Any:
        """Send through this transport while preserving the logical platform."""
        return await (self.adapter.send_for_platform(logical_platform, chat_id, content, metadata=metadata)
                      if self.is_relay else self.adapter.send(chat_id, content, metadata=metadata))


def resolve_delivery_transport(platform: Platform, config: GatewayConfig,
                               adapters: Optional[Dict[Platform, Any]]) -> Optional[DeliveryTransport]:
    """Resolve a logical platform to its live delivery transport. A concrete native adapter always wins;
    Relay is eligible only when its authenticated transport explicitly advertises that it fronts the
    logical platform, so restart-time delivery is independent of per-chat caches without letting Relay
    hijack unrelated platform targets."""
    live_adapters = adapters or {}
    native, native_config = live_adapters.get(platform), config.platforms.get(platform)
    # Explicitly supplied live adapters with no config block are honored, but an
    # explicitly disabled native adapter never shadows an enabled Relay transport.
    if native is not None and (native_config is None or native_config.enabled):
        return DeliveryTransport(native, native_config, platform)
    relay, relay_config = live_adapters.get(Platform.RELAY), config.platforms.get(Platform.RELAY)
    fronts_platform = getattr(relay, "fronts_platform", None)
    if (relay is not None and (relay_config is None or relay_config.enabled)
            and callable(fronts_platform) and fronts_platform(platform)):
        return DeliveryTransport(relay, relay_config, Platform.RELAY)
    return None


def looks_like_telegram_private_chat_id(chat_id: Optional[str]) -> bool:
    """True when ``chat_id`` is a positive int — Telegram's private-chat shape (groups/channels are negative).
    Single source of truth, reused by the handoff seed path in ``gateway/run.py`` so handoff-created DM
    topics key the same way as inbound DM-topic messages."""
    try:
        return int(chat_id) > 0
    except (TypeError, ValueError):
        return False


def _looks_like_int(value: Optional[str]) -> bool:
    try:
        return int(value) is not None
    except (TypeError, ValueError):
        return False


def _send_result_error(result: Any) -> Optional[str]:
    """Error string of a failed SendResult object / plain result dict ("" if none), or None on success."""
    get = result.get if isinstance(result, dict) else (lambda name, default=None: getattr(result, name, default))
    return None if get("success", True) is not False else str(get("error") or "")


@dataclass
class DeliveryTarget:
    """One target: "origin", "local", "telegram" (home channel) or "telegram:123456[:thread]"."""
    platform: Platform
    chat_id: Optional[str] = None  # None means use home channel
    thread_id: Optional[str] = None
    is_origin: bool = False
    is_explicit: bool = False  # True if chat_id was explicitly specified
    # Raw target string when the platform name is unknown. The platform falls
    # back to LOCAL for routing, but the original name is preserved so
    # deliver() can report {success: False, error: unknown_platform} instead
    # of silently misrouting to local files.
    unknown_platform: Optional[str] = None

    @classmethod
    def parse(cls, target: str, origin: Optional[SessionSource] = None) -> "DeliveryTarget":
        """Parse "origin" | "local" | "<platform>" | "<platform>:<chat_id>[:<thread_id>]"."""
        target = target.strip()
        if target.lower() == "origin":
            return (cls(platform=origin.platform, chat_id=origin.chat_id, thread_id=origin.thread_id, is_origin=True)
                    if origin else cls(platform=Platform.LOCAL, is_origin=True))
        # Platform names are case-insensitive; chat/thread ids keep case. Unknown platforms ->
        # LOCAL for routing but preserve the raw target so deliver() reports
        # unknown_platform instead of silently saving locally.
        parts = target.split(":", 2)
        try:
            platform = Platform(parts[0].lower())
        except ValueError:
            return cls(platform=Platform.LOCAL, unknown_platform=target)
        return (cls(platform=platform, chat_id=parts[1], thread_id=parts[2] if len(parts) > 2 else None, is_explicit=True)
                if len(parts) > 1 else cls(platform=platform))

    def to_string(self) -> str:
        """Convert back to string format."""
        if self.is_origin:
            return "origin"
        if self.unknown_platform is not None:
            return self.unknown_platform
        if self.platform == Platform.LOCAL:
            return "local"
        parts = [self.platform.value, self.chat_id, self.thread_id if self.chat_id else None]
        return ":".join(p for p in parts if p)


async def _ensure_named_dm_topic(adapter: Any, chat_id: str, name: str, *, refresh: bool) -> str:
    """Create (or force-recreate) a named Telegram private DM topic; return its thread id."""
    verb, ensure_dm_topic = "refresh" if refresh else "create", getattr(adapter, "ensure_dm_topic", None)
    if ensure_dm_topic is None:
        raise RuntimeError(f"Telegram adapter cannot {verb} named private DM topics")
    thread_id = await ensure_dm_topic(chat_id, name, **({"force_create": True} if refresh else {}))
    if not thread_id:
        raise RuntimeError(f"Failed to {verb} Telegram private DM topic '{name}'")
    return str(thread_id)


class DeliveryRouter:
    """Resolves delivery targets and dispatches messages to platform adapters."""

    def __init__(self, config: GatewayConfig, adapters: Dict[Platform, Any] = None,
                 dead_targets: Optional[DeadTargetRegistry] = None):  # profile-local registry when omitted
        self.config = config
        self.adapters = adapters or {}
        self.output_dir = get_hermes_home() / "cron" / "output"
        self.dead_targets = dead_targets or DeadTargetRegistry()

    async def deliver(self, content: str, targets: List[DeliveryTarget], job_id: Optional[str] = None,
                      job_name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Deliver content to all targets; returns per-target results keyed by target string."""
        results = {}
        for target in targets:
            if target.unknown_platform is not None:
                results[target.to_string()] = {
                    "success": False, "error": f"unknown_platform: {target.unknown_platform}"}
                continue
            # Skip targets proven permanently unreachable (deleted group, blocked bot, deactivated user) —
            # re-sending each tick wastes flood-control budget. Self-healing: a later successful send
            # clears the flag. LOCAL/origin-without-chat targets are never dead-tracked.
            tracked = target.platform != Platform.LOCAL and target.chat_id
            if tracked and self.dead_targets.is_dead(target.platform.value, target.chat_id):
                logger.info("Skipping delivery to known-dead target %s:%s (send to it again to clear)",
                            target.platform.value, target.chat_id)
                results[target.to_string()] = {"success": False, "skipped": "dead_target",
                                               "error": "target previously confirmed unreachable"}
                continue
            try:
                if target.platform == Platform.LOCAL:
                    result = self._deliver_local(content, job_id, job_name, metadata)
                else:
                    result = await self._deliver_to_platform(target, content, metadata)
                    if target.chat_id and _send_result_error(result) is None:
                        self.dead_targets.clear(target.platform.value, target.chat_id)
                results[target.to_string()] = {"success": True, "result": result}
            except Exception as e:
                # Hard failures raise. Record a whole-chat death so future deliveries short-circuit.
                dead_kind = classify_dead_error(str(e)) if tracked else None
                if dead_kind:
                    self.dead_targets.mark_dead(target.platform.value, target.chat_id,
                                                reason=f"{dead_kind}: {str(e)[:120]}")
                results[target.to_string()] = {"success": False, "error": str(e)}
        return results

    def _deliver_local(self, content: str, job_id: Optional[str], job_name: Optional[str],
                       metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Save content to local files."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = self.output_dir / (job_id or "misc") / f"{timestamp}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {job_name}" if job_name else "# Delivery Output", "",
                 f"**Timestamp:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
        lines += [f"**Job ID:** {job_id}"] if job_id else []
        lines += [f"**{key}:** {value}" for key, value in (metadata or {}).items()] + ["", "---", "", content]
        output_path.write_text("\n".join(lines), encoding="utf-8")
        return {"path": str(output_path), "timestamp": timestamp}

    def _save_full_output(self, content: str, job_id: str) -> Path:
        """Save full cron output to disk and return the file path."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = get_hermes_home() / "cron" / "output" / f"{job_id}_{timestamp}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _save_delivery_fallback(
        self,
        content: str,
        target: "DeliveryTarget",
        job_id: Optional[str] = None,
    ) -> Path:
        """Save undelivered content to a deterministic fallback file.

        When platform delivery fails, this preserves the content at a
        well-known path so the user can retrieve it even when the primary
        messaging channel is unreachable.

        Fallback path: ``{HERMES_HOME}/cron/output/delivery_fallback/``
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback_dir = get_hermes_home() / "cron" / "output" / "delivery_fallback"
        fallback_dir.mkdir(parents=True, exist_ok=True)

        job_part = f"_{job_id}" if job_id else ""
        target_str = target.to_string().replace(":", "_")
        path = (
            fallback_dir
            / f"{target_str}{job_part}_{timestamp}.md"
        )

        formatted_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        path.write_text(
            f"# Delivery Fallback — {target.to_string()}\n\n"
            f"**Timestamp:** {formatted_time}\n"
            f"**Target:** {target.to_string()}\n"
            f"**Error:** Delivery failed — content preserved below\n\n"
            f"---\n\n{content}\n",
            encoding="utf-8",
        )
        return path

    def _filter_silence_narration_enabled(self) -> bool:
        """``HERMES_FILTER_SILENCE_NARRATION`` env overrides the ``gateway.filter_silence_narration`` flag."""
        env = os.getenv("HERMES_FILTER_SILENCE_NARRATION")
        return (bool(getattr(self.config, "filter_silence_narration", True)) if env is None
                else env.strip().lower() in ("1", "true", "yes", "on"))

    def _cap_oversized_output(self, adapter: Any, content: str, job_id: str) -> str:
        """Audit-save oversized cron output; truncate it for non-chunking adapters. Above MAX_PLATFORM_OUTPUT
        the full output is always written to disk as an audit trail, best-effort — a failed save (full disk,
        permissions) never blocks delivery. Non-chunking adapters then get the content truncated with a
        footer pointing to the saved file; ``splits_long_messages`` adapters receive the full payload."""
        if len(content) <= MAX_PLATFORM_OUTPUT:
            return content
        saved_path: Optional[Path] = None
        try:
            saved_path = self._save_full_output(content, job_id)
        except OSError as exc:
            logger.warning("Audit save failed for cron output (%d chars, job=%s): %s — "
                           "delivery proceeds without audit copy", len(content), job_id, exc)
        if getattr(adapter, "splits_long_messages", False):
            if saved_path:
                logger.info("Cron output preserved for chunking adapter (%d chars) — "
                            "full output saved to %s", len(content), saved_path)
            return content
        # The footer needs a valid path: if the best-effort save failed, retry
        # (a failure now is a real delivery problem and propagates).
        saved_path = saved_path or self._save_full_output(content, job_id)
        footer = f"\n\n... [truncated, full output saved to {saved_path}]"
        logger.info("Cron output truncated (%d chars) — full output: %s", len(content), saved_path)
        return content[:max(0, MAX_PLATFORM_OUTPUT - len(footer))] + footer

    @staticmethod
    def _long_reply_capable(adapter: Any) -> bool:
        """Whether this adapter can offer the split-or-file choice at all (capability, not platform)."""
        return callable(getattr(adapter, "send_document", None)) and callable(
            getattr(adapter, "send_choice_picker", None))

    async def _deliver_as_document(self, adapter: Any, chat_id: str, content: str,
                                   metadata: Optional[Dict[str, Any]], job_id: str) -> Dict[str, Any]:
        """Send an over-cap reply as one Markdown file instead of several messages."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmp_dir = tempfile.mkdtemp(prefix="hermes-report-")
        # The basename is what the reader sees in their chat, so keep it meaningful.
        path = Path(tmp_dir) / f"report-{stamp}.md"
        try:
            path.write_text(content, encoding="utf-8")
            caption = f"📄 Report as a file ({len(content):,} characters)."
            result = await adapter.send_document(
                chat_id=str(chat_id), file_path=str(path), caption=caption, metadata=metadata)
            if not getattr(result, "success", False):
                # Fall back to the normal split delivery rather than losing the report.
                logger.warning("Document delivery failed (job=%s): %s — falling back to split messages",
                               job_id, getattr(result, "error", None))
                return {}
            return {"success": True, "delivered": True, "as_document": True,
                    "message_id": getattr(result, "message_id", None)}
        except Exception as exc:
            logger.warning("Document delivery raised (job=%s): %s — falling back to split messages",
                           job_id, exc)
            return {}
        finally:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
                os.rmdir(tmp_dir)

    async def _offer_long_reply_choice(self, adapter: Any, platform: str, chat_id: str,
                                       metadata: Optional[Dict[str, Any]]) -> None:
        """Offer the one-time split-or-file choice, after the report itself has been delivered.

        Order matters: the report goes out first, unchanged. Holding a scheduled report until a human
        taps a button would trade a cosmetic question for a missed delivery, and an unattended chat
        would never receive it at all.
        """
        from gateway import long_reply_pref

        async def _on_choice(_chat_id: str, value: str) -> str:
            if long_reply_pref.set_preference(platform, str(chat_id), value):
                return ("📄 Long replies will arrive as a file from now on."
                        if value == long_reply_pref.DOCUMENT
                        else "✂️ Long replies will keep arriving as separate messages.")
            return "Could not save that preference."

        try:
            long_reply_pref.mark_asked(platform, str(chat_id))
            await adapter.send_choice_picker(
                str(chat_id),
                "That report did not fit one message. How would you like long replies delivered?",
                [{"value": long_reply_pref.SPLIT, "label": "✂️ Separate messages", "is_current": True},
                 {"value": long_reply_pref.DOCUMENT, "label": "📄 One file"}],
                f"long-reply-pref:{platform}:{chat_id}",
                _on_choice,
                metadata=metadata,
            )
        except Exception as exc:
            # A failed courtesy prompt must never affect the delivery that already succeeded.
            logger.debug("Long-reply choice prompt failed (chat=%s): %s", chat_id, exc)

    async def _deliver_to_platform(self, target: DeliveryTarget, content: str,
                                   metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Deliver content to a messaging platform."""
        transport = resolve_delivery_transport(target.platform, self.config, self.adapters)
        if transport is None:
            raise ValueError(f"No adapter configured for {target.platform.value}")
        if not target.chat_id:
            raise ValueError(f"No chat ID for {target.platform.value} delivery")
        adapter = transport.adapter
        # Whether the reply fit ONE message is a property of what the agent produced, so it is read
        # before _cap_oversized_output, which truncates for adapters that cannot split. The document
        # path also needs the untruncated text — writing the capped version to a file would ship the
        # very truncation the file exists to avoid.
        full_content = content
        oversized_reply = len(content) > MAX_PLATFORM_OUTPUT
        content = self._cap_oversized_output(adapter, content, (metadata or {}).get("job_id", "unknown"))

        # Substrate-level anti-loop guard: drop hallucinated "silence narration" (*(silent)*, 🔇, a bare ".")
        # before it reaches any adapter — in bot-to-bot channels these mirror back and forth until a model
        # crashes with "no content after all retries"; prompt rules drift across providers, so this single
        # chokepoint covers every platform. Local/file delivery is never filtered (saved silence has no loop
        # risk). Cron output is an ARTIFACT, not model chatter: a legitimately terse job ("...", a single 🔇)
        # has no mirror loop, and dropping it while returning success is how a cron gets logged as delivered
        # with nothing on the wire. Cron sends carry job_id in metadata; everything else is filtered.
        # See #77763.
        is_cron_artifact = "job_id" in (metadata or {})
        if self._filter_silence_narration_enabled() and not is_cron_artifact and _is_silence_narration(content):
            logger.warning("Dropped silence-narration outbound to %s (chat=%s): %r",
                           target.platform.value, target.chat_id, content[:40])
            return {"success": True, "filtered": "silence_narration", "delivered": False}

        send_metadata = dict(metadata or {})
        home = self.config.get_home_channel(target.platform) if transport.is_relay else None
        if home is not None and home.chat_id == target.chat_id:
            send_metadata.update({k: v for k, v in (("user_id", home.user_id), ("scope_id", home.scope_id)) if v})

        # Caller-supplied thread routing always wins over target.thread_id.
        named_topic: Optional[str] = None  # named Telegram private topic created for this send
        thread_id = target.thread_id
        if thread_id and not any(key in send_metadata for key in _THREAD_ROUTING_KEYS):
            send_metadata["thread_id"] = thread_id
            if target.platform == Platform.TELEGRAM and looks_like_telegram_private_chat_id(target.chat_id):
                if not _looks_like_int(thread_id):
                    # Named topic: create via createForumTopic, use message_thread_id directly.
                    named_topic = thread_id
                    send_metadata["thread_id"] = await _ensure_named_dm_topic(adapter, target.chat_id, thread_id, refresh=False)
                    send_metadata["telegram_dm_topic_created_for_send"] = True
                elif send_metadata.get("telegram_reply_to_message_id") is None:
                    # Legacy numeric private topic ids not created by this send path need a reply
                    # anchor to stay visible in the requested lane.
                    raise RuntimeError(
                        "Telegram private DM topic delivery requires telegram_reply_to_message_id; "
                        "send to the bare chat or provide a reply anchor"
                    )
                else:
                    send_metadata["telegram_dm_topic_reply_fallback"] = True

        # Over-cap reply: honour this chat's recorded choice, and offer it once if never asked.
        # The preference is the reader's, never ours (gateway/long_reply_pref.py).
        _oversized = oversized_reply and self._long_reply_capable(adapter)
        _platform_name = target.platform.value
        if _oversized:
            from gateway import long_reply_pref
            if long_reply_pref.get_preference(_platform_name, target.chat_id) == long_reply_pref.DOCUMENT:
                _doc = await self._deliver_as_document(
                    adapter, target.chat_id, full_content, send_metadata or None,
                    (metadata or {}).get("job_id", "unknown"))
                if _doc:
                    return _doc

        for retry in (False, True):
            result = await transport.send(target.platform, target.chat_id, content, metadata=send_metadata or None)
            error = _send_result_error(result)
            if retry or error is None or not named_topic or "thread not found" not in error.lower():
                break
            # The named topic vanished under us: recreate it once and resend.
            send_metadata["thread_id"] = await _ensure_named_dm_topic(adapter, target.chat_id, named_topic, refresh=True)
            send_metadata["telegram_dm_topic_created_for_send"] = True
        if error is not None:
            # Delivery to the platform failed — save the content to a
            # well-known fallback file so the user can still retrieve it
            # when the primary channel is unreachable.
            job_id = (metadata or {}).get("job_id")
            fallback_path = self._save_delivery_fallback(
                content=content,
                target=target,
                job_id=job_id,
            )
            logger.warning(
                "Delivery to %s failed — content saved to fallback file: %s",
                target.to_string(),
                fallback_path,
            )
            raise RuntimeError(
                error
                or f"{target.platform.value} delivery failed — "
                f"content preserved at {fallback_path}",
            )
        if _oversized:
            from gateway import long_reply_pref
            if not long_reply_pref.was_asked(_platform_name, target.chat_id):
                await self._offer_long_reply_choice(
                    adapter, _platform_name, target.chat_id, send_metadata or None)
        return result
