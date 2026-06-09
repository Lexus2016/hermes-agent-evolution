"""
Entropy-Based Evaluation of AI Agents (EEA) for Hermes.

Lightweight post-hoc analysis module that computes behavioral structure
metrics from session message histories.  No runtime dependency on the
agent loop — runs against the SQLite session DB or exported JSONL logs.

Metrics
-------
* action_entropy      – Shannon entropy over tool-type/action frequency
* trajectory_entropy  – Entropy over (prev_action, next_action) bigram counts
* tool_entropy        – Entropy over unique tool names used
* information_gain    – KL divergence in tool distribution vs baseline
* exploration_ratio   – Fraction of unique transitions vs total actions

Reference
---------
* arXiv:2606.05872 — Entropy-Based Evaluation of AI Agents
"""

import math
import statistics
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence


def _safe_entropy(counter: Counter, total: int) -> float:
    """Shannon entropy (base-2) from a Counter.

    Returns 0.0 if total is 0.
    """
    if total <= 0:
        return 0.0
    return -sum(
        (count / total) * math.log2(count / total)
        for count in counter.values()
    )


class SessionEntropyReport:
    """Container for entropy metrics of a single session."""

    def __init__(
        self,
        session_id: str,
        action_entropy: float,
        trajectory_entropy: float,
        tool_entropy: float,
        information_gain: float,
        exploration_ratio: float,
        action_counts: Dict[str, int],
        transition_counts: Dict[str, int],
    ):
        self.session_id = session_id
        self.action_entropy = action_entropy
        self.trajectory_entropy = trajectory_entropy
        self.tool_entropy = tool_entropy
        self.information_gain = information_gain
        self.exploration_ratio = exploration_ratio
        self.action_counts = action_counts
        self.transition_counts = transition_counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "action_entropy": round(self.action_entropy, 4),
            "trajectory_entropy": round(self.trajectory_entropy, 4),
            "tool_entropy": round(self.tool_entropy, 4),
            "information_gain": round(self.information_gain, 4),
            "exploration_ratio": round(self.exploration_ratio, 4),
            "action_counts": dict(self.action_counts),
            "transition_counts": dict(self.transition_counts),
        }


