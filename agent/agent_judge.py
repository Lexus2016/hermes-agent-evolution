"""Agent-as-a-Judge evaluation harness (first increment of #226).

A self-contained, post-hoc evaluator that scores an agent execution trace
against a structured rubric.  Mirrors the shape of ``agent.entropy_eval`` (a
behavioural-metrics sibling): it runs against a session message list with no
runtime dependency on the agent loop, exposes plain dataclasses, and offers a
``to_dict`` / ``format_report_terminal`` pair for downstream consumers.

Two scoring paths share one rubric:

* **deterministic** (``score_trace_heuristic``) — no LLM, no cost.  Derives a
  defensible baseline score per dimension from structural trace signals (tool
  failures, repetition, refusals, whether the agent finished).  Always
  available, used as the fallback when no LLM provider is configured.
* **LLM-backed** (``score_trace_llm``) — sends a compact, schema-constrained
  prompt to the shared auxiliary client (``agent.auxiliary_client.call_llm``,
  the same router session-titling and compression use) and parses a STRICT
  JSON object back.  Malformed or out-of-range model output is rejected, never
  silently trusted (see the held-out-evaluation lesson: the judge computes the
  verdict itself and clamps every number).

Public API
----------
    from agent.agent_judge import AgentJudge, DEFAULT_RUBRIC

    judge = AgentJudge()                      # default rubric
    verdict = judge.score(session_id, messages, task="…")  # LLM if available
    print(judge.format_report_terminal(verdict))
    verdict.to_dict()                         # JSON-serialisable

Wiring this into a post-execution hook / regression dashboard (success
criteria 3 & 4 of #226) is intentionally DEFERRED — this increment delivers
the standalone, independently-testable scoring core other code can call.

Reference
---------
* Agent-as-a-Judge, ICML 2025 — https://proceedings.mlr.press/v267/zhuge25a.html
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Score bounds.  Every dimension and the overall score live on [0, 1] so the
# rubric weights compose into a clean weighted mean.
MIN_SCORE = 0.0
MAX_SCORE = 1.0


def _clamp(value: float) -> float:
    """Clamp a score into [MIN_SCORE, MAX_SCORE]; coerce non-finite to 0.0.

    The judge NEVER trusts a raw model-emitted number — out-of-range or NaN
    values are silently corrected here rather than propagated into a verdict.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return MIN_SCORE
    if f != f:  # NaN
        return MIN_SCORE
    return max(MIN_SCORE, min(MAX_SCORE, f))


# ── Rubric ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RubricDimension:
    """One scored axis of the rubric.

    Attributes:
        key: Stable machine identifier (used as the JSON field the LLM emits).
        title: Human-readable label for terminal output.
        description: What "high" means — included verbatim in the LLM prompt
            so the model and the deterministic heuristic share one definition.
        weight: Relative weight in the overall score (weights are normalised,
            so absolute magnitude is irrelevant — only ratios matter).
    """

    key: str
    title: str
    description: str
    weight: float = 1.0


@dataclass(frozen=True)
class Rubric:
    """An ordered set of weighted scoring dimensions."""

    dimensions: Sequence[RubricDimension]

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise ValueError("Rubric requires at least one dimension")
        keys = [d.key for d in self.dimensions]
        if len(keys) != len(set(keys)):
            raise ValueError("Rubric dimension keys must be unique")

    def keys(self) -> List[str]:
        return [d.key for d in self.dimensions]

    def total_weight(self) -> float:
        total = sum(d.weight for d in self.dimensions)
        # Guard a degenerate all-zero-weight rubric so weighting falls back to
        # an unweighted mean instead of dividing by zero.
        return total if total > 0 else float(len(self.dimensions))


# Default rubric aligned with the human-judgment criteria named in #226:
# does the trace show planning, does it actually solve the task, does it use
# tools soundly and verify, and is it efficient (no spinning).
DEFAULT_RUBRIC = Rubric(
    dimensions=(
        RubricDimension(
            key="task_completion",
            title="Task completion",
            description=(
                "The agent fully accomplished what the task asked, with a clear "
                "final result. Partial or abandoned work scores low."
            ),
            weight=2.0,
        ),
        RubricDimension(
            key="tool_use",
            title="Tool use & verification",
            description=(
                "Tools were used soundly: appropriate tools, recovered from "
                "errors, and verified results rather than asserting success blindly."
            ),
            weight=1.5,
        ),
        RubricDimension(
            key="reasoning",
            title="Reasoning & planning",
            description=(
                "The trajectory shows coherent planning and step-by-step "
                "reasoning toward the goal, not flailing or guessing."
            ),
            weight=1.0,
        ),
        RubricDimension(
            key="efficiency",
            title="Efficiency",
            description=(
                "The agent reached the result without redundant, repeated, or "
                "wasted steps. Loops and repetition score low."
            ),
            weight=1.0,
        ),
    )
)


