#!/usr/bin/env python3
"""Fixed step-level failure codebook and model-prelabelling for trace annotation (#3508).

Reference:
    Annotrace: A Workbench for Step-Level Annotation of Agent Traces
    (arXiv:2610.23284).

Provides:
  1. A written, machine-readable step-level failure codebook across the three
     canonical step types:
       - delegation (subagent/child task handoffs)
       - tool_call (environment tools: file, terminal, patch, etc.)
       - gate_decision (safety, evidence, regression, admission gates)
     Each step type possesses a mutually exclusive, exhaustive set of failure
     classes.
  2. Model-prelabelling with confidence scores: candidate steps are pre-labelled
     against the codebook, escalating only low-confidence labels (< 0.80) to
     human review.
  3. Cycle-comparable failure-class counts and suppression support for the
     selection stage to prevent re-filing already known failure classes.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

# Threshold below which an annotation is escalated to human review.
DEFAULT_CONFIDENCE_THRESHOLD = 0.80

STEP_TYPES: tuple[str, ...] = ("delegation", "tool_call", "gate_decision")

CODEBOOK: dict[str, dict[str, Any]] = {
    "delegation": {
        "description": "Steps delegating execution to subagents or child processes.",
        "failure_classes": {
            "timeout": {
                "description": "Delegated subagent/task exceeded execution time limit.",
                "keywords": ("timed out", "timeout", "deadline exceeded", "time limit exceeded"),
            },
            "context_overflow": {
                "description": "Context window or prompt token budget exceeded in delegated step.",
                "keywords": ("context length", "maximum context", "token limit", "prompt too long"),
            },
            "recursion_limit": {
                "description": "Maximum delegation depth exceeded or cyclical delegation loop detected.",
                "keywords": ("recursion limit", "delegation loop", "maximum depth", "cycle detected"),
            },
            "subagent_crash": {
                "description": "Subagent process terminated abnormally or raised an unhandled exception.",
                "keywords": ("subagent crashed", "process exited abnormally", "unhandled error in worker", "worker crashed"),
            },
            "contract_violation": {
                "description": "Subagent result violated the required return schema or format contract.",
                "keywords": ("contract violation", "schema validation failed", "missing required key", "malformed subagent response"),
            },
            "unauthorized_delegation": {
                "description": "Delegation blocked by policy, role restrictions, or missing permissions.",
                "keywords": ("unauthorized delegation", "disallowed role", "delegation forbidden", "permission denied for subagent"),
            },
            "other_delegation_failure": {
                "description": "Unclassified delegation failure.",
                "keywords": (),
            },
        },
    },
    "tool_call": {
        "description": "Steps invoking environment tools.",
        "failure_classes": {
            "precondition_failed": {
                "description": "Invocation preconditions unmet (e.g. file missing, invalid arguments, patch mismatch).",
                "keywords": (
                    "no such file",
                    "not found",
                    "does not exist",
                    "cannot find",
                    "patch-no-match",
                    "patch-indentation-mismatch",
                    "patch-ambiguous",
                    "old_string",
                    "invalid argument",
                    "valueerror",
                ),
            },
            "permission_denied": {
                "description": "Execution blocked by environment permissions, protected files, or safety guards.",
                "keywords": (
                    "permission denied",
                    "access denied",
                    "forbidden",
                    "write-sensitive-path",
                    "write-protected-instruction",
                    "safety-gate-blocked",
                    "eacces",
                    "operation not permitted",
                ),
            },
            "execution_error": {
                "description": "Execution failed with non-zero exit code or runtime command exception.",
                "keywords": ("non-zero exit", "exit code", "command failed", "execution exception", "syntaxerror"),
            },
            "timeout": {
                "description": "Tool operation timed out.",
                "keywords": ("timed out", "timeout", "deadline exceeded", "etimedout"),
            },
            "output_unparsable": {
                "description": "Output could not be parsed as valid JSON or expected envelope.",
                "keywords": ("jsondecodeerror", "parse error", "unparsable", "unexpected token"),
            },
            "quota_exceeded": {
                "description": "Rate limits, billing, or call quotas exhausted.",
                "keywords": ("rate limit", "too many requests", "quota", "429", "billing"),
            },
            "other_tool_failure": {
                "description": "Unclassified tool execution failure.",
                "keywords": (),
            },
        },
    },
    "gate_decision": {
        "description": "Steps evaluating automated validation, safety, or regression gates.",
        "failure_classes": {
            "evidence_mismatch": {
                "description": "Claim rejected because evidence was missing, unsupported, or of incompatible type.",
                "keywords": ("evidence_type_mismatch", "unbacked claim", "unsupported assertion", "no matching evidence"),
            },
            "safety_violation": {
                "description": "Rejected by safety gate for policy breach, prompt injection, or sensitive content.",
                "keywords": ("safety violation", "prompt injection", "sensitive content", "safety policy"),
            },
            "regression_detected": {
                "description": "Rejected by regression gate for failing tests or degraded benchmark metrics.",
                "keywords": ("regression detected", "test failure", "score decreased", "metric degraded"),
            },
            "policy_rejected": {
                "description": "Rejected for violating repo invariants, budget limits, or architectural rules.",
                "keywords": ("invariant violated", "budget overspent", "admission rejected", "policy check failed"),
            },
            "holdout_failed": {
                "description": "Failed generalization on the holdout evaluation set.",
                "keywords": ("holdout failed", "generalization failure", "overfitting"),
            },
            "other_gate_failure": {
                "description": "Unclassified gate decision failure.",
                "keywords": (),
            },
        },
    },
}


@dataclass(frozen=True)
class StepAnnotation:
    """A standardized, step-level failure annotation."""

    step_id: str
    step_type: str
    failure_class: str
    canonical_tag: str
    confidence: float
    escalated: bool
    rationale: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_step_failure_classes(step_type: str) -> list[str]:
    """Return the mutually exclusive failure classes for a given step type."""
    type_meta = CODEBOOK.get(step_type)
    if not type_meta:
        raise ValueError(f"Unknown step_type: {step_type!r}. Expected one of {STEP_TYPES}")
    return list(type_meta["failure_classes"].keys())


def is_valid_failure_class(step_type: str, failure_class: str) -> bool:
    """Check if a failure class belongs to the specified step type's codebook."""
    type_meta = CODEBOOK.get(step_type)
    if not type_meta:
        return False
    return failure_class in type_meta["failure_classes"]


