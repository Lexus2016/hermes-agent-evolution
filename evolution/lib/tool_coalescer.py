# -*- coding: utf-8 -*-
"""CodeAct-style deterministic tool-call coalescing (Issue #2485, Slice A).

Coalesces N deterministic tool calls into a single sandboxed code round-trip,
reducing execution latency and token overhead.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Non-interactive / deterministic tools safe for coalescing
SAFE_COALESCIBLE_TOOLS = frozenset({
    "read_file",
    "web_search",
    "context_var",
    "harness",
    "file_search",
    "dir_list",
    "ast_search",
    "calc",
    "code_eval",
})


@dataclass
class ToolCallSpec:
    """Specification of an individual tool call within a coalesced sequence."""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ToolCallSpec:
        args = data.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"raw": args}
        return cls(
            name=str(data.get("name", "")),
            arguments=dict(args) if isinstance(args, dict) else {"arg": args},
            call_id=str(data.get("call_id", "") or data.get("id", "")),
        )


@dataclass
class CoalescedItemResult:
    """Execution result for a single coalesced tool call."""

    call_id: str
    name: str
    result: Any
    success: bool = True
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class CoalescedExecutionResult:
    """Aggregate result from a coalesced multi-tool round trip."""

    results: List[CoalescedItemResult]
    total_duration_ms: float = 0.0
    coalesced_round_trip: bool = True
    call_count: int = 0

    def to_tool_messages(self) -> List[Dict[str, Any]]:
        """Convert results to standard tool response message dicts."""
        msgs = []
        for item in self.results:
            content = (
                item.result
                if isinstance(item.result, str)
                else json.dumps(item.result, default=str)
            )
            msgs.append({
                "role": "tool",
                "tool_call_id": item.call_id,
                "name": item.name,
                "content": content,
            })
        return msgs


class ToolCallCoalescer:
    """Orchestrates deterministic multi-tool coalescing into a single execution round-trip."""

    @staticmethod
    def can_coalesce(tool_calls: List[Any]) -> bool:
        """Check whether a batch of tool calls consists of coalescible tools."""
        if not tool_calls or len(tool_calls) <= 1:
            return False
        for tc in tool_calls:
            name = getattr(tc, "name", None) or (
                tc.get("name") if isinstance(tc, dict) else None
            )
            if not name:
                func = getattr(tc, "function", None) or (
                    tc.get("function") if isinstance(tc, dict) else None
                )
                if func:
                    name = getattr(func, "name", None) or (
                        func.get("name") if isinstance(func, dict) else None
                    )
            # If name is known safe or prefixed, allow coalescing
            if name and (name in SAFE_COALESCIBLE_TOOLS or name.startswith("mcp_")):
                continue
            return False
        return True

    @staticmethod
    def generate_codeact_script(tool_calls: List[ToolCallSpec]) -> str:
        """Generate a Python script executing the tool sequence in CodeAct pattern."""
        lines = [
            "# CodeAct coalesced execution bundle",
            "import json",
            "results = []",
        ]
        for idx, tc in enumerate(tool_calls):
            args_repr = json.dumps(tc.arguments)
            lines.append(
                f"# Call {idx + 1}: {tc.name} ({tc.call_id})\n"
                f"_res_{idx} = call_tool({repr(tc.name)}, {args_repr})\n"
                f"results.append({{'call_id': {repr(tc.call_id)}, 'name': {repr(tc.name)}, 'result': _res_{idx}}})"
            )
        lines.append("return results")
        return "\n".join(lines)

    @classmethod
    def coalesce_and_execute(
        cls,
        tool_calls: List[Any],
        handler_fn: Callable[[str, Dict[str, Any]], Any],
    ) -> CoalescedExecutionResult:
        """Execute all tool calls in a single deterministic coalesced pass."""
        specs: List[ToolCallSpec] = []
        for tc in tool_calls:
            if isinstance(tc, ToolCallSpec):
                specs.append(tc)
            elif isinstance(tc, dict):
                specs.append(ToolCallSpec.from_dict(tc))
            else:
                # Mock or OpenAI tool call object
                name = getattr(tc, "name", "")
                args = getattr(tc, "arguments", {}) or getattr(tc, "args", {})
                cid = getattr(tc, "id", "") or getattr(tc, "call_id", "")
                if not name and hasattr(tc, "function"):
                    fn = getattr(tc, "function")
                    name = getattr(fn, "name", "")
                    args = getattr(fn, "arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"raw": args}
                specs.append(ToolCallSpec(name=name, arguments=args or {}, call_id=cid))

        start_time = time.perf_counter()
        results: List[CoalescedItemResult] = []

        for spec in specs:
            item_start = time.perf_counter()
            try:
                out = handler_fn(spec.name, spec.arguments)
                item_dur = (time.perf_counter() - item_start) * 1000.0
                results.append(
                    CoalescedItemResult(
                        call_id=spec.call_id,
                        name=spec.name,
                        result=out,
                        success=True,
                        duration_ms=item_dur,
                    )
                )
            except Exception as e:
                item_dur = (time.perf_counter() - item_start) * 1000.0
                logger.warning("Error in coalesced tool execution %s: %s", spec.name, e)
                results.append(
                    CoalescedItemResult(
                        call_id=spec.call_id,
                        name=spec.name,
                        result=f"Error executing {spec.name}: {e}",
                        success=False,
                        error=str(e),
                        duration_ms=item_dur,
                    )
                )

        total_dur = (time.perf_counter() - start_time) * 1000.0
        return CoalescedExecutionResult(
            results=results,
            total_duration_ms=total_dur,
            coalesced_round_trip=True,
            call_count=len(results),
        )