# ── Trace extraction ──────────────────────────────────────────────────────


@dataclass
class TraceSummary:
    """Structural signals distilled from a session message list.

    Deliberately the SAME message shape ``agent.entropy_eval`` consumes
    (OpenAI-style ``{"role", "content", "tool_calls"}`` dicts) so any caller
    that already has a trajectory can feed both evaluators.
    """

    message_count: int = 0
    user_turns: int = 0
    assistant_turns: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    tool_failures: int = 0
    repeated_tool_runs: int = 0
    refusals: int = 0
    has_final_answer: bool = False
    tool_names: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_count": self.message_count,
            "user_turns": self.user_turns,
            "assistant_turns": self.assistant_turns,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "tool_failures": self.tool_failures,
            "repeated_tool_runs": self.repeated_tool_runs,
            "refusals": self.refusals,
            "has_final_answer": self.has_final_answer,
            "tool_names": list(self.tool_names),
        }


# Failure / refusal markers. Reuse loop_guard's failure detector when present
# (single source of truth, consistent with introspection_extract.py), keeping
# a small literal fallback so this module stays importable standalone.
try:  # pragma: no cover - exercised indirectly; import shape varies by tree
    from agent.loop_guard import _looks_like_failure as _looks_like_failure
except Exception:  # pragma: no cover
    _FAILURE_MARKERS = (
        "error:", "failed", "permission denied", "command not found",
        "no such file", "timed out", "timeout", "traceback (most recent call",
    )

    def _looks_like_failure(content: Any) -> bool:
        return isinstance(content, str) and any(
            m in content.lower() for m in _FAILURE_MARKERS
        )


_REFUSAL_RE = re.compile(
    r"\b(i can'?t|i cannot|i'?m (?:unable|not able)|no access|access denied|"
    r"not permitted|don'?t have (?:access|permission))\b",
    re.IGNORECASE,
)
# A repeated-run spiral: the SAME tool fired this many times in a row.
_REPEAT_THRESHOLD = 3


def _tool_name(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return "unknown"
    fn = tool_call.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(tool_call.get("name") or "unknown")


def summarize_trace(messages: Sequence[Dict[str, Any]]) -> TraceSummary:
    """Extract structural signals from a session message list.

    Pure and deterministic — no LLM, no I/O.  Drives the heuristic score and
    is embedded (compactly) into the LLM prompt so the model sees the same
    objective signals the heuristic does.
    """
    summary = TraceSummary(message_count=len(messages))
    last_assistant_text = ""
    consecutive_tool = ""
    consecutive_count = 0

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []

        if role == "user":
            summary.user_turns += 1
        elif role == "assistant":
            summary.assistant_turns += 1
            if tool_calls:
                for tc in tool_calls:
                    name = _tool_name(tc)
                    summary.tool_calls += 1
                    summary.tool_names.append(name)
                    if name == consecutive_tool:
                        consecutive_count += 1
                    else:
                        consecutive_tool = name
                        consecutive_count = 1
                    if consecutive_count == _REPEAT_THRESHOLD:
                        # Count the spiral once, when it crosses the threshold.
                        summary.repeated_tool_runs += 1
            elif isinstance(content, str) and content.strip():
                last_assistant_text = content
                if _REFUSAL_RE.search(content):
                    summary.refusals += 1
        elif role == "tool":
            summary.tool_results += 1
            if _looks_like_failure(content):
                summary.tool_failures += 1

    # A trace "has a final answer" when its last substantive assistant message
    # is plain text (a delivered result) rather than a dangling tool call.
    summary.has_final_answer = bool(last_assistant_text.strip())
    return summary


# ── Verdict ────────────────────────────────────────────────────────────────


@dataclass
class JudgeVerdict:
    """The structured result of scoring one trace."""

    session_id: str
    overall_score: float
    dimension_scores: Dict[str, float]
    rationale: str
    method: str  # "llm" | "heuristic"
    trace_summary: TraceSummary
    model: Optional[str] = None

    def passed(self, threshold: float = 0.7) -> bool:
        """Whether the verdict clears a pass/fail threshold (default 0.7)."""
        return self.overall_score >= threshold

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "overall_score": round(self.overall_score, 4),
            "dimension_scores": {
                k: round(v, 4) for k, v in self.dimension_scores.items()
            },
            "rationale": self.rationale,
            "method": self.method,
            "model": self.model,
            "trace_summary": self.trace_summary.to_dict(),
        }


