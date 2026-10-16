"""Watch-pattern event delivery and rate limiting for background processes."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession


class ProcessWatchMixin:
    """The registry supplies the completion queue and global watch-breaker state.

    Resolve shared constants and helpers through the facade at call time so existing
    runtime overrides and monkeypatches retain their original lookup point.
    """

    def _check_watch_patterns(self, session: "ProcessSession", new_text: str) -> None:
        """Scan a freshly-read chunk for watch patterns and queue notifications.
        Per-session rate limiting (see WATCH_* constants): one match per cooldown
        window, a match inside the window is one strike, WATCH_STRIKE_LIMIT consecutive
        strikes or WATCH_LIFETIME_MAX_HITS total deliveries disable watching and
        promote the session to notify_on_complete."""
        from tools import process_registry as pr

        if not session.watch_patterns or session._watch_disabled:
            return
        # Late chunks after the reader declared exit are post-exit noise; dropping them
        # avoids stale notifications minutes after the process ended.
        if session.exited:
            return
        hits = [  # (first matching pattern, line) — one match per line
            (next(p for p in session.watch_patterns if p in line), line.rstrip())
            for line in new_text.splitlines() if any(p in line for p in session.watch_patterns)]
        if not hits:
            return
        matched_pattern = hits[0][0]
        matched_lines = [line for _, line in hits]
        now = pr.time.time()
        with session._lock:
            if session._watch_cooldown_until and now < session._watch_cooldown_until:
                # Inside the cooldown: drop, count one strike per window, disable +
                # promote once the strike limit is hit.
                session._watch_suppressed += len(matched_lines)
                if session._watch_strike_candidate:
                    return
                session._watch_strike_candidate = True
                session._watch_consecutive_strikes += 1
                if session._watch_consecutive_strikes < pr.WATCH_STRIKE_LIMIT:
                    return
                session._watch_disabled = True
                # Promote so the agent still gets exactly one notification on exit,
                # plus exactly one summary so it sees why things went quiet.
                session.notify_on_complete = True
                self._emit_watch_disabled(
                    session, session._watch_suppressed,
                    f"{pr.WATCH_STRIKE_LIMIT} consecutive rate-limit windows triggered "
                    f"(min spacing {pr.WATCH_MIN_INTERVAL_SECONDS}s). ")
                return
            # Cooldown expired. A prior window with no drops resets the
            # consecutive-strike counter (healthy cadence again).
            if session._watch_cooldown_until and not session._watch_strike_candidate:
                session._watch_consecutive_strikes = 0
            session._watch_strike_candidate = False
            # Emit and start a new cooldown window.
            session._watch_cooldown_until = now + pr.WATCH_MIN_INTERVAL_SECONDS
            session._watch_hits += 1
            suppressed = session._watch_suppressed
            session._watch_suppressed = 0
            # Lifetime cap: this match is still delivered, but no further ones.
            lifetime_exhausted = session._watch_hits >= pr.WATCH_LIFETIME_MAX_HITS
            if lifetime_exhausted:
                session._watch_disabled = True
                session.notify_on_complete = True
        output = "\n".join(matched_lines[:20])
        if len(output) > 2000:
            output = output[:2000] + "\n...(truncated)"
        if self._global_watch_admit(now):
            notification = {
                **self._watch_event_base(session),
                "type": "watch_match",
                "pattern": matched_pattern,
                "output": output,
                "suppressed": suppressed,
            }
            pr._redact_process_result(notification)
            self.completion_queue.put(notification)
        # Even when the breaker drops the final match, still explain the silence.
        if lifetime_exhausted:
            self._emit_watch_disabled(
                session, 0, f"reached the lifetime cap of {pr.WATCH_LIFETIME_MAX_HITS} delivered matches. ",
            )

    def _emit_watch_disabled(self, session: "ProcessSession", suppressed: int, why: str) -> None:
        """Queue the one-shot watch_disabled summary (strike-limit or lifetime-cap path)."""
        self.completion_queue.put({
            **self._watch_event_base(session),
            "type": "watch_disabled",
            "suppressed": suppressed,
            "message": (
                f"Watch patterns disabled for process {session.id} — {why}"
                f"Falling back to notify_on_complete semantics; you'll get "
                f"exactly one notification when the process exits."),
        })

    @staticmethod
    def _watch_event_base(session: "ProcessSession") -> dict:
        """Session identity + watcher routing fields shared by every watch event."""
        from tools import process_registry as pr

        return {
            "event_id": pr.uuid.uuid4().hex,
            "started_at": session.started_at,
            "parent_session_id": session.parent_session_id,
            "session_id": session.id,
            "session_key": session.session_key,
            "task_id": session.task_id,
            "owner_task_id": session.owner_task_id,
            "command": session.command,
            **{key: getattr(session, f"watcher_{key}") for key in pr._WATCHER_ROUTE_KEYS},
        }

    @staticmethod
    def _global_watch_event(type_: str, message: str, **extra) -> dict:
        """Unaddressed (all-sessions) watch breaker event."""
        return {
            "session_id": "", "session_key": "", "command": "", "type": type_, **extra,
            "message": message,
            "platform": "", "chat_id": "", "user_id": "", "user_name": "", "thread_id": "",
        }

    def _global_watch_admit(self, now: float) -> bool:
        """True if this watch_match may pass the global breaker.
        In cooldown: drop and count. Otherwise slide the rolling window; exceeding
        the cap trips the breaker for WATCH_GLOBAL_COOLDOWN_SECONDS with ONE
        "tripped" summary, and the cooldown's end emits ONE "released" summary."""
        from tools import process_registry as pr

        events = []  # summary events, queued outside the lock
        with self._global_watch_lock:
            # Handle cooldown expiry first so we can emit the release summary.
            if self._global_watch_tripped_until and now >= self._global_watch_tripped_until:
                suppressed = self._global_watch_suppressed_during_trip
                self._global_watch_tripped_until = 0.0
                self._global_watch_suppressed_during_trip = 0
                self._global_watch_window_start, self._global_watch_window_hits = now, 0
                if suppressed > 0:
                    events.append(self._global_watch_event(
                        "watch_overflow_released",
                        f"Watch-pattern notifications resumed. "
                        f"{suppressed} match event(s) were suppressed during the flood.",
                        suppressed=suppressed))
            if self._global_watch_tripped_until and now < self._global_watch_tripped_until:
                # Still in cooldown — drop and count.
                self._global_watch_suppressed_during_trip += 1
                admit = False
            else:
                if now - self._global_watch_window_start >= pr.WATCH_GLOBAL_WINDOW_SECONDS:
                    self._global_watch_window_start, self._global_watch_window_hits = now, 0
                admit = self._global_watch_window_hits < pr.WATCH_GLOBAL_MAX_PER_WINDOW
                if admit:
                    self._global_watch_window_hits += 1
                else:
                    self._global_watch_tripped_until = now + pr.WATCH_GLOBAL_COOLDOWN_SECONDS
                    self._global_watch_suppressed_during_trip += 1
                    events.append(self._global_watch_event(
                        "watch_overflow_tripped",
                        f"Watch-pattern overflow: >{pr.WATCH_GLOBAL_MAX_PER_WINDOW} "
                        f"notifications in {pr.WATCH_GLOBAL_WINDOW_SECONDS}s across all processes. "
                        f"Suppressing further watch_match events for "
                        f"{pr.WATCH_GLOBAL_COOLDOWN_SECONDS}s."))
        for msg in events:
            self.completion_queue.put(msg)
        return admit
