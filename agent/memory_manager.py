"""MemoryManager — orchestrates memory providers for the agent.

Single integration point in run_agent.py. Replaces scattered per-backend
code with one manager that delegates to registered providers.

Only ONE external plugin provider is allowed at a time — attempting to
register a second external provider is rejected with a warning.  This
prevents tool schema bloat and conflicting memory backends.

Usage in run_agent.py:
    self._memory_manager = MemoryManager()
    # Only ONE of these:
    self._memory_manager.add_provider(plugin_provider)

    # System prompt
    prompt_parts.append(self._memory_manager.build_system_prompt())

    # Pre-turn
    context = self._memory_manager.prefetch_all(user_message)

    # Post-turn
    self._memory_manager.sync_all(user_msg, assistant_response)
    self._memory_manager.queue_prefetch_all(user_msg)
"""

from __future__ import annotations

import json
import logging
import re
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from agent.memory_importance import EpisodicMemoryStore, MemoryEvent, score_importance
from agent.memory_provider import MemoryProvider
from agent.skill_commands import extract_user_instruction_from_skill_message
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# How long shutdown_all() waits for in-flight background sync/prefetch work
# to drain before abandoning it. A wedged provider must never block process
# teardown indefinitely — the worker threads are daemon, so anything still
# running past this window dies with the interpreter.
_SYNC_DRAIN_TIMEOUT_S = 5.0


def normalize_tool_schema(schema: Any) -> Optional[Dict[str, Any]]:
    """Return a function-tool dict with a resolvable top-level ``name``.

    Context engines and memory providers expose tool schemas via
    ``get_tool_schemas()``. The expected shape is a bare function schema
    (``{"name": ..., "description": ..., "parameters": ...}``) which callers
    wrap as ``{"type": "function", "function": schema}``.

    Some providers instead return an entry that is *already* in OpenAI tool
    form (``{"type": "function", "function": {"name": ...}}``). Wrapping that
    a second time produces ``{"type": "function", "function": {"type":
    "function", "function": {...}}}`` whose ``function`` has no top-level
    ``name``. Strict providers (e.g. DeepSeek) reject the *entire* request
    with ``tools[N].function: missing field name`` (HTTP 400), so one bad
    schema disables the whole toolset and breaks every turn (#47707).

    This helper normalizes both shapes to the bare function schema and
    returns ``None`` for anything without a resolvable name, so callers can
    skip-with-warning rather than appending a nameless tool.
    """
    if not isinstance(schema, dict):
        return None
    # Unwrap an already-wrapped OpenAI tool entry.
    if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
        schema = schema["function"]
        if not isinstance(schema, dict):
            return None
    name = schema.get("name", "")
    if not name or not isinstance(name, str):
        return None
    return schema


def memory_provider_tools_enabled(enabled_toolsets: Optional[List[str]]) -> bool:
    """Return whether external memory-provider tools should be exposed."""
    if enabled_toolsets is None:
        return True
    if not enabled_toolsets:
        return False
    if "memory" in enabled_toolsets:
        return True

    try:
        from toolsets import resolve_toolset

        return any("memory" in resolve_toolset(name) for name in enabled_toolsets)
    except Exception:
        logger.debug("Failed to resolve enabled toolsets for memory-provider tools", exc_info=True)
        return False


def inject_memory_provider_tools(agent: Any) -> int:
    """Append external memory-provider tool schemas to an agent tool surface."""
    memory_manager = getattr(agent, "_memory_manager", None)
    tools = getattr(agent, "tools", None)
    if not memory_manager or tools is None:
        return 0

    existing_tool_names = {
        tool.get("function", {}).get("name")
        for tool in tools
        if isinstance(tool, dict)
    }
    if (
        "memory" not in existing_tool_names
        and not memory_provider_tools_enabled(getattr(agent, "enabled_toolsets", None))
    ):
        return 0

    get_schemas = getattr(memory_manager, "get_all_tool_schemas", None)
    if not callable(get_schemas):
        return 0

    valid_tool_names = getattr(agent, "valid_tool_names", None)
    if valid_tool_names is None:
        valid_tool_names = set()
        agent.valid_tool_names = valid_tool_names

    added = 0
    for raw_schema in get_schemas():
        schema = normalize_tool_schema(raw_schema)
        if schema is None:
            logger.warning(
                "Memory provider returned a tool schema with no resolvable "
                "name; skipping to avoid poisoning the request (%r)",
                raw_schema,
            )
            continue
        tool_name = schema["name"]
        if tool_name in existing_tool_names:
            continue
        tools.append({"type": "function", "function": schema})
        valid_tool_names.add(tool_name)
        existing_tool_names.add(tool_name)
        added += 1

    return added


# ---------------------------------------------------------------------------
# Context fencing helpers
# ---------------------------------------------------------------------------

