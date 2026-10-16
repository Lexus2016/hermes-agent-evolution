"""Serialized Telegram text delivery and certain-undelivered continuation accounting."""
from __future__ import annotations

from typing import Any, Optional

from gateway.platforms.base import SendResult


class TelegramSendMixin:
    async def _send_text_locked(
        self, chat_id: str, content: str, reply_to: Optional[str], metadata: Optional[dict[str, Any]]) -> SendResult:
        """``send()`` body under the per-chat lock: rich fast-path, else MarkdownV2 chunks."""
        from . import adapter
        # Re-checked under the lock: a burst queued behind a refused send must not each fire once.
        cooldown = self._send_cooldown_remaining(chat_id)
        if cooldown > 0:
            adapter.logger.warning(
                "[%s] Telegram flood control still active for chat %s (%.0fs left); refusing locally without an API call",
                self.name, chat_id, cooldown)
            return adapter._flood_cap_result(cooldown)
        # Shared per-chat budget (#116312): a send WAITS for its slot (a send that waits
        # is delivered; one that is skipped would drop a message).
        slot_remaining = self._chat_outbound_slot_remaining(chat_id)
        if slot_remaining > 0:
            adapter.logger.debug(
                "[%s] pacing send for chat %s (shared send+edit budget: slot in %.1fs)",
                self.name, chat_id, slot_remaining)
            await adapter.asyncio.sleep(slot_remaining)
        self._hold_chat_outbound_slot(chat_id)
        error_types = self._telegram_error_types()
        chunks: list[str] = []
        delivered: list[str] = []
        try:
            # Re-read the cooldown now that the queue has moved: a burst that all passed the gate
            # before queueing must not each fire a doomed request once the first one is refused.
            _queued_cooldown = self._send_cooldown_remaining(chat_id)
            if _queued_cooldown > 0:
                adapter.logger.debug("[%s] Send skipped after queueing: chat %s inside a %.1fs flood cooldown",
                             self.name, chat_id, _queued_cooldown)
                return adapter._flood_cap_result(round(_queued_cooldown, 1))
            # Bot API 10.1 rich fast-path; falls through to legacy MarkdownV2 on permanent/capability
            # errors or DM-topic skips; returns directly on success or transient failure (no legacy resend).
            _paced_slot = False
            if self._should_attempt_rich(content, metadata=metadata):
                await self._pace_send(chat_id)
                _paced_slot = True
                rich_result = await self._try_send_rich(chat_id, content, reply_to, metadata)
                if rich_result is not None:
                    if rich_result.success:
                        await self._retrigger_typing(chat_id, metadata)
                    elif rich_result.retry_after is not None:
                        # Rich refusals must arm the gate too, else the hottest content type
                        # (structured reports — 6% of final replies here, and the long ones) keeps
                        # probing an active penalty, which is what the gate exists to stop.
                        self._record_send_cooldown(chat_id, rich_result.retry_after)
                    return rich_result
            # Raw-text chunking (see _chunks_for_send): the boundaries stay content offsets so a
            # partially delivered split reports its undelivered tail. format_message escapes the
            # "(1/2)" indicator along with the chunk body.
            raw_chunks, chunks = self._chunks_for_send(content)
            result = await self._send_chunks(
                chat_id, chunks, delivered, reply_to, metadata, error_types,
                first_slot_reserved=_paced_slot)
            if not result.success and delivered:
                # Preserve fork plain-text offsets for ledger consumers alongside upstream's
                # already-formatted resume payload (formatting it again would double-escape).
                prefix, tail = self._split_at_chunk_boundary(content, raw_chunks, len(delivered))
                raw = result.raw_response if isinstance(result.raw_response, dict) else {}
                if "undelivered_chunks" in raw:
                    raw.update(delivered_prefix=prefix, undelivered_tail=tail)
            return result
        except Exception as e:
            classified = self._classify_send_exception(e, error_types)
            logger = adapter.logger
            logger.exception("Telegram send boundary failed", exc_info=(RuntimeError, RuntimeError(classified.error), None))
            return self._with_partial_send(classified, chunks[len(delivered):], delivered, tail_certain=classified.retryable)


    def _classify_send_exception(self, e: Exception, error_types: tuple) -> SendResult:
        """The failed ``SendResult`` for an exception escaping the chunk loop. ``retryable`` doubles as
        "non-delivery is certain": a plain ``TimedOut`` may have reached Telegram, so it is neither re-sent
        by ``_send_with_retry`` nor resumed from; a wrapped ConnectTimeout / httpx pool timeout never left."""
        from . import adapter
        safe_error = adapter._redact_telegram_error_text(e)
        adapter.logger.error("[%s] Failed to send Telegram message: %s", self.name, safe_error)
        err_str = str(e).lower()
        error_kind = adapter.classify_send_error(e)
        # Content exceeded 4096 chars: fail so the stream consumer enters fallback mode.
        if "message_too_long" in err_str or "too long" in err_str:
            adapter.logger.debug("[%s] send() content too long, falling back to new-message continuation", self.name)
            return SendResult(success=False, error="message_too_long", error_kind="too_long")
        _to = error_types[2]
        is_timeout = (_to and isinstance(e, _to)) or "timed out" in err_str
        return SendResult(
            success=False, error=safe_error,
            retryable=(self._looks_like_connect_timeout(e) or self._looks_like_pool_timeout(e) or not is_timeout),
            error_kind=error_kind)


    async def _send_chunks(
        self, chat_id: str, chunks: list[str], delivered: list[str], reply_to: Optional[str],
        metadata: Optional[dict[str, Any]], error_types: tuple, *, first_slot_reserved: bool = False) -> SendResult:
        """Deliver formatted ``chunks`` in order, appending each landed message id to ``delivered``
        (pre-seeded with the ids of an earlier partial send when resuming — the chunk index used for
        reply routing continues from there). A mid-loop refusal returns the ``partial_overflow`` result."""
        from . import adapter
        thread_id = self._metadata_thread_id(metadata)
        requested_thread_id = self._message_thread_id_for_send(thread_id)
        used_thread_fallback = False
        prior = len(delivered)
        for chunk in chunks:
            if len(delivered) != prior or not first_slot_reserved:
                await self._pace_send(chat_id)
            outcome = await self._send_chunk_with_retries(
                chat_id, chunk, len(delivered), reply_to, metadata, thread_id, used_thread_fallback, error_types)
            if isinstance(outcome, SendResult):
                # Every SendResult returned here is a DEFINITE non-delivery (flood cap, DM-topic refusal);
                # ambiguous timeouts raise instead, so the remainder is safe to resume from.
                return self._with_partial_send(outcome, chunks[len(delivered) - prior:], delivered)
            msg, used_thread_fallback = outcome
            delivered.append(str(msg.message_id))
        self._clear_send_cooldown(chat_id)
        await self._retrigger_typing(chat_id, metadata)
        return SendResult(
            success=True, message_id=delivered[0] if delivered else None,
            raw_response={
                "message_ids": list(delivered), "requested_thread_id": requested_thread_id, "thread_fallback": used_thread_fallback})


    @staticmethod
    def _with_partial_send(
        result: SendResult, undelivered: list[str], delivered: list[str], *, tail_certain: bool = True) -> SendResult:
        """Mark a split-send failure that happened after earlier chunks landed with the ``partial_overflow``
        contract (the same key ``_edit_overflow_split`` sets and the stream consumer reads), so no caller
        re-sends the already-visible head. ``undelivered`` (the formatted remainder, for
        :meth:`_resume_partial_send`) is attached only when ``tail_certain``. No-op when nothing landed."""
        from . import adapter
        if not delivered:
            return result
        raw = dict(result.raw_response) if isinstance(result.raw_response, dict) else {}
        raw.update({
            "partial_overflow": True, "delivered_chunks": len(delivered), "total_chunks": len(delivered) + len(undelivered),
            "last_message_id": delivered[-1], "continuation_message_ids": tuple(delivered[1:])})
        if tail_certain and undelivered:
            raw["undelivered_chunks"] = tuple(undelivered)
            raw["delivered_message_ids"] = tuple(delivered)
        result.raw_response = raw
        result.message_id = delivered[-1]
        result.continuation_message_ids = tuple(delivered[1:])
        return result


    async def _resume_partial_send(
        self, chat_id: str, result: SendResult, *, reply_to: Optional[str], metadata: Optional[dict[str, Any]]) -> Optional[SendResult]:
        """Send only the chunks a partial ``send()`` could not deliver (``raw_response["undelivered_chunks"]``),
        continuing the message-id sequence; ``None`` when the tail was not certain-undelivered."""
        from . import adapter
        raw = result.raw_response if isinstance(result.raw_response, dict) else {}
        undelivered = raw.get("undelivered_chunks")
        if not undelivered or not self._bot:
            return None
        delivered = list(raw.get("delivered_message_ids") or ())
        prior = len(delivered)
        async with self._chat_send_guard(chat_id):
            cooldown = self._send_cooldown_remaining(chat_id)
            if cooldown > 0:
                return self._with_partial_send(adapter._flood_cap_result(cooldown), list(undelivered), delivered)
            error_types = self._telegram_error_types()
            try:
                return await self._send_chunks(chat_id, list(undelivered), delivered, reply_to, metadata, error_types)
            except Exception as e:
                classified = self._classify_send_exception(e, error_types)
                logger = adapter.logger
                logger.exception("Telegram resume boundary failed", exc_info=(RuntimeError, RuntimeError(classified.error), None))
                return self._with_partial_send(
                    classified, list(undelivered)[len(delivered) - prior:], delivered, tail_certain=classified.retryable)