# ── Prompt construction & parsing ──────────────────────────────────────────


def _build_rubric_block(rubric: Rubric) -> str:
    lines = []
    for d in rubric.dimensions:
        lines.append(f'- "{d.key}" ({d.title}, weight {d.weight:g}): {d.description}')
    return "\n".join(lines)


def build_judge_messages(
    session_id: str,
    summary: TraceSummary,
    transcript_excerpt: str,
    rubric: Rubric,
    task: Optional[str],
) -> List[Dict[str, str]]:
    """Assemble the chat messages for an LLM scoring call.

    The system prompt pins the output to a strict JSON schema; the user message
    carries the rubric, the objective trace signals, and a bounded transcript
    excerpt.  Kept compact on purpose — this is a cheap auxiliary call, not a
    full replay (success criterion 2: cost well under an external baseline).
    """
    keys = rubric.keys()
    schema_fields = ", ".join(f'"{k}": <0.0-1.0>' for k in keys)
    system = (
        "You are a strict, impartial evaluator of AI-agent execution traces. "
        "Score the trace against the rubric. Be skeptical: reward verified "
        "results, penalise unrecovered tool failures, repetition, and "
        "abandoned work. Compute every score yourself from the evidence; do "
        "not trust any number the trace asserts about itself.\n\n"
        "Respond with ONLY a single JSON object, no markdown, no prose, of the "
        "exact form:\n"
        f'{{"scores": {{{schema_fields}}}, "rationale": "<=2 sentences"}}\n'
        "Every score is a float in [0.0, 1.0]. Include every rubric key."
    )
    parts = [f"## Rubric\n{_build_rubric_block(rubric)}"]
    if task:
        parts.append(f"## Task the agent was given\n{task.strip()[:1000]}")
    parts.append(
        "## Objective trace signals\n"
        + json.dumps(summary.to_dict(), ensure_ascii=False, indent=2)
    )
    parts.append(f"## Trace excerpt\n{transcript_excerpt}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


_JSON_DECODER = json.JSONDecoder()


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first parseable top-level JSON object out of model text.

    Tolerates code fences and leading/trailing prose (small models sometimes
    wrap JSON despite instructions). Each ``{`` is handed to the stdlib
    decoder's ``raw_decode`` — the C-optimised parser owns string-escape and
    brace-balancing, so prose braces or a ``{}`` inside a string literal never
    confuse the boundary. Returns None if nothing parseable.
    """
    if not text:
        return None
    stripped = text.strip()
    start = stripped.find("{")
    while start != -1:
        try:
            obj, _ = _JSON_DECODER.raw_decode(stripped, start)
        except (json.JSONDecodeError, ValueError):
            start = stripped.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        # Parsed a non-object (e.g. set-like text the decoder rejected as
        # something else); keep scanning for a real object.
        start = stripped.find("{", start + 1)
    return None


def parse_judge_response(text: str, rubric: Rubric) -> Optional[Dict[str, Any]]:
    """Validate and normalise a raw LLM judge response.

    Enforces the strict schema: a ``scores`` object must be present and must
    cover EVERY rubric dimension. Any missing dimension rejects the whole
    response (returns None) — a partial score is worse than falling back to the
    deterministic heuristic, because it silently hides the gap.

    Returns ``{"scores": {key: clamped float}, "rationale": str}`` or None.
    """
    obj = _extract_json_object(text)
    if obj is None:
        return None
    raw_scores = obj.get("scores")
    if not isinstance(raw_scores, dict):
        return None
    scores: Dict[str, float] = {}
    for key in rubric.keys():
        if key not in raw_scores:
            return None  # incomplete -> reject, fall back to heuristic
        scores[key] = _clamp(raw_scores[key])
    rationale = obj.get("rationale")
    if not isinstance(rationale, str):
        rationale = ""
    return {"scores": scores, "rationale": rationale.strip()}


def _weighted_overall(scores: Dict[str, float], rubric: Rubric) -> float:
    total_w = rubric.total_weight()
    acc = 0.0
    for d in rubric.dimensions:
        acc += scores.get(d.key, 0.0) * d.weight
    return _clamp(acc / total_w)


# ── Deterministic heuristic scoring ──────────────────────────────────────────


def score_trace_heuristic(
    session_id: str,
    messages: Sequence[Dict[str, Any]],
    rubric: Rubric = DEFAULT_RUBRIC,
    *,
    summary: Optional[TraceSummary] = None,
) -> JudgeVerdict:
    """Score a trace from structural signals only — no LLM, no cost.

    A defensible baseline: it cannot judge semantic correctness, but it
    reliably penalises the failure shapes the trace makes objective (unrecovered
    tool errors, refusal, repetition spirals, no delivered answer). Always
    available, and the automatic fallback when no LLM provider is configured.

    Only the rubric dimensions present in ``DEFAULT_RUBRIC`` get a tailored
    signal; any custom dimension falls back to a neutral 0.5 so a custom rubric
    still produces a usable score rather than crashing.
    """
    summary = summary if summary is not None else summarize_trace(messages)
    scores: Dict[str, float] = {}

    # task_completion: did the agent deliver a final answer, undamaged by
    # refusal? Heavily binary by nature.
    completion = 0.0
    if summary.has_final_answer:
        completion = 0.8
        if summary.refusals == 0:
            completion = 1.0
    if summary.refusals and not summary.has_final_answer:
        completion = 0.1

    # tool_use: clean if there were tool calls and few/no failures.
    if summary.tool_calls == 0:
        tool_use = 0.6  # no tools used — neutral, can't fault tool handling
    else:
        fail_ratio = summary.tool_failures / max(summary.tool_results, 1)
        tool_use = _clamp(1.0 - fail_ratio)

    # reasoning: proxy via turn structure — some assistant turns relative to
    # tool churn. Very rough; the LLM path is where real reasoning is judged.
    if summary.assistant_turns == 0:
        reasoning = 0.0
    else:
        reasoning = 0.7 if summary.has_final_answer else 0.4

    # efficiency: penalise repetition spirals.
    efficiency = _clamp(1.0 - 0.3 * summary.repeated_tool_runs)

    tailored = {
        "task_completion": completion,
        "tool_use": tool_use,
        "reasoning": reasoning,
        "efficiency": efficiency,
    }
    for d in rubric.dimensions:
        scores[d.key] = _clamp(tailored.get(d.key, 0.5))

    overall = _weighted_overall(scores, rubric)
    rationale = (
        f"Heuristic: {'delivered' if summary.has_final_answer else 'no'} final "
        f"answer, {summary.tool_failures}/{summary.tool_results} tool failures, "
        f"{summary.repeated_tool_runs} repetition spiral(s), "
        f"{summary.refusals} refusal(s)."
    )
    return JudgeVerdict(
        session_id=session_id,
        overall_score=overall,
        dimension_scores=scores,
        rationale=rationale,
        method="heuristic",
        trace_summary=summary,
        model=None,
    )


# ── Transcript rendering for the LLM ──────────────────────────────────────────


def render_transcript_excerpt(
    messages: Sequence[Dict[str, Any]],
    *,
    max_chars: int = 6000,
    max_messages: int = 40,
) -> str:
    """Render a bounded, role-tagged transcript for the LLM prompt.

    Bounded on BOTH message count and total characters so an enormous trace
    cannot blow the auxiliary call's context or cost. When truncated, the head
    and tail are kept (the task setup and the outcome are the most diagnostic
    parts) with an elision marker between them.
    """
    rendered: List[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "unknown")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            names = ", ".join(_tool_name(tc) for tc in tool_calls)
            text = (content or "").strip()
            line = f"[assistant] calls tools: {names}"
            if text:
                line += f"\n  {text}"
        elif role == "tool":
            body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            line = f"[tool result] {body}"
        else:
            body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            line = f"[{role}] {body}"
        rendered.append(line)

    if len(rendered) > max_messages:
        head = rendered[: max_messages // 2]
        tail = rendered[-(max_messages // 2):]
        rendered = head + [f"… ({len(rendered) - max_messages} messages elided) …"] + tail

    excerpt = "\n".join(rendered)
    if len(excerpt) > max_chars:
        keep = max_chars // 2
        excerpt = (
            excerpt[:keep]
            + f"\n… ({len(excerpt) - max_chars} chars elided) …\n"
            + excerpt[-keep:]
        )
    return excerpt


# ── LLM-backed scoring ──────────────────────────────────────────────────────


def score_trace_llm(
    session_id: str,
    messages: Sequence[Dict[str, Any]],
    rubric: Rubric = DEFAULT_RUBRIC,
    *,
    task: Optional[str] = None,
    timeout: float = 60.0,
    main_runtime: Optional[Dict[str, Any]] = None,
    summary: Optional[TraceSummary] = None,
) -> Optional[JudgeVerdict]:
    """Score a trace with an LLM via the shared auxiliary client.

    Returns a ``JudgeVerdict`` (method="llm") on success, or ``None`` if no
    provider is configured or the response can't be validated against the
    rubric schema. Callers that want a guaranteed result use ``AgentJudge.score``
    which falls back to the deterministic heuristic on None.

    The LLM is imported lazily so this module stays importable (and the
    heuristic path stays usable) in environments where the auxiliary client or
    its provider chain is unavailable.
    """
    # The objective trace signals are computed from the FULL message list
    # (summarize_trace), never the truncated excerpt — only the LLM-facing
    # transcript is bounded. This keeps the failure/repetition counts the model
    # sees (and the heuristic uses) accurate even when the trace is huge.
    summary = summary if summary is not None else summarize_trace(messages)
    excerpt = render_transcript_excerpt(messages)
    chat = build_judge_messages(session_id, summary, excerpt, rubric, task)

    try:
        from agent.auxiliary_client import call_llm
    except Exception as e:  # pragma: no cover - import-time only
        logger.debug("agent_judge: auxiliary client unavailable: %s", e)
        return None

    try:
        response = call_llm(
            task="agent_judge",
            messages=chat,
            max_tokens=600,
            temperature=0.0,  # deterministic verdicts
            timeout=timeout,
            main_runtime=main_runtime,
        )
    except Exception as e:
        logger.warning("agent_judge: LLM scoring failed: %s", e)
        logger.debug("agent_judge LLM traceback", exc_info=True)
        return None

    content = ""
    model = None
    try:
        content = response.choices[0].message.content or ""
        model = getattr(response, "model", None)
    except (AttributeError, IndexError, TypeError):
        logger.warning("agent_judge: unexpected LLM response shape")
        return None

    parsed = parse_judge_response(content, rubric)
    if parsed is None:
        logger.warning("agent_judge: LLM response failed schema validation")
        return None

    scores = parsed["scores"]
    overall = _weighted_overall(scores, rubric)
    return JudgeVerdict(
        session_id=session_id,
        overall_score=overall,
        dimension_scores=scores,
        rationale=parsed["rationale"],
        method="llm",
        trace_summary=summary,
        model=model,
    )


# ── Engine + formatting ──────────────────────────────────────────────────────


class AgentJudge:
    """Score agent execution traces against a rubric.

    Usage
    -----
        from agent.agent_judge import AgentJudge

        judge = AgentJudge()
        verdict = judge.score(session_id, messages, task="fix the failing test")
        print(judge.format_report_terminal(verdict))
    """

    def __init__(self, rubric: Rubric = DEFAULT_RUBRIC):
        self.rubric = rubric

    def score(
        self,
        session_id: str,
        messages: Sequence[Dict[str, Any]],
        *,
        task: Optional[str] = None,
        use_llm: bool = True,
        timeout: float = 60.0,
        main_runtime: Optional[Dict[str, Any]] = None,
    ) -> JudgeVerdict:
        """Score a trace, preferring the LLM and falling back to the heuristic.

        Always returns a verdict: if ``use_llm`` is False, or no provider is
        configured, or the LLM output fails validation, the deterministic
        heuristic verdict is returned instead.
        """
        summary = summarize_trace(messages)
        if use_llm:
            verdict = score_trace_llm(
                session_id,
                messages,
                self.rubric,
                task=task,
                timeout=timeout,
                main_runtime=main_runtime,
                summary=summary,
            )
            if verdict is not None:
                return verdict
        return score_trace_heuristic(
            session_id, messages, self.rubric, summary=summary
        )

    def format_report_terminal(self, verdict: JudgeVerdict) -> str:
        """Return a compact, terminal-ready verdict summary."""
        return format_report_terminal(verdict, self.rubric)


def format_report_terminal(verdict: JudgeVerdict, rubric: Rubric = DEFAULT_RUBRIC) -> str:
    """Render a verdict for the terminal (mirrors entropy_eval's formatter)."""
    sid = verdict.session_id
    sid_display = f"{sid[:24]}..." if len(sid) > 24 else sid
    titles = {d.key: d.title for d in rubric.dimensions}
    verdict_word = "PASS" if verdict.passed() else "FAIL"
    lines = [
        f"  ⚖️  Agent-as-a-Judge verdict (session {sid_display})",
        f"  {'─' * 44}",
        f"  Overall:  {verdict.overall_score:6.3f}  [{verdict_word}]  ({verdict.method})",
        f"  {'─' * 44}",
    ]
    for key, score in verdict.dimension_scores.items():
        label = titles.get(key, key)
        lines.append(f"  {label:<24} {score:6.3f}")
    lines.append(f"  {'─' * 44}")
    if verdict.rationale:
        lines.append(f"  {verdict.rationale}")
    return "\n".join(lines)