_FENCE_TAG_RE = re.compile(r'</?\s*memory-context\s*>', re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r'<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>',
    re.IGNORECASE,
)
_INTERNAL_NOTE_RE = re.compile(
    r'\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*',
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes from provider output."""
    text = _INTERNAL_CONTEXT_RE.sub('', text)
    text = _INTERNAL_NOTE_RE.sub('', text)
    text = _FENCE_TAG_RE.sub('', text)
    return text


class StreamingContextScrubber:
    """Stateful scrubber for streaming text that may contain split memory-context spans.

    The one-shot ``sanitize_context`` regex cannot survive chunk boundaries:
    a ``<memory-context>`` opened in one delta and closed in a later delta
    leaks its payload to the UI because the non-greedy block regex needs
    both tags in one string.  This scrubber runs a small state machine
    across deltas, holding back partial-tag tails and discarding
    everything inside a span (including the system-note line).

    Usage::

        scrubber = StreamingContextScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        trailing = scrubber.flush()  # at end of stream
        if trailing:
            emit(trailing)

    The scrubber is re-entrant per agent instance.  Callers building new
    top-level responses (new turn) should create a fresh scrubber or call
    ``reset()``.
    """

    _OPEN_TAG = "<memory-context>"
    _CLOSE_TAG = "</memory-context>"

    def __init__(self) -> None:
        self._in_span: bool = False
        self._buf: str = ""
        self._at_block_boundary: bool = True

    def reset(self) -> None:
        self._in_span = False
        self._buf = ""
        self._at_block_boundary = True

    def feed(self, text: str) -> str:
        """Return the visible portion of ``text`` after scrubbing.

        Any trailing fragment that could be the start of an open/close tag
        is held back in the internal buffer and surfaced on the next
        ``feed()`` call or discarded/emitted by ``flush()``.
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                idx = buf.lower().find(self._CLOSE_TAG)
                if idx == -1:
                    # Hold back a potential partial close tag; drop the rest
                    held = self._max_partial_suffix(buf, self._CLOSE_TAG)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                # Found close — skip span content + tag, continue
                buf = buf[idx + len(self._CLOSE_TAG):]
                self._in_span = False
            else:
                idx = self._find_boundary_open_tag(buf)
                if idx == -1:
                    # No open tag — hold back a potential partial open tag
                    held = (
                        self._max_pending_open_suffix(buf)
                        or self._max_partial_suffix(buf, self._OPEN_TAG)
                    )
                    if held:
                        self._append_visible(out, buf[:-held])
                        self._buf = buf[-held:]
                    else:
                        self._append_visible(out, buf)
                    return "".join(out)
                # Emit text before the tag, enter span
                if idx > 0:
                    self._append_visible(out, buf[:idx])
                buf = buf[idx + len(self._OPEN_TAG):]
                self._in_span = True

        return "".join(out)

    def flush(self) -> str:
        """Emit any held-back buffer at end-of-stream.

        If we're still inside an unterminated span the remaining content is
        discarded (safer: leaking partial memory context is worse than a
        truncated answer).  Otherwise the held-back partial-tag tail is
        emitted verbatim (it turned out not to be a real tag).
        """
        if self._in_span:
            self._buf = ""
            self._in_span = False
            return ""
        tail = self._buf
        self._buf = ""
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        """Return the length of the longest buf-suffix that is a tag-prefix.

        Case-insensitive.  Returns 0 if no suffix could start the tag.
        """
        tag_lower = tag.lower()
        buf_lower = buf.lower()
        max_check = min(len(buf_lower), len(tag_lower) - 1)
        for i in range(max_check, 0, -1):
            if tag_lower.startswith(buf_lower[-i:]):
                return i
        return 0

    def _find_boundary_open_tag(self, buf: str) -> int:
        """Find an opening fence only when it starts a block-like span."""
        buf_lower = buf.lower()
        search_start = 0
        while True:
            idx = buf_lower.find(self._OPEN_TAG, search_start)
            if idx == -1:
                return -1
            if self._is_block_boundary(buf, idx) and self._has_block_opener_suffix(buf, idx):
                return idx
            search_start = idx + 1

    def _max_pending_open_suffix(self, buf: str) -> int:
        """Hold a complete boundary tag until the following char confirms it."""
        if not buf.lower().endswith(self._OPEN_TAG):
            return 0
        idx = len(buf) - len(self._OPEN_TAG)
        if not self._is_block_boundary(buf, idx):
            return 0
        return len(self._OPEN_TAG)

    def _has_block_opener_suffix(self, buf: str, idx: int) -> bool:
        after_idx = idx + len(self._OPEN_TAG)
        if after_idx >= len(buf):
            return False
        return buf[after_idx] in "\r\n"

    def _is_block_boundary(self, buf: str, idx: int) -> bool:
        if idx == 0:
            return self._at_block_boundary
        preceding = buf[:idx]
        last_newline = preceding.rfind("\n")
        if last_newline == -1:
            return self._at_block_boundary and preceding.strip() == ""
        return preceding[last_newline + 1:].strip() == ""

    def _append_visible(self, out: list[str], text: str) -> None:
        if not text:
            return
        out.append(text)
        self._update_block_boundary(text)

    def _update_block_boundary(self, text: str) -> None:
        last_newline = text.rfind("\n")
        if last_newline != -1:
            self._at_block_boundary = text[last_newline + 1:].strip() == ""
        else:
            self._at_block_boundary = self._at_block_boundary and text.strip() == ""


def build_memory_context_block(raw_context: str) -> str:
    """Wrap prefetched memory in a fenced block with system note."""
    if not raw_context or not raw_context.strip():
        return ""
    clean = sanitize_context(raw_context)
    if clean != raw_context:
        logger.warning("memory provider returned pre-wrapped context; stripped")
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data — "
        "this is the agent's persistent memory and should inform all responses.]\n\n"
        f"{clean}\n"
        "</memory-context>"
    )