class EntropyEngine:
    """Compute entropy metrics from a session message list.

    Usage
    -----
        from agent.entropy_eval import EntropyEngine

        engine = EntropyEngine()
        report = engine.analyze(session_id, messages)
        print(report.to_dict())
    """

    def __init__(self, baseline_actions: Optional[Counter] = None):
        """
        Args:
            baseline_actions: Optional global tool/action frequency Counter
                used to compute *information_gain* (KL-divergence vs baseline).
                If None, the session's own distribution is used as baseline
                (information_gain will be 0.0).
        """
        self.baseline_actions = baseline_actions

    @staticmethod
    def _extract_actions(messages: Sequence[Dict[str, Any]]) -> List[str]:
        """Build a flat action list from messages.

        Each action is represented as ``tool:<name>`` or ``role:<role>`` if there
        is no tool call.  For assistant messages with multiple tool_calls we
        emit them in order followed by a synthetic ``tool:collective_result``
        marker so the loop structure is preserved.
        """
        actions: List[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content") or ""
            _tool_calls = msg.get("tool_calls") or []
            if role in ("user", "system"):
                actions.append(f"role:{role}")
                continue
            if role == "assistant":
                if _tool_calls:
                    actions.append("role:assistant(tool)")
                    for tc in _tool_calls:
                        name = tc.get("function", {}).get("name") or tc.get("name") or "unknown"
                        actions.append(f"tool:{name}")
                    actions.append("builtin:tool_result")
                else:
                    actions.append("role:assistant(text)")
                continue
            if role == "tool":
                actions.append(f"tool:result")
                continue
            actions.append(f"role:{role}")
        return actions

    def analyze(
        self,
        session_id: str,
        messages: Sequence[Dict[str, Any]],
        *,
        _internal_actions: Optional[List[str]] = None,
    ) -> SessionEntropyReport:
        """Compute entropy metrics for *messages*."""
        actions = _internal_actions if _internal_actions is not None else self._extract_actions(messages)
        total_actions = len(actions)

        # 1. action_entropy
        action_counter: Counter = Counter(actions)
        action_entropy = _safe_entropy(action_counter, total_actions)

        # 2. tool_entropy (over real tool names only)
        tool_names = [a.split(":", 1)[1] for a in actions if a.startswith("tool:")]
        tool_counter: Counter = Counter(tool_names)
        tool_entropy = _safe_entropy(tool_counter, len(tool_names))

        # 3. trajectory_entropy (bigram over all actions)
        transitions: Counter = Counter()
        for prev, nxt in zip(actions, actions[1:]):
            transitions[f"{prev} >> {nxt}"] += 1
        total_transitions = len(transitions)
        trajectory_entropy = _safe_entropy(transitions, total_transitions)

        # 4. exploration_ratio = |unique transitions| / total_actions
        exploration_ratio = total_transitions / max(total_actions, 1)

        # 5. information_gain  (KL divergence vs optional baseline)
        if self.baseline_actions and total_actions > 0:
            baseline_total = sum(self.baseline_actions.values())
            kl = 0.0
            for act, count in action_counter.items():
                p = count / total_actions
                q = max(self.baseline_actions.get(act, 0), 1) / (baseline_total + len(action_counter))
                if p > 0:
                    kl += p * math.log2(p / q)
            information_gain = kl
        else:
            information_gain = 0.0

        return SessionEntropyReport(
            session_id=session_id,
            action_entropy=action_entropy,
            trajectory_entropy=trajectory_entropy,
            tool_entropy=tool_entropy,
            information_gain=information_gain,
            exploration_ratio=exploration_ratio,
            action_counts=dict(action_counter),
            transition_counts=dict(transitions),
        )


class MultiSessionAggregator:
    """Aggregate entropy reports across multiple sessions."""

    @staticmethod
    def aggregate(reports: Sequence[SessionEntropyReport]) -> Dict[str, Any]:
        """Return mean / std-dev / max for each numeric field."""
        if not reports:
            return {}

        def stats(field_name: str) -> Dict[str, float]:
            values = [getattr(r, field_name) for r in reports]
            return {
                "mean": round(statistics.fmean(values), 4),
                "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
                "max": round(max(values), 4),
            }

        return {
            "action_entropy": stats("action_entropy"),
            "trajectory_entropy": stats("trajectory_entropy"),
            "tool_entropy": stats("tool_entropy"),
            "information_gain": stats("information_gain"),
            "exploration_ratio": stats("exploration_ratio"),
            "session_count": len(reports),
        }


def format_report_terminal(report: SessionEntropyReport) -> str:
    """Return a compact, terminal-ready summary string."""
    lines = [
        f"  🔬 Entropy Metrics (session {report.session_id[:24]}...)",
        f"  {'─' * 40}",
        f"  Action entropy:      {report.action_entropy:8.3f}",
        f"  Trajectory entropy:  {report.trajectory_entropy:8.3f}",
        f"  Tool entropy:        {report.tool_entropy:8.3f}",
        f"  Information gain:    {report.information_gain:8.3f}",
        f"  Exploration ratio:   {report.exploration_ratio:8.3f}",
        f"  {'─' * 40}",
    ]

    if report.tool_entropy > 2.0:
        lines.append(
            "  ⚠️  Tool entropy is high — agent uses many different tools."
        )
    if report.exploration_ratio < 0.1:
        lines.append(
            "  💡 Exploration is low — very repetitive action pattern."
        )
    if report.exploration_ratio > 0.8:
        lines.append(
            "  ⚠️  Exploration ratio is very high — chaotic or unfocused."
        )
    return "\n".join(lines)