def _extract_searchable_text(data: Any) -> str:
    """Extract a normalized string from various structured error formats."""
    if isinstance(data, str):
        return data.lower()
    if isinstance(data, dict):
        parts = []
        for key in ("error", "message", "status", "stderr", "output", "reason", "details", "failure_category"):
            val = data.get(key)
            if isinstance(val, str) and val:
                parts.append(val)
            elif isinstance(val, dict):
                parts.append(_extract_searchable_text(val))
        return " ".join(parts).lower()
    return ""


def classify_step_heuristics(step_dict: dict[str, Any], step_type: Optional[str] = None) -> tuple[str, float, str]:
    """Deterministic classifier implementing codebook rules over step metadata.

    Returns:
        (failure_class, confidence, rationale)
    """
    resolved_type = step_type or infer_step_type(step_dict)
    if resolved_type not in CODEBOOK:
        resolved_type = "tool_call"

    haystack = _extract_searchable_text(step_dict)
    classes_meta = CODEBOOK[resolved_type]["failure_classes"]

    # 1. Structural check for specific authoritative fields
    if resolved_type == "tool_call":
        exit_code = step_dict.get("exit_code")
        if exit_code is not None and str(exit_code) != "0":
            # Check if exit code is informational (e.g. grep 1 no matches)
            meaning = str(step_dict.get("exit_code_meaning", "")).lower()
            if "not an error" in meaning or "expected" in meaning:
                return "other_tool_failure", 0.70, "Informational non-zero exit code"

        # Explicit safety gate reason
        if step_dict.get("reason") == "safety-gate-blocked" or "tool execution blocked by safety gate" in haystack:
            return "permission_denied", 0.98, "Tool blocked by safety gate guard"

    elif resolved_type == "gate_decision":
        if "evidence" in haystack or "evidence_type_mismatch" in haystack:
            return "evidence_mismatch", 0.95, "Evidence verification mismatch"
        if "safety" in haystack:
            return "safety_violation", 0.95, "Gate identified safety violation"
        if "regression" in haystack:
            return "regression_detected", 0.95, "Gate identified regression in tests/metrics"
        if "holdout" in haystack:
            return "holdout_failed", 0.95, "Gate failed on holdout set"
        if "policy" in haystack or "invariant" in haystack or "budget" in haystack:
            return "policy_rejected", 0.92, "Gate rejected candidate on policy/invariants"

    elif resolved_type == "delegation":
        if "recursion" in haystack or "depth" in haystack:
            return "recursion_limit", 0.95, "Delegation recursion limit reached"
        if "context" in haystack or "token" in haystack:
            return "context_overflow", 0.95, "Subagent context length exceeded"
        if "timeout" in haystack or "deadline" in haystack:
            return "timeout", 0.95, "Subagent execution timed out"
        if "contract" in haystack or "schema" in haystack:
            return "contract_violation", 0.92, "Subagent result contract violation"
        if "permission" in haystack or "unauthorized" in haystack:
            return "unauthorized_delegation", 0.92, "Unauthorized delegation target"
        if "crash" in haystack or "abnormal" in haystack:
            return "subagent_crash", 0.90, "Subagent crashed during execution"

    # 2. Keyword scans in priority order (specific before generic)
    for f_class, meta in classes_meta.items():
        if f_class.startswith("other_"):
            continue
        for kw in meta.get("keywords", ()):
            if kw in haystack:
                # High confidence for distinct keyword hits
                return f_class, 0.88, f"Matched keyword {kw!r} for class {f_class}"

    # 3. Fallback to 'other_*' with lower confidence to trigger escalation
    catch_alls = {
        "delegation": "other_delegation_failure",
        "tool_call": "other_tool_failure",
        "gate_decision": "other_gate_failure",
    }
    catch_all = catch_alls.get(resolved_type, list(classes_meta.keys())[-1])
    return catch_all, 0.65, f"No specific failure pattern matched; fell back to catch-all {catch_all}"