class MemoryManager:
    """Orchestrates the built-in provider plus at most one external provider.

    The builtin provider is always first. Only one non-builtin (external)
    provider is allowed.  Failures in one provider never block the other.
    """

    def __init__(self) -> None:
        self._providers: List[MemoryProvider] = []
        self._tool_to_provider: Dict[str, MemoryProvider] = {}
        self._has_external: bool = False  # True once a non-builtin provider is added
        # Background executor for end-of-turn sync/prefetch. Lazily created on
        # first use so the common builtin-only path spawns no extra threads.
        # A single worker serializes a provider's writes (turn N must land
        # before turn N+1) and caps thread growth at one per manager. See
        # _submit_background() and the sync_all/queue_prefetch_all rationale.
        self._sync_executor: Optional[ThreadPoolExecutor] = None
        self._sync_executor_lock = threading.Lock()
        # Episodic store for importance-weighted turn history (#752).
        # Populated by score_memories() during sync_all(). Kept on the
        # manager so the ``hermes memory score`` CLI subcommand and tests
        # can inspect/score it without re-deriving the events.
        self.episodic_store: EpisodicMemoryStore = EpisodicMemoryStore()

    # -- Registration --------------------------------------------------------

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a memory provider.

        Built-in provider (name ``"builtin"``) is always accepted.
        Only **one** external (non-builtin) provider is allowed — a second
        attempt is rejected with a warning.
        """
        is_builtin = provider.name == "builtin"

        if not is_builtin:
            if self._has_external:
                existing = next(
                    (p.name for p in self._providers if p.name != "builtin"), "unknown"
                )
                logger.warning(
                    "Rejected memory provider '%s' — external provider '%s' is "
                    "already registered. Only one external memory provider is "
                    "allowed at a time. Configure which one via memory.provider "
                    "in config.yaml.",
                    provider.name, existing,
                )
                return
            self._has_external = True

        self._providers.append(provider)

        # Core tool names are reserved — a memory provider must never register
        # a tool that shadows a built-in (e.g. ``clarify``, ``delegate_task``).
        # Built-ins always win, so such a tool is dropped at agent init and
        # would otherwise linger in ``_tool_to_provider`` and hijack dispatch
        # (#40466). Reject it here, at the door, so it never enters the routing
        # table at all — matching the built-ins-always-win invariant used by
        # the TTS/browser/search provider registries.
        from toolsets import _HERMES_CORE_TOOLS

        _core_tool_names = set(_HERMES_CORE_TOOLS)

        # Index tool names → provider for routing
        for raw_schema in provider.get_tool_schemas():
            schema = normalize_tool_schema(raw_schema)
            if schema is None:
                continue
            tool_name = schema["name"]
            if tool_name in _core_tool_names:
                logger.warning(
                    "Memory provider '%s' tool '%s' shadows a reserved core "
                    "tool name; registration ignored. Core tools always win — "
                    "rename the provider's tool to something unique.",
                    provider.name, tool_name,
                )
                continue
            if tool_name and tool_name not in self._tool_to_provider:
                self._tool_to_provider[tool_name] = provider
            elif tool_name in self._tool_to_provider:
                logger.warning(
                    "Memory tool name conflict: '%s' already registered by %s, "
                    "ignoring from %s",
                    tool_name,
                    self._tool_to_provider[tool_name].name,
                    provider.name,
                )

        logger.info(
            "Memory provider '%s' registered (%d tools)",
            provider.name,
            len(provider.get_tool_schemas()),
        )

    @property
    def providers(self) -> List[MemoryProvider]:
        """All registered providers in order."""
        return list(self._providers)

    def get_provider(self, name: str) -> Optional[MemoryProvider]:
        """Get a provider by name, or None if not registered."""
        for p in self._providers:
            if p.name == name:
                return p
        return None

    # -- System prompt -------------------------------------------------------

    def build_system_prompt(self) -> str:
        """Collect system prompt blocks from all providers.

        Returns combined text, or empty string if no providers contribute.
        Each non-empty block is labeled with the provider name.
        """
        blocks = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    blocks.append(block)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' system_prompt_block() failed: %s",
                    provider.name, e,
                )
        return "\n\n".join(blocks)

    # -- Prefetch / recall ---------------------------------------------------

    @staticmethod
    def _strip_skill_scaffolding(text: str) -> Optional[str]:
        """Return memory-worthy user text, or None to skip the turn.

        When a user invokes a /skill or /bundle, Hermes expands the turn into
        a model-facing message that embeds the entire skill body. Feeding that
        verbatim to memory providers pollutes their stores/embeddings with
        prompt scaffolding instead of what the user actually asked. We recover
        just the user's instruction here, once, for every provider — so this
        is fixed for the whole provider fan-out, not per backend.

        - Non-skill messages pass through unchanged.
        - Skill turns with a user instruction return that instruction.
        - Bare skill invocations (no instruction) return None → callers skip
          the turn, since there is no user content worth remembering.
        """
        return extract_user_instruction_from_skill_message(text)

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        """Collect prefetch context from all providers.

        Returns merged context text labeled by provider. Empty providers
        are skipped. Failures in one provider don't block others.
        """
        clean_query = self._strip_skill_scaffolding(query)
        if not clean_query:
            return ""
        parts = []
        for provider in self._providers:
            try:
                result = provider.prefetch(clean_query, session_id=session_id)
                if result and result.strip():
                    parts.append(result)
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' prefetch failed (non-fatal): %s",
                    provider.name, e,
                )
        return "\n\n".join(parts)

    def queue_prefetch_all(self, query: str, *, session_id: str = "") -> None:
        """Queue background prefetch on all providers for the next turn.

        Provider work is dispatched to a background worker so a slow or
        wedged provider can never block the caller. See ``sync_all`` for
        the full rationale (agent stuck "running" minutes after a turn).
        """
        providers = list(self._providers)
        if not providers:
            return

        clean_query = self._strip_skill_scaffolding(query)
        if not clean_query:
            return

        def _run() -> None:
            for provider in providers:
                try:
                    provider.queue_prefetch(clean_query, session_id=session_id)
                except Exception as e:
                    logger.debug(
                        "Memory provider '%s' queue_prefetch failed (non-fatal): %s",
                        provider.name, e,
                    )

        self._submit_background(_run)

    # -- Sync ----------------------------------------------------------------

    @staticmethod
    def _provider_sync_accepts_messages(provider: MemoryProvider) -> bool:
        """Return whether sync_turn accepts a messages keyword."""
        try:
            signature = inspect.signature(provider.sync_turn)
        except (TypeError, ValueError):
            return True
        params = list(signature.parameters.values())
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return True
        return "messages" in signature.parameters

    # -- Importance scoring (#752) -------------------------------------------

    @staticmethod
    def _friction_signals_from_turn(
        user_content: str,
        assistant_content: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> dict[str, int]:
        """Derive friction signals for a completed turn.

        Heuristic, deterministic, and side-effect free. Counts signals that
        the weighted importance model in ``agent.memory_importance`` knows
        how to score:

        - ``retries``: tool calls in the turn that raised an error (a
          retry loop surfaces as repeated error results).
        - ``task_failures``: a turn whose assistant output looks like a
          failure (apology + error-ish keywords) counts as one failure.
        - ``human_corrections``: a user message that reads as a correction
          ("no", "wrong", "actually", "instead") counts as one correction.
        - ``explicit_saves``: a memory tool write in the turn counts as an
          explicit save.

        Unknown/empty signals simply contribute zero — the scorer ignores
        them. This is intentionally cheap so it is safe to call on every
        turn-sync.
        """
        signals: dict[str, int] = {}
        if not user_content and not assistant_content:
            return signals

        # human_corrections: correction-like user phrasing.
        if user_content:
            u = user_content.lower()
            correction_markers = ("no,", "wrong", "actually", "instead", "not that", "redo")
            if any(m in u for m in correction_markers):
                signals["human_corrections"] = 1

        # task_failures: assistant output that looks like a failure.
        if assistant_content:
            a = assistant_content.lower()
            failure_markers = ("sorry", "i can't", "i cannot", "failed", "error", "unable to")
            if any(m in a for m in failure_markers):
                signals["task_failures"] = 1

        # retries / explicit_saves: inspect tool calls in the message list.
        if messages:
            errors = 0
            saves = 0
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role == "assistant" and isinstance(msg.get("tool_calls"), list):
                    for tc in msg["tool_calls"]:
                        if not isinstance(tc, dict):
                            continue
                        fn = (tc.get("function") or {}).get("name", "") if isinstance(
                            tc.get("function"), dict
                        ) else ""
                        if fn == "memory":
                            saves += 1
                if role == "tool":
                    content = msg.get("content")
                    if isinstance(content, str) and (
                        "error" in content.lower() or "failed" in content.lower()
                    ):
                        errors += 1
            if errors:
                signals["retries"] = errors
            if saves:
                signals["explicit_saves"] = saves

        return signals

    def score_memories(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> MemoryEvent:
        """Score a completed turn and record it as an episodic memory event.

        This is the real consumer of ``agent.memory_importance``: it derives
        friction signals from the turn, scores them with
        :func:`score_importance`, wraps the result in a :class:`MemoryEvent`,
        and adds it to :attr:`episodic_store`. Called from :meth:`sync_all`
        on the turn-sync path and from the ``hermes memory score`` CLI
        subcommand.

        The returned event carries the raw (pre-decay) importance in its
        ``importance`` field; callers can compute a decayed score on demand
        via the store.
        """
        signals = self._friction_signals_from_turn(
            user_content, assistant_content, messages
        )
        importance = score_importance(signals)
        event = MemoryEvent(
            what=user_content[:500] if user_content else "(empty turn)",
            outcome=assistant_content[:500] if assistant_content else "",
            importance=importance,
            friction_signals=signals,
            category="turn",
            tags=[session_id] if session_id else [],
            metadata={"session_id": session_id} if session_id else {},
        )
        self.episodic_store.add(event)
        logger.debug(
            "score_memories: recorded turn (importance=%.3f, signals=%s)",
            importance,
            signals,
        )
        return event

    def sync_all(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Sync a completed turn to all providers.

        Runs on a background worker thread, NOT inline on the
        turn-completion path. A provider's ``sync_turn`` may make a
        blocking network/daemon call (a misconfigured Hindsight daemon
        was observed blocking ~298s before failing); doing that inline
        held ``run_conversation`` open long after the user saw their
        response, so every interface (CLI, TUI, gateway) kept the agent
        marked "running" for minutes and any follow-up message triggered
        an aggressive interrupt. Dispatching off-thread means a slow or
        broken provider can never stall the turn — the sync simply
        completes (or fails, logged) in the background.

        Writes are serialized through a single worker so turn N lands
        before turn N+1; provider implementations don't need their own
        ordering guarantees.
        """
        clean_user_content = self._strip_skill_scaffolding(user_content)
        if not clean_user_content:
            return
        user_content = clean_user_content

        # Score this turn's friction signals and record it as an episodic
        # memory event (#752). This is the real consumer of
        # agent.memory_importance — every synced turn is scored so the
        # episodic store accumulates importance-weighted history. Scoring
        # is synchronous and cheap (no network), so it runs inline before
        # the provider guard; this keeps the store populated even in
        # built-in-only mode without spawning the background executor.
        try:
            self.score_memories(
                user_content,
                assistant_content,
                session_id=session_id,
                messages=messages,
            )
        except Exception as e:
            logger.debug(
                "score_memories() failed during sync (non-fatal): %s", e
            )

        providers = list(self._providers)
        if not providers:
            return

        def _run() -> None:
            for provider in providers:
                try:
                    if messages is not None and self._provider_sync_accepts_messages(provider):
                        provider.sync_turn(
                            user_content,
                            assistant_content,
                            session_id=session_id,
                            messages=messages,
                        )
                    else:
                        provider.sync_turn(
                            user_content,
                            assistant_content,
                            session_id=session_id,
                        )
                except Exception as e:
                    logger.warning(
                        "Memory provider '%s' sync_turn failed: %s",
                        provider.name, e,
                    )

        self._submit_background(_run)

    # -- Background dispatch -------------------------------------------------

    def _submit_background(self, fn) -> None:
        """Run ``fn`` on the manager's background worker.

        The executor is created lazily and shared across calls. If the
        executor can't be created or has already been shut down, ``fn``
        runs inline as a last-resort fallback — losing the async benefit
        but never losing the write itself. ``fn`` must do its own
        per-provider error handling; this wrapper only guards executor
        plumbing.
        """
        executor = self._get_sync_executor()
        if executor is None:
            # Executor unavailable (shut down / creation failed) — run
            # inline rather than drop the work. Slow, but correct.
            try:
                fn()
            except Exception as e:  # pragma: no cover - fn guards internally
                logger.debug("Inline memory background task failed: %s", e)
            return
        try:
            executor.submit(fn)
        except RuntimeError:
            # Executor was shut down between the get and the submit
            # (teardown race). Fall back to inline.
            try:
                fn()
            except Exception as e:  # pragma: no cover - fn guards internally
                logger.debug("Inline memory background task failed: %s", e)

    def _get_sync_executor(self) -> Optional[ThreadPoolExecutor]:
        """Lazily create the single-worker background executor."""
        if self._sync_executor is not None:
            return self._sync_executor
        with self._sync_executor_lock:
            if self._sync_executor is None:
                try:
                    # Daemon workers (see tools.daemon_pool): a provider wedged
                    # on a network call must never block interpreter exit —
                    # stdlib ThreadPoolExecutor's atexit hook would join it
                    # unconditionally even after shutdown(wait=False).
                    from tools.daemon_pool import DaemonThreadPoolExecutor
                    self._sync_executor = DaemonThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="mem-sync",
                    )
                except Exception as e:  # pragma: no cover - resource exhaustion
                    logger.warning("Failed to create memory sync executor: %s", e)
                    return None
            return self._sync_executor

    def flush_pending(self, timeout: Optional[float] = None) -> bool:
        """Block until queued sync/prefetch work has drained.

        Single-worker executor means submitting a sentinel and waiting on
        it guarantees every previously-submitted task has run. Returns
        True if the barrier completed within ``timeout`` (or no executor
        exists), False on timeout. Used at real session boundaries and by
        tests that need to assert provider state deterministically.
        """
        executor = self._sync_executor
        if executor is None:
            return True
        try:
            fut = executor.submit(lambda: None)
        except RuntimeError:
            # Executor already shut down — nothing pending.
            return True
        try:
            fut.result(timeout=timeout)
            return True
        except Exception:
            return False

    # -- Tools ---------------------------------------------------------------

    def get_all_tool_schemas(self) -> List[Dict[str, Any]]:
        """Collect tool schemas from all providers.

        Reserved core tool names (``clarify``, ``delegate_task``, etc.) are
        skipped — they are rejected from the routing table in
        :meth:`add_provider`, so the manager must not advertise a schema it
        will never route. Built-ins always win (#40466).
        """
        from toolsets import _HERMES_CORE_TOOLS

        _core_tool_names = set(_HERMES_CORE_TOOLS)
        schemas = []
        seen = set()
        for provider in self._providers:
            try:
                for raw_schema in provider.get_tool_schemas():
                    schema = normalize_tool_schema(raw_schema)
                    if schema is None:
                        logger.warning(
                            "Memory provider '%s' returned a tool schema with "
                            "no resolvable name; skipping (%r)",
                            provider.name, raw_schema,
                        )
                        continue
                    name = schema["name"]
                    if name in _core_tool_names:
                        continue
                    if name not in seen:
                        schemas.append(schema)
                        seen.add(name)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' get_tool_schemas() failed: %s",
                    provider.name, e,
                )
        return schemas

    def get_all_tool_names(self) -> set:
        """Return set of all tool names across all providers."""
        return set(self._tool_to_provider.keys())

    def has_tool(self, tool_name: str) -> bool:
        """Check if any provider handles this tool."""
        return tool_name in self._tool_to_provider

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs
    ) -> str:
        """Route a tool call to the correct provider.

        Returns JSON string result. Raises ValueError if no provider
        handles the tool.
        """
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return tool_error(f"No memory provider handles tool '{tool_name}'")
        try:
            return provider.handle_tool_call(tool_name, args, **kwargs)
        except Exception as e:
            logger.error(
                "Memory provider '%s' handle_tool_call(%s) failed: %s",
                provider.name, tool_name, e,
            )
            return tool_error(f"Memory tool '{tool_name}' failed: {e}")

    # -- Lifecycle hooks -----------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Notify all providers of a new turn.

        kwargs may include: remaining_tokens, model, platform, tool_count.
        """
        for provider in self._providers:
            try:
                provider.on_turn_start(turn_number, message, **kwargs)
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_turn_start failed: %s",
                    provider.name, e,
                )

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Notify all providers of session end."""
        for provider in self._providers:
            try:
                provider.on_session_end(messages)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' on_session_end failed: %s",
                    provider.name, e,
                    exc_info=True,
                )

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Notify all providers that the agent's session_id has rotated.

        Fires on ``/resume``, ``/branch``, ``/reset``, ``/new``, and
        context compression — any path that reassigns
        ``AIAgent.session_id`` without tearing the provider down.

        Providers keep running; they only need to refresh cached
        per-session state so subsequent writes land in the correct
        session's record. See ``MemoryProvider.on_session_switch`` for
        the full contract.

        ``rewound=True`` signals that session_id is unchanged but the
        transcript was truncated; providers caching per-turn document
        state should invalidate.
        """
        if not new_session_id:
            return
        # Only forward ``rewound`` when it's actually set. Passing it
        # unconditionally would inject ``rewound=False`` into every
        # provider's **kwargs for the common /resume, /branch, /new, and
        # compression paths, polluting providers that capture extra kwargs
        # (and breaking exact-dict assertions). The /undo path sets
        # rewound=True explicitly; everyone else stays clean.
        if rewound:
            kwargs["rewound"] = True
        for provider in self._providers:
            try:
                provider.on_session_switch(
                    new_session_id,
                    parent_session_id=parent_session_id,
                    reset=reset,
                    **kwargs,
                )
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_session_switch failed: %s",
                    provider.name, e,
                )

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Notify all providers before context compression.

        Returns combined text from providers to include in the compression
        summary prompt. Empty string if no provider contributes.
        """
        parts = []
        for provider in self._providers:
            try:
                result = provider.on_pre_compress(messages)
                if result and result.strip():
                    parts.append(result)
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_pre_compress failed: %s",
                    provider.name, e,
                )
        return "\n\n".join(parts)

    @staticmethod
    def _provider_memory_write_metadata_mode(provider: MemoryProvider) -> str:
        """Return how to pass metadata to a provider's memory-write hook."""
        try:
            signature = inspect.signature(provider.on_memory_write)
        except (TypeError, ValueError):
            return "keyword"

        params = list(signature.parameters.values())
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return "keyword"
        if "metadata" in signature.parameters:
            return "keyword"

        accepted = [
            p for p in params
            if p.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        ]
        if len(accepted) >= 4:
            return "positional"
        return "legacy"

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Notify external providers when the built-in memory tool writes.

        Skips the builtin provider itself (it's the source of the write).
        """
        for provider in self._providers:
            if provider.name == "builtin":
                continue
            try:
                metadata_mode = self._provider_memory_write_metadata_mode(provider)
                if metadata_mode == "keyword":
                    provider.on_memory_write(
                        action, target, content, metadata=dict(metadata or {})
                    )
                elif metadata_mode == "positional":
                    provider.on_memory_write(action, target, content, dict(metadata or {}))
                else:
                    provider.on_memory_write(action, target, content)
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_memory_write failed: %s",
                    provider.name, e,
                )

    # Actions the bridge mirrors to external providers. The built-in memory
    # tool can also return non-mutating shapes (errors, staged-for-approval
    # records); those are filtered out by ``notify_memory_tool_write`` before
    # we ever reach a provider.
    _MIRRORED_MEMORY_ACTIONS = {"add", "replace", "remove"}

    @staticmethod
    def _memory_tool_result_succeeded(result: Any) -> bool:
        """True only when the built-in memory tool actually committed a write.

        Fails closed: a string that isn't JSON, a non-dict result, a missing
        ``success``, or a write staged for approval (``staged is True``) all
        return False so external providers are never told about a write that
        did not land.
        """
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                return False
        if not isinstance(result, dict):
            return False
        return result.get("success") is True and result.get("staged") is not True

    def notify_memory_tool_write(
        self,
        tool_result: Any,
        tool_args: Dict[str, Any],
        *,
        build_metadata: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        """Mirror a built-in memory tool call to external providers.

        This is the single entry point the agent loop calls after running the
        built-in ``memory`` tool. All the decisions about *whether* and *what*
        to mirror live here, behind the manager interface — the loop only hands
        over the raw tool result and args:

        * gate on a committed (non-staged, successful) write,
        * expand the single-op and batched (``operations``) shapes,
        * keep only mutating actions (add/replace/remove),
        * build per-op provenance metadata and forward ``old_text``.

        ``build_metadata`` is an optional agent-side callable (the loop knows
        session/task/tool-call provenance the manager does not) invoked once per
        mirrored op.
        """
        if not self._memory_tool_result_succeeded(tool_result):
            return

        target = str(tool_args.get("target") or "memory")
        operations = tool_args.get("operations")
        if isinstance(operations, list) and operations:
            raw_operations = operations
        else:
            raw_operations = [{
                "action": tool_args.get("action"),
                "content": tool_args.get("content"),
                "old_text": tool_args.get("old_text"),
            }]

        for op in raw_operations:
            if not isinstance(op, dict):
                continue
            action = str(op.get("action") or "")
            if action not in self._MIRRORED_MEMORY_ACTIONS:
                continue
            try:
                metadata = dict(build_metadata() if build_metadata else {})
                old_text = op.get("old_text")
                if old_text:
                    metadata["old_text"] = str(old_text)
                self.on_memory_write(
                    action,
                    target,
                    str(op.get("content") or ""),
                    metadata=metadata,
                )
            except Exception as e:
                logger.debug("notify_memory_tool_write failed for op %s: %s", action, e)

    # -- Staleness detection (#797) ------------------------------------------

    @staticmethod
    def _entries_to_notes(entries: List[str], *, target: str) -> List["Note"]:
        """Convert built-in memory-store entries to staleness :class:`Note` objects.

        The built-in store keeps entries as plain strings (delimited by ``§``)
        with no per-entry id/timestamp metadata. Each entry is mapped to a
        :class:`~agent.memory_staleness.Note` using the entry index as a stable
        id, the first non-empty line as the title, and the remainder as the
        body content. The ``target`` (``"memory"``/``"user"``) is recorded as
        the note ``kind`` so the report can distinguish the two stores.
        """
        from agent.memory_staleness import Note

        notes: List[Note] = []
        for idx, entry in enumerate(entries):
            text = (entry or "").strip()
            if not text:
                continue
            lines = text.splitlines()
            title = lines[0].strip() if lines else text
            content = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
            notes.append(
                Note(
                    id=f"{target}-{idx}",
                    title=title or text,
                    content=content or text,
                    kind=target,
                )
            )
        return notes

    def collect_notes(self) -> List["Note"]:
        """Gather :class:`~agent.memory_staleness.Note` objects from all stores.

        Reads the on-disk built-in memory store (MEMORY.md + USER.md) via
        :func:`tools.memory_tool.load_on_disk_store` and converts each entry
        to a :class:`Note`. This is the bridge between the real memory store
        and the side-effect-free staleness analysis — no notes are mutated.

        Returns an empty list when no memory files exist (a fresh profile),
        which yields a pristine ``StalenessReport`` (quality score 1.0).
        """
        try:
            from tools.memory_tool import load_on_disk_store

            store = load_on_disk_store()
        except Exception as e:
            logger.warning("check_staleness: could not load memory store: %s", e)
            return []

        notes: List["Note"] = []
        notes.extend(self._entries_to_notes(store.memory_entries, target="memory"))
        notes.extend(self._entries_to_notes(store.user_entries, target="user"))
        return notes

    def check_staleness(self, *, config: Optional[Dict[str, Any]] = None) -> "StalenessReport":
        """Run staleness detection over the current memory corpus (#797).

        This is the real consumer of :func:`agent.memory_staleness.analyze`:
        it collects notes from the on-disk memory store and runs every
        staleness detector (age, contradiction, low-quality, duplicate,
        superseded), then returns a :class:`StalenessReport` the caller can
        render or act on. Suitable as an end-of-turn hook or a CLI
        ``hermes memory stale`` invocation.

        The analysis is pure — no notes are mutated and no memory API is
        called. Pass ``config`` to override the default thresholds.
        """
        from agent.memory_staleness import analyze, StalenessReport

        notes = self.collect_notes()
        return analyze(notes, config=config)

    def render_staleness_report(self, *, config: Optional[Dict[str, Any]] = None) -> str:
        """Run :meth:`check_staleness` and render the result as markdown.

        Convenience wrapper for the CLI ``hermes memory stale`` subcommand and
        any caller that wants a human-readable string rather than the
        structured :class:`StalenessReport`.
        """
        from agent.memory_staleness import render_report

        return render_report(self.check_staleness(config=config))

    # -- Conflict detection (#908) -------------------------------------------

    def detect_memory_conflicts(
        self, *, config: Optional[Dict[str, Any]] = None
    ) -> "ConflictReport":
        """Run conflict detection over the current memory corpus (#908).

        This is the real consumer of :func:`agent.memory_conflicts.analyze_conflicts`:
        it collects notes from the on-disk memory store (the same
        :meth:`collect_notes` used by :meth:`check_staleness`) and flags pairs
        of notes that claim different values for the same topic. Both notes
        stay exactly as they are — this is analysis only, not a mutation —
        so a caller can surface the disagreement (CLI report, system-prompt
        note) instead of an agent silently trusting whichever entry it read
        last.

        Pass ``config`` to override the default similarity thresholds.
        """
        from agent.memory_conflicts import ConflictReport, analyze_conflicts

        notes = self.collect_notes()
        return analyze_conflicts(notes, config=config)

    def render_memory_conflicts(
        self, *, config: Optional[Dict[str, Any]] = None
    ) -> str:
        """Run :meth:`detect_memory_conflicts` and render the result as markdown.

        Convenience wrapper for the CLI ``hermes memory conflicts`` subcommand
        and any caller that wants a human-readable string rather than the
        structured :class:`~agent.memory_conflicts.ConflictReport`.
        """
        from agent.memory_conflicts import render_conflict_report

        return render_conflict_report(self.detect_memory_conflicts(config=config))

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """Notify all providers that a subagent completed."""
        for provider in self._providers:
            try:
                provider.on_delegation(
                    task, result, child_session_id=child_session_id, **kwargs
                )
            except Exception as e:
                logger.debug(
                    "Memory provider '%s' on_delegation failed: %s",
                    provider.name, e,
                )

    def shutdown_all(self) -> None:
        """Shut down all providers (reverse order for clean teardown).

        Drains the background sync/prefetch executor first (bounded by
        ``_SYNC_DRAIN_TIMEOUT_S``) so a turn's final sync has a chance to
        land before providers are torn down. The worker threads are
        daemon, so anything still wedged past the drain window dies with
        the interpreter rather than blocking exit.
        """
        self._drain_sync_executor()
        for provider in reversed(self._providers):
            try:
                provider.shutdown()
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' shutdown failed: %s",
                    provider.name, e,
                )

    def _drain_sync_executor(self) -> None:
        """Shut down the background executor, waiting briefly for drain.

        Bounded by ``_SYNC_DRAIN_TIMEOUT_S``: a wedged provider must never
        hang process/session teardown. We stop accepting new work and
        cancel anything still queued, then wait at most the drain timeout
        for the currently-running task on a watcher thread. The worker is
        daemon, so an over-running task dies with the interpreter.
        """
        with self._sync_executor_lock:
            executor = self._sync_executor
            self._sync_executor = None
        if executor is None:
            return
        try:
            # Stop accepting new work and drop anything still queued, but
            # do NOT block here — cancel_futures cancels not-yet-started
            # tasks; the in-flight one keeps running on its daemon thread.
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Older Python without cancel_futures kwarg.
            try:
                executor.shutdown(wait=False)
            except Exception as e:  # pragma: no cover
                logger.debug("Memory sync executor shutdown failed: %s", e)
            return
        except Exception as e:  # pragma: no cover
            logger.debug("Memory sync executor shutdown failed: %s", e)
            return
        # Give an in-flight sync a bounded chance to finish on a watcher
        # thread so we don't block the caller past the drain timeout.
        drainer = threading.Thread(
            target=lambda: self._bounded_executor_wait(executor),
            daemon=True,
            name="mem-sync-drain",
        )
        drainer.start()
        drainer.join(timeout=_SYNC_DRAIN_TIMEOUT_S)

    @staticmethod
    def _bounded_executor_wait(executor: ThreadPoolExecutor) -> None:
        try:
            executor.shutdown(wait=True)
        except Exception as e:  # pragma: no cover
            logger.debug("Memory sync executor drain wait failed: %s", e)

    def initialize_all(self, session_id: str, **kwargs) -> None:
        """Initialize all providers.

        Automatically injects ``hermes_home`` into *kwargs* so that every
        provider can resolve profile-scoped storage paths without importing
        ``get_hermes_home()`` themselves.
        """
        if "hermes_home" not in kwargs:
            from hermes_constants import get_hermes_home
            kwargs["hermes_home"] = str(get_hermes_home())
        for provider in self._providers:
            try:
                provider.initialize(session_id=session_id, **kwargs)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' initialize failed: %s",
                    provider.name, e,
                )
