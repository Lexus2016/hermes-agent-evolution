"""Observational context compression module for long-horizon terminal work (issues #3066, #3068, #3069).

Provides a deterministic, syntax-aware observational compression strategy for
terminal logs, build outputs, test matrices, and repetitive polling loops
(informed by Ren et al., arXiv:2507.17049).

Implements the ContextEngine ABC and can be selected via ``context.engine: "observational"``
or enabled via ``context.observational_compression: true``.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from agent.context_engine import ContextEngine
from agent.model_metadata import estimate_messages_tokens_rough

logger = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_PROGRESS_BAR_RE = re.compile(r"(?:\[[=\->\s#\.]+\]|.*?\d+%)")
_POLL_LINE_RE = re.compile(r"(?i)(?:waiting for|polling|checking status|pending|in progress|still running)[. ]*")


@dataclass
class ObservationalCompressionConfig:
    enabled: bool = True
    max_head_lines: int = 15
    max_tail_lines: int = 25
    max_output_chars: int = 4000
    strip_ansi: bool = True
    dedup_polling_threshold: int = 3
    token_threshold: int = 12000
    tail_turns_to_preserve: int = 4


def distill_terminal_output(
    raw_output: str,
    max_head_lines: int = 15,
    max_tail_lines: int = 25,
    max_chars: int = 4000,
) -> str:
    if not raw_output:
        return ""

    text = _ANSI_ESCAPE_RE.sub("", raw_output)
    if len(text) <= 200:
        return text

    raw_lines = text.splitlines()
    clean_lines: list[str] = []

    last_poll_pattern: Optional[str] = None
    poll_repeat_count = 0

    for line in raw_lines:
        line_s = line.strip()
        if not line_s:
            continue

        if _PROGRESS_BAR_RE.search(line_s) and len(line_s) > 10:
            continue

        if _POLL_LINE_RE.match(line_s):
            prefix = line_s[:20].lower()
            if prefix == last_poll_pattern:
                poll_repeat_count += 1
                continue
            else:
                if poll_repeat_count > 0:
                    clean_lines.append(f"... [{poll_repeat_count} similar status lines suppressed] ...")
                last_poll_pattern = prefix
                poll_repeat_count = 0
        else:
            if poll_repeat_count > 0:
                clean_lines.append(f"... [{poll_repeat_count} similar status lines suppressed] ...")
                last_poll_pattern = None
                poll_repeat_count = 0

        clean_lines.append(line)

    if poll_repeat_count > 0:
        clean_lines.append(f"... [{poll_repeat_count} similar status lines suppressed] ...")

    total_lines = len(clean_lines)
    if total_lines <= (max_head_lines + max_tail_lines + 5):
        distilled = "\n".join(clean_lines)
    else:
        head = clean_lines[:max_head_lines]
        tail = clean_lines[-max_tail_lines:]
        omitted = total_lines - (max_head_lines + max_tail_lines)
        distilled = (
            "\n".join(head)
            + f"\n\n... [{omitted} intermediate output lines distilled by observational compressor] ...\n\n"
            + "\n".join(tail)
        )

    if len(distilled) > max_chars:
        half = max_chars // 2 - 50
        distilled = distilled[:half] + "\n...[truncated]...\n" + distilled[-half:]

    return distilled


class ObservationalContextEngine(ContextEngine):
    @property
    def name(self) -> str:
        return "observational"

    def __init__(self, config: Optional[ObservationalCompressionConfig] = None):
        self.config = config or ObservationalCompressionConfig()
        self.session_id: Optional[str] = None
        self.total_tokens_compressed: int = 0
        self.compressions_count: int = 0

    def on_session_start(self, session_id: str, platform: str = "cli", model: str = "") -> None:
        self.session_id = session_id
        self.total_tokens_compressed = 0
        self.compressions_count = 0

    def update_from_response(
        self,
        response_usage: dict[str, Any],
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        pass

    def should_compress(self, messages: list[dict[str, Any]], model: str = "") -> bool:
        if not self.config.enabled:
            return False
        total_tokens = estimate_messages_tokens_rough(messages)
        return total_tokens >= self.config.token_threshold

    def compress(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        max_tokens: int = 4000,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not messages:
            return messages, {"compressed": False, "tokens_saved": 0}

        initial_tokens = estimate_messages_tokens_rough(messages)
        compressed_msgs: list[dict[str, Any]] = []

        total_msgs = len(messages)
        cutoff_idx = max(0, total_msgs - self.config.tail_turns_to_preserve)

        for idx, msg in enumerate(messages):
            role = msg.get("role")
            content = msg.get("content")

            if idx >= cutoff_idx or role == "system" or not isinstance(content, str):
                compressed_msgs.append(msg)
                continue

            if role == "tool" or (role == "user" and "Tool Call Result" in str(content)):
                distilled_content = distill_terminal_output(
                    content,
                    max_head_lines=self.config.max_head_lines,
                    max_tail_lines=self.config.max_tail_lines,
                    max_chars=self.config.max_output_chars,
                )
                msg_copy = dict(msg)
                msg_copy["content"] = distilled_content
                compressed_msgs.append(msg_copy)
            else:
                compressed_msgs.append(msg)

        final_tokens = estimate_messages_tokens_rough(compressed_msgs)
        tokens_saved = max(0, initial_tokens - final_tokens)

        self.total_tokens_compressed += tokens_saved
        self.compressions_count += 1

        return compressed_msgs, {
            "compressed": True,
            "tokens_saved": tokens_saved,
            "initial_tokens": initial_tokens,
            "final_tokens": final_tokens,
            "compression_ratio": round(final_tokens / initial_tokens, 4) if initial_tokens > 0 else 1.0,
        }

    def on_session_end(self) -> None:
        self.session_id = None