def infer_step_type(step_dict: dict[str, Any]) -> str:
    """Infer the step type ('delegation', 'tool_call', 'gate_decision') from structure."""
    if "step_type" in step_dict and step_dict["step_type"] in STEP_TYPES:
        return str(step_dict["step_type"])

    # Probe delegation indicators
    if any(k in step_dict for k in ("subagent", "delegate_task", "child_task_id", "delegation")):
        return "delegation"
    if str(step_dict.get("tool_name", "")).startswith("delegate") or str(step_dict.get("tool", "")).startswith("delegate"):
        return "delegation"

    # Probe gate indicators
    if any(k in step_dict for k in ("gate", "gate_name", "verdict", "admission", "evaluation")):
        return "gate_decision"

    # Default to tool_call
    return "tool_call"


def sanitize_evidence(step_dict: dict[str, Any]) -> dict[str, Any]:
    """Sanitize evidence dictionary so raw user text/content is NEVER exposed (#89 privacy invariant)."""
    allowed_keys = {
        "step_id",
        "step_type",
        "tool",
        "tool_name",
        "reason",
        "exit_code",
        "exit_code_meaning",
        "status",
        "verdict",
        "failure_category",
        "gate_name",
    }
    safe: dict[str, Any] = {}
    for k, v in step_dict.items():
        if k in allowed_keys and isinstance(v, (str, int, float, bool)):
            safe[k] = v
    return safe


def prelabel_step(
    step_dict: dict[str, Any],
    *,
    step_type: Optional[str] = None,
    model_fn: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> StepAnnotation:
    """Pre-label an execution step against the codebook with a confidence score.

    Escalates low-confidence classifications (confidence < confidence_threshold)
    for human review.
    """
    stype = step_type or infer_step_type(step_dict)
    sid = str(step_dict.get("step_id") or step_dict.get("id") or step_dict.get("tool_call_id") or "step-unknown")

    # If an external model function is supplied, invoke it first.
    if model_fn is not None:
        try:
            m_res = model_fn(step_dict, CODEBOOK[stype])
            if isinstance(m_res, dict) and "failure_class" in m_res:
                f_class = str(m_res["failure_class"])
                if is_valid_failure_class(stype, f_class):
                    conf = float(m_res.get("confidence", 0.85))
                    conf = max(0.0, min(1.0, conf))
                    rationale = str(m_res.get("rationale") or "Model pre-labelling verdict")
                    escalated = conf < confidence_threshold
                    canonical = f"{stype}.{f_class}"
                    return StepAnnotation(
                        step_id=sid,
                        step_type=stype,
                        failure_class=f_class,
                        canonical_tag=canonical,
                        confidence=round(conf, 4),
                        escalated=escalated,
                        rationale=rationale,
                        evidence=sanitize_evidence(step_dict),
                    )
        except Exception:
            pass  # Fallback to deterministic heuristics on model error

    # Fallback / Deterministic classification
    f_class, conf, rationale = classify_step_heuristics(step_dict, step_type=stype)
    escalated = conf < confidence_threshold
    canonical = f"{stype}.{f_class}"

    return StepAnnotation(
        step_id=sid,
        step_type=stype,
        failure_class=f_class,
        canonical_tag=canonical,
        confidence=round(conf, 4),
        escalated=escalated,
        rationale=rationale,
        evidence=sanitize_evidence(step_dict),
    )


def prelabel_steps(
    steps: Iterable[dict[str, Any]],
    *,
    model_fn: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[StepAnnotation]:
    """Pre-label a collection of execution steps."""
    return [
        prelabel_step(step, model_fn=model_fn, confidence_threshold=confidence_threshold)
        for step in steps
        if isinstance(step, dict)
    ]


def aggregate_step_annotations(
    annotations: Iterable[StepAnnotation | dict[str, Any]],
) -> dict[str, int]:
    """Aggregate annotations into canonical count tallies (`<step_type>.<failure_class>`)."""
    counts: dict[str, int] = {}
    for ann in annotations:
        tag = ann.canonical_tag if isinstance(ann, StepAnnotation) else ann.get("canonical_tag")
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items()))


