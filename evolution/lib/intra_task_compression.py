# -*- coding: utf-8 -*-
"""Intra-task context compression with knowledge consolidation.

Issue #1033 — child of #995 (Active Intra-Trajectory Context Compression).

Provides a proactive compression trigger that fires when the token count of
the running task's message history crosses a configurable threshold.  Older
messages are compressed into a summary placeholder while recent messages and
system messages are preserved.  A knowledge-extraction step captures key
facts and decisions so they survive compression.

All components are pure-Python with injectable seams — no real LLM, network,
or database calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Compression trigger
# ---------------------------------------------------------------------------

class CompressionTrigger(Enum):
    """Why compression was (or was not) initiated."""

    TOKEN_THRESHOLD = "token_threshold"
    MANUAL = "manual"
    CONTEXT_PRESSURE = "context_pressure"
    NONE = "none"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class CompressionConfig:
    """Configuration for intra-task compression.

    Attributes:
        enabled: Master switch; when False, compression never fires.
        token_threshold: Minimum estimated token count to trigger compression.
        compression_ratio: Target fraction of original tokens to retain (0–1).
        preserve_recent_count: Number of recent messages always kept verbatim.
        preserve_system_messages: Whether system-role messages are always kept.
        max_compressions_per_task: Hard cap on how many times compression may
            fire within a single task.
    """

    enabled: bool = True
    token_threshold: int = 8000
    compression_ratio: float = 0.3
    preserve_recent_count: int = 5
    preserve_system_messages: bool = True
    max_compressions_per_task: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CompressionConfig":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

class TokenEstimator:
    """Light-weight token-count estimator (≈4 chars per token).

    A custom estimator can be injected via ``token_func`` for testing or
    production-grade tokenisation.
    """

    def __init__(self, token_func: Optional[Callable[[str], int]] = None) -> None:
        self._token_func = token_func

    def estimate_message(self, msg: Dict[str, Any]) -> int:
        """Estimate tokens in a single message dict."""
        if self._token_func is not None:
            content = msg.get("content", "")
            if isinstance(content, str):
                return max(1, self._token_func(content))
            return max(1, self._token_func(str(content)))
        content = msg.get("content", "")
        if isinstance(content, str):
            return max(1, len(content) // 4)
        return max(1, len(str(content)) // 4)

    def estimate(self, messages: List[Dict[str, Any]]) -> int:
        """Estimate total tokens across all messages."""
        if not messages:
            return 0
        return sum(self.estimate_message(m) for m in messages)


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------

class CompressionTriggerEvaluator:
    """Decide whether compression should fire based on current state."""

    def __init__(self, config: CompressionConfig) -> None:
        self.config = config

    def evaluate(
        self,
        token_count: int,
        messages: List[Dict[str, Any]],
        compression_count: int,
    ) -> CompressionTrigger:
        """Return the trigger that applies, or ``NONE``."""
        if not self.config.enabled:
            return CompressionTrigger.NONE
        if compression_count >= self.config.max_compressions_per_task:
            return CompressionTrigger.NONE

        msg_count = len(messages) if messages else 0

        if token_count >= self.config.token_threshold:
            return CompressionTrigger.TOKEN_THRESHOLD
        if msg_count > 50:
            return CompressionTrigger.CONTEXT_PRESSURE
        return CompressionTrigger.NONE

    @staticmethod
    def should_compress(
        trigger: CompressionTrigger,
        config: Optional[CompressionConfig] = None,
    ) -> bool:
        """Return True for any trigger other than NONE."""
        return trigger != CompressionTrigger.NONE


# ---------------------------------------------------------------------------
# Compression result
# ---------------------------------------------------------------------------

@dataclass
class CompressionResult:
    """Outcome of a single compression operation."""

    original_token_count: int
    compressed_token_count: int
    trigger: str
    messages_preserved: int
    messages_compressed: int
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def tokens_saved(self) -> int:
        return max(0, self.original_token_count - self.compressed_token_count)

    @property
    def compression_ratio_achieved(self) -> float:
        if self.original_token_count == 0:
            return 1.0
        return self.compressed_token_count / self.original_token_count

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CompressionResult":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Knowledge block extraction
# ---------------------------------------------------------------------------

# Patterns that signal a "key fact" or "decision" worth preserving.
_KNOWLEDGE_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("decision", re.compile(
        r"(?:decid|chose|will |select|adopt|prefer|going to )", re.I)),
    ("file_path", re.compile(
        r"(?:^|\s)([/~][\w./\-]+\.\w+)")),
    ("error", re.compile(
        r"(?:error|fail|exception|traceback)", re.I)),
    ("result", re.compile(
        r"(?:result|outcome|output|return)\s*[:=]", re.I)),
    ("numeric", re.compile(r"\b\d+\b")),
]


class KnowledgeBlockExtractor:
    """Extract key facts / decisions from messages as knowledge blocks."""

    def __init__(self, patterns: Optional[List[Tuple[str, re.Pattern]]] = None) -> None:
        self._patterns = patterns or _KNOWLEDGE_PATTERNS

    # -- public API --------------------------------------------------------

    def extract(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract knowledge blocks from a list of messages."""
        blocks: List[Dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            block = self.extract_from_message(msg)
            if block is not None:
                block["source_index"] = idx
                blocks.append(block)
        return blocks

    def extract_from_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Pull key information from a single message.

        Returns ``None`` when no recognisable knowledge pattern is found.
        """
        content = msg.get("content", "")
        if not content or not isinstance(content, str):
            return None

        found: List[str] = []
        for label, pattern in self._patterns:
            if pattern.search(content):
                found.append(label)

        if not found:
            return None

        return {
            "type": "knowledge",
            "labels": found,
            "content": content.strip()[:500],  # cap to avoid huge blocks
            "role": msg.get("role", "unknown"),
        }


# ---------------------------------------------------------------------------
# Compressor
# ---------------------------------------------------------------------------

class IntraTaskCompressor:
    """Compress old messages while preserving recent + system messages."""

    def __init__(self, config: CompressionConfig) -> None:
        self.config = config
        self._estimator = TokenEstimator()
        self._extractor = KnowledgeBlockExtractor()
        self._stats_total = 0
        self._stats_tokens_saved = 0
        self._stats_ratios: List[float] = []

    # -- main entry --------------------------------------------------------

    def compress(
        self,
        messages: List[Dict[str, Any]],
        trigger: CompressionTrigger = CompressionTrigger.TOKEN_THRESHOLD,
    ) -> Tuple[List[Dict[str, Any]], CompressionResult]:
        """Compress *messages* and return (new_messages, result).

        ``trigger`` records *why* compression fired and is echoed back on the
        resulting :class:`CompressionResult` (e.g. ``CONTEXT_PRESSURE`` vs the
        default ``TOKEN_THRESHOLD``).  It documents the cause; it does not
        change how the compression itself is performed.
        """
        original_tokens = self._estimator.estimate(messages)
        self._stats_total += 1

        if not messages:
            result = CompressionResult(
                original_token_count=0,
                compressed_token_count=0,
                trigger=CompressionTrigger.NONE.value,
                messages_preserved=0,
                messages_compressed=0,
            )
            self._stats_ratios.append(1.0)
            return [], result

        preserve_n = self.config.preserve_recent_count
        preserve_system = self.config.preserve_system_messages

        # When preserve_n is 0, we still need at least the recent slice logic
        # to work correctly — treat it as "compress everything".
        # Partition: everything before the recent window is a candidate for
        # compression, except system messages which are always preserved.
        if preserve_n > 0 and len(messages) <= preserve_n:
            # Nothing to compress — everything is "recent"
            result = CompressionResult(
                original_token_count=original_tokens,
                compressed_token_count=original_tokens,
                trigger=CompressionTrigger.NONE.value,
                messages_preserved=len(messages),
                messages_compressed=0,
            )
            self._stats_ratios.append(1.0)
            return list(messages), result

        if preserve_n > 0:
            recent = messages[-preserve_n:]
            older = messages[:-preserve_n]
        else:
            recent = []
            older = list(messages)

        # Split older into system (preserve) and non-system (compress)
        system_msgs: List[Dict[str, Any]] = []
        to_compress: List[Dict[str, Any]] = []
        for m in older:
            if preserve_system and m.get("role") == "system":
                system_msgs.append(m)
            else:
                to_compress.append(m)

        compressed = self._compress_old_messages(to_compress)

        new_messages: List[Dict[str, Any]] = []
        new_messages.extend(system_msgs)
        if compressed is not None:
            new_messages.append(compressed)
        new_messages.extend(recent)

        compressed_tokens = self._estimator.estimate(new_messages)

        result = CompressionResult(
            original_token_count=original_tokens,
            compressed_token_count=compressed_tokens,
            trigger=trigger.value,
            messages_preserved=len(system_msgs) + len(recent),
            messages_compressed=len(to_compress),
            metadata={
                "knowledge_blocks": len(
                    self._extractor.extract(to_compress)
                ),
            },
        )

        saved = result.tokens_saved
        self._stats_tokens_saved += saved
        if original_tokens > 0:
            self._stats_ratios.append(compressed_tokens / original_tokens)
        else:
            self._stats_ratios.append(1.0)

        return new_messages, result

    # -- helpers -----------------------------------------------------------

    def _compress_old_messages(
        self, messages: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Compress a list of old messages into a single summary dict."""
        if not messages:
            return None

        knowledge_blocks = self._extractor.extract(messages)

        # Derive the summary character budget from the configured
        # compression_ratio ("target fraction of original tokens to retain"),
        # bounded by a floor (keep the summary useful) and a ceiling (never
        # unbounded).  A lower ratio yields a tighter summary.
        original_chars = sum(
            len(m.get("content", ""))
            for m in messages
            if isinstance(m.get("content", ""), str)
        )
        budget = max(200, min(1000, int(original_chars * self.config.compression_ratio)))

        summary_parts: List[str] = []
        for kb in knowledge_blocks:
            summary_parts.append(kb["content"])

        # If no structured knowledge was extracted, fall back to a
        # heavily-truncated concatenation of the original content, capping
        # each message to ~50 chars and the total to the ratio-derived budget
        # so the summary is always smaller than the original messages.
        if not summary_parts:
            per_msg_cap = 50
            running_len = 0
            for m in messages:
                content = m.get("content", "")
                if isinstance(content, str) and content.strip():
                    snippet = content.strip()[:per_msg_cap]
                    if running_len + len(snippet) > budget:
                        break
                    summary_parts.append(snippet)
                    running_len += len(snippet)

        # Cap total summary to the ratio-derived budget.
        summary_text = " | ".join(summary_parts)[:budget]

        return {
            "role": "system",
            "content": (
                f"[compressed {len(messages)} earlier messages — "
                f"{len(knowledge_blocks)} knowledge blocks preserved]\n"
                f"{summary_text}"
            ),
            "_compressed": True,
        }

    def get_stats(self) -> Dict[str, Any]:
        """Return cumulative statistics."""
        avg_ratio = (
            sum(self._stats_ratios) / len(self._stats_ratios)
            if self._stats_ratios
            else 1.0
        )
        return {
            "total_compressions": self._stats_total,
            "total_tokens_saved": self._stats_tokens_saved,
            "average_ratio": round(avg_ratio, 4),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CompressionTrigger",
    "CompressionConfig",
    "TokenEstimator",
    "CompressionTriggerEvaluator",
    "CompressionResult",
    "KnowledgeBlockExtractor",
    "IntraTaskCompressor",
]