def compare_cycle_counts(
    previous_counts: dict[str, int], current_counts: dict[str, int]
) -> dict[str, Any]:
    """Compare failure-class counts across cycles to yield trend analytics.

    Returns a report detailing:
      - 'deltas': per-class absolute and relative changes
      - 'new_classes': classes present in current but not previous
      - 'resolved_classes': classes present in previous but zero in current
      - 'total_failures_previous': total failures in previous cycle
      - 'total_failures_current': total failures in current cycle
    """
    all_keys = sorted(set(previous_counts.keys()) | set(current_counts.keys()))
    deltas: dict[str, dict[str, Any]] = {}
    new_classes: list[str] = []
    resolved_classes: list[str] = []

    for k in all_keys:
        prev = previous_counts.get(k, 0)
        curr = current_counts.get(k, 0)
        diff = curr - prev
        pct_change = round((diff / prev * 100.0), 2) if prev > 0 else (100.0 if curr > 0 else 0.0)

        status = "unchanged"
        if prev == 0 and curr > 0:
            status = "new"
            new_classes.append(k)
        elif prev > 0 and curr == 0:
            status = "resolved"
            resolved_classes.append(k)
        elif diff > 0:
            status = "increased"
        elif diff < 0:
            status = "decreased"

        deltas[k] = {
            "previous": prev,
            "current": curr,
            "delta": diff,
            "percent_change": pct_change,
            "status": status,
        }

    return {
        "deltas": deltas,
        "new_classes": new_classes,
        "resolved_classes": resolved_classes,
        "total_failures_previous": sum(previous_counts.values()),
        "total_failures_current": sum(current_counts.values()),
    }


def load_suppression_registry(path: Path) -> dict[str, Any]:
    """Load the selection stage's filed failure-class registry."""
    if not path.is_file():
        return {}
    try:
        content = path.read_text(encoding="utf-8-sig")
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_suppression_registry(path: Path, registry: dict[str, Any]) -> None:
    """Save the selection stage's filed failure-class registry atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def is_class_suppressed(canonical_class: str, registry: dict[str, Any]) -> bool:
    """Determine whether a failure class is suppressed from re-filing."""
    if not registry:
        return False
    entry = registry.get(canonical_class)
    if not entry:
        return False
    if isinstance(entry, dict):
        status = str(entry.get("status", "")).lower()
        if status in ("filed", "in_progress", "active", "suppressed"):
            return True
        return False
    return bool(entry)


def record_filed_class(
    registry: dict[str, Any],
    canonical_class: str,
    *,
    issue_number: Optional[int] = None,
    cycle_id: Optional[str] = None,
    status: str = "filed",
) -> None:
    """Record an improvement issue or active task filing against a failure class."""
    registry[canonical_class] = {
        "canonical_class": canonical_class,
        "status": status,
        "issue_number": issue_number,
        "cycle_id": cycle_id,
    }


def suppress_filed_classes(
    current_counts: dict[str, int], registry: dict[str, Any]
) -> dict[str, int]:
    """Filter out failure classes that the selection stage has already filed.

    Returns the unsuppressed failure classes that still require action.
    """
    return {
        cls: count
        for cls, count in current_counts.items()
        if not is_class_suppressed(cls, registry)
    }


def main(argv: list[str]) -> int:
    """CLI tool for step-level failure annotation and codebook inspection."""
    if len(argv) < 2 or "--help" in argv or "-h" in argv:
        print("Usage:")
        print("  introspection_codebook.py codebook                  # Print codebook JSON")
        print("  introspection_codebook.py annotate <steps.json>     # Pre-label steps from file")
        print("  introspection_codebook.py compare <prev.json> <curr.json>  # Compare cycle counts")
        return 0

    cmd = argv[1]
    if cmd == "codebook":
        print(json.dumps(CODEBOOK, indent=2, sort_keys=True))
        return 0

    if cmd == "annotate" and len(argv) >= 3:
        p = Path(argv[2])
        if not p.is_file():
            print(f"File not found: {p}", file=sys.stderr)
            return 1
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        steps = data if isinstance(data, list) else data.get("steps", [])
        annotations = prelabel_steps(steps)
        counts = aggregate_step_annotations(annotations)
        escalated = [a.to_dict() for a in annotations if a.escalated]
        output = {
            "total_steps": len(annotations),
            "escalated_count": len(escalated),
            "counts": counts,
            "escalated_annotations": escalated,
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0

    if cmd == "compare" and len(argv) >= 4:
        p_prev = Path(argv[2])
        p_curr = Path(argv[3])
        d_prev = json.loads(p_prev.read_text(encoding="utf-8-sig"))
        d_curr = json.loads(p_curr.read_text(encoding="utf-8-sig"))
        report = compare_cycle_counts(d_prev, d_curr)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
