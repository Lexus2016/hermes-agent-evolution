#!/usr/bin/env python3
"""Rubric-based quality judges for the Hermes Evolution pipeline.

Evaluates the QUALITY of each cycle's output across 6 dimensions using two
swappable graders:

  StrictRubricJudgeGrader  — Deterministic, rule-based scoring (no LLM calls).
                             Runs as a no_agent cron job alongside the funnel.

  AgentJudgeGrader         — LLM-based qualitative assessment. Runs as an
                             LLM cron job that reads the strict scores and
                             produces narrative commentary.

Scorecard schema (both graders produce the same shape):

  {
    "cycle_date": "2026-06-23",
    "grader": "strict" | "agent",
    "dimensions": {
      "research":     {"score": float, "max": 10, "criteria": {...}},
      "issues":       {"score": float, "max": 8,  "criteria": {...}},
      "introspection": {"score": float, "max": 10, "criteria": {...}},
      "implementation": {"score": float, "max": 10, "criteria": {...}},
      "integration":  {"score": float, "max": 8,  "criteria": {...}},
      "pipeline_health": {"score": float, "max": 6,  "criteria": {...}},
    },
    "total_score": float,
    "total_max": 52,
    "overall_percentage": float,
    "flags": [str],
  }
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────
# Rubric definition — 6 dimensions, 20 criteria, max 52 points
# ──────────────────────────────────────────────────────────────────────

RUBRIC_DIMENSIONS: Dict[str, Dict[str, Any]] = {
    "research": {
        "max": 10,
        "criteria": {
            "coverage": {
                "max": 3,
                "label": "Source coverage — multiple competitive sources, papers, trends",
            },
            "actionability": {
                "max": 3,
                "label": "Concrete proposals with priority/effort scores",
            },
            "depth": {
                "max": 2,
                "label": "Backed by URLs, code snippets, architecture details",
            },
            "signal_vs_noise": {
                "max": 2,
                "label": "Substance-to-length ratio; focused on relevant, high-impact findings",
            },
        },
    },
    "issues": {
        "max": 8,
        "criteria": {
            "priority_distribution": {
                "max": 2,
                "label": "Issues scored with meaningful priority/effort",
            },
            "self_critique": {
                "max": 2,
                "label": "Low-quality proposals rejected with explicit reasoning",
            },
            "labeling": {
                "max": 2,
                "label": "Proper label assignment (fix/enhancement/proposal/…)",
            },
            "dedup_awareness": {
                "max": 2,
                "label": "Cross-references existing issues to avoid duplicates",
            },
        },
    },
    "introspection": {
        "max": 10,
        "criteria": {
            "session_coverage": {
                "max": 2,
                "label": "Sessions scanned relative to window size",
            },
            "signal_quality": {
                "max": 3,
                "label": "Clear, actionable patterns identified with supporting data",
            },
            "cross_referencing": {
                "max": 2,
                "label": "Findings reference tracked issue numbers",
            },
            "action_proposals": {
                "max": 3,
                "label": "New issues proposed with impact/effort scores",
            },
        },
    },
    "implementation": {
        "max": 10,
        "criteria": {
            "scope_discipline": {
                "max": 3,
                "label": "Implementation matches what analysis selected",
            },
            "test_presence": {
                "max": 2,
                "label": "Tests added or updated",
            },
            "documentation": {
                "max": 2,
                "label": "Implementation documented explicitly",
            },
            "diff_quality": {
                "max": 3,
                "label": "Clean diff — no debug code, no unrelated changes",
            },
        },
    },
    "integration": {
        "max": 8,
        "criteria": {
            "ci_verification": {
                "max": 2,
                "label": "CI checks verified green before merge",
            },
            "merge_discipline": {
                "max": 2,
                "label": "Limited merges per run; only evolution/* branches",
            },
            "self_update": {
                "max": 2,
                "label": "hermes update --yes run after merge",
            },
            "conflict_handling": {
                "max": 2,
                "label": "Conflicts merged gracefully",
            },
        },
    },
    "pipeline_health": {
        "max": 6,
        "criteria": {
            "stage_completeness": {
                "max": 2,
                "label": "Proportion of expected stages that produced output",
            },
            "freshness": {
                "max": 2,
                "label": "Output dates match the cycle date",
            },
            "failure_awareness": {
                "max": 2,
                "label": "Failure rates acknowledged and reported in outputs",
            },
        },
    },
}


def _total_max() -> int:
    return sum(dim["max"] for dim in RUBRIC_DIMENSIONS.values())


# Generic-judge baseline for the held-out gate (#2782): the shipped rubric
# stated as text the keyword-requirements judge can apply — its criteria
# labels are the requirements the generic judge has always enforced.
_SHIPPED_RUBRIC_TEXT = "\n".join(
    f"{dim}:\n" + "\n".join(f"  {c['label']}" for c in spec["criteria"].values())
    for dim, spec in RUBRIC_DIMENSIONS.items()
)


def _keyword_requirements_judge(rubric_text: str, example: Any) -> bool:
    """Deterministic default judge for RubricForge agreement (#2780).

    A labeled example is ``{"requires": [..keywords..], "forbids": [..]}``:
    the rubric accepts it when it mentions every required keyword and at
    least one forbidden one. ``label: True`` examples encode requirements a
    good rubric must state; ``label: False`` examples encode anti-requirements
    (a rubric containing them disagrees and loses agreement).
    """
    ex = example if isinstance(example, dict) else {}
    requires = [k for k in (ex.get("requires") or []) if isinstance(k, str)]
    forbids = [k for k in (ex.get("forbids") or []) if isinstance(k, str)]
    verdict = all(k in rubric_text for k in requires)
    if forbids:
        verdict = verdict and any(k in rubric_text for k in forbids)
    return bool(verdict)


def resolve_active_rubric(evolution_dir: Path) -> Optional[Dict[str, Any]]:
    """RubricForge consumer (#2780): pick the agreement-maximizing rubric.

    Reads ``rubric-forge/candidates.json`` (list of rubric texts — each may
    carry ``<dimension>: <int>`` override lines) and ``rubric-forge/labeled.json``
    (keyword-requirement examples with ground-truth labels), selects the best
    candidate via :func:`evolution.lib.rubric_forge.select_best_rubric`,
    records the verdict to ``rubric-forge/selected.json`` for audit, and
    returns ``{"rubric", "agreement", "max_overrides"}``. Returns ``None``
    when either input is absent — the judge then runs the shipped rubric,
    byte-identical to pre-RubricForge behavior.
    """
    rf_dir = evolution_dir / "rubric-forge"
    candidates = _load_json(rf_dir / "candidates.json")
    labeled = _load_json(rf_dir / "labeled.json")
    if not isinstance(candidates, list) or not candidates:
        return None
    if not isinstance(labeled, list) or not labeled:
        return None
    try:
        from evolution.lib.rubric_forge import select_best_rubric
    except ImportError:
        # Standalone-script deployment (HERMES_HOME/scripts without the repo
        # tree): the primitive is unavailable — run the shipped rubric.
        return None

    labels = [
        bool(e.get("label")) if isinstance(e, dict) else False for e in labeled
    ]
    best_text, agreement = select_best_rubric(
        [str(c) for c in candidates], labeled, labels, _keyword_requirements_judge
    )
    max_overrides: Dict[str, float] = {}
    for line in best_text.splitlines():
        m = re.match(r"^([a-z_]+)\s*:\s*(\d+)\s*$", line.strip())
        if m and m.group(1) in RUBRIC_DIMENSIONS:
            max_overrides[m.group(1)] = float(m.group(2))
    # Held-out false-pass gate (#2782): the winner was selected ON this
    # labeled set, so before its overrides become the ACTIVE rubric it must
    # prove false-pass parity on a held-out tail it never saw. Refusal
    # means the shipped rubric runs (recorded as gate=blocked).
    gate_verdict = None
    active_text, active_agreement, active_overrides = best_text, agreement, max_overrides
    try:
        from evolution.lib.rubric_heldout_gate import held_out_false_pass_gate

        gate_verdict = held_out_false_pass_gate(
            best_text,
            _SHIPPED_RUBRIC_TEXT,
            labeled,
            labels,
            _keyword_requirements_judge,
        )
        if not gate_verdict.adopt:
            active_text, active_agreement, active_overrides = "", 0.0, {}
    except ImportError:
        gate_verdict = None  # standalone deployment — no gate, run the winner

    try:
        rf_dir.mkdir(parents=True, exist_ok=True)
        (rf_dir / "selected.json").write_text(
            json.dumps(
                {
                    "rubric": active_text[:2000],
                    "agreement": active_agreement,
                    "max_overrides": active_overrides,
                    "held_out_gate": {
                        "adopt": gate_verdict.adopt,
                        "evolved_false_pass_rate": gate_verdict.evolved_false_pass_rate,
                        "baseline_false_pass_rate": gate_verdict.baseline_false_pass_rate,
                        "held_out_size": gate_verdict.held_out_size,
                        "reason": gate_verdict.reason,
                    }
                    if gate_verdict is not None
                    else None,
                    "selected_at": datetime.now().isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    if not active_text:
        return None  # gate blocked — the judge runs the shipped rubric
    return {
        "rubric": active_text,
        "agreement": active_agreement,
        "max_overrides": active_overrides,
        "held_out_gate": gate_verdict,
    }


def _hot_path(evolution_dir: Path) -> Path:
    """Canonical path: $EVOLUTION_PROFILE_DIR or ~/.hermes/evolution."""
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "")
    if env:
        return Path(env)
    return Path.home() / ".hermes" / "evolution"


# ──────────────────────────────────────────────────────────────────────
# Helpers — safe JSON/MD loading
# ──────────────────────────────────────────────────────────────────────


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_md(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _count_matches(text: str, pattern: str) -> int:
    """Count non-overlapping regex matches in text."""
    return len(re.findall(pattern, text))


def _bool(v: Any) -> bool:
    return bool(v) if v is not None else False


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ──────────────────────────────────────────────────────────────────────
# StrictRubricJudgeGrader — deterministic, rule-based, no LLM
# ──────────────────────────────────────────────────────────────────────


class StrictRubricJudgeGrader:
    """Score each dimension by parsing the structured outputs with explicit rules.

    Every method returns ``{criterion_name: score}``.  Missing stages produce
    zeroes — the stage-completeness criterion in pipeline_health catches them.
    """

    # ── Research ──────────────────────────────────────────────────────

    def _score_research(self, text: str | None) -> Dict[str, float]:
        tx = text or ""
        # coverage: count unique-looking URLs / cited sources
        urls = set(re.findall(r"https?://\S+", tx))
        # Also count "##" section headers as topical areas
        sections = len(re.findall(r"^##\s+\d+\.", tx, re.MULTILINE))
        coverage = min(3, (len(urls) // 4) + (sections // 3))

        # actionability: count mentions of priority_score, effort, Impact
        priority_matches = _count_matches(
            tx, r"(?i)priority[_\s]*(?:score)?[\s:=]+[\d.]+"
        )
        effort_matches = _count_matches(tx, r"(?i)effort[\s:=]+[\d.]+")
        actionability = min(3, (priority_matches + effort_matches) // 3)

        # depth: presence of code blocks + well-formed URLs per proposal
        code_blocks = _count_matches(tx, r"```")
        depth = 0
        if code_blocks >= 2:
            depth += 1
        if len(urls) >= 3:
            depth += 1

        # signal_vs_noise: rough heuristic — if the first ~30 lines include
        # specifics (URLs, scores, code), it's signal-rich.
        first_30 = "\n".join(tx.split("\n")[:30])
        signal_indicators = (
            _count_matches(first_30, r"https?://")
            + _count_matches(first_30, r"(?i)score")
            + _count_matches(first_30, r"```")
        )
        signal_vs_noise = min(2, signal_indicators // 3)

        return {
            "coverage": float(coverage),
            "actionability": float(actionability),
            "depth": float(depth),
            "signal_vs_noise": float(signal_vs_noise),
        }

    # ── Issues ────────────────────────────────────────────────────────

    def _score_issues(self, data: Dict[str, Any]) -> Dict[str, float]:
        issues = data.get("issues") or []
        meta = _as_dict(data.get("meta"))

        # priority_distribution: all issues have priority_score > 0
        scored = sum(1 for i in issues if _safe_int(i.get("priority_score", 0)) > 0)
        if not issues:
            priority_distribution = 0.0
        elif scored == len(issues):
            priority_distribution = 2.0
        elif scored >= len(issues) // 2:
            priority_distribution = 1.0
        else:
            priority_distribution = 0.0

        # self_critique: rejected_self_critique count + detail messages
        rej_self = _safe_int(meta.get("rejected_self_critique"))
        rej_details = meta.get("rejected_details") or {}
        if rej_self > 0 and len(rej_details) >= 2:
            self_critique = 2.0
        elif rej_self > 0 or len(rej_details) > 0:
            self_critique = 1.0
        else:
            self_critique = 0.0

        # labeling: each issue has 2+ labels
        well_labeled = sum(1 for i in issues if len(i.get("labels") or []) >= 2)
        if not issues:
            labeling = 0.0
        elif well_labeled == len(issues):
            labeling = 2.0
        elif well_labeled >= len(issues) // 2:
            labeling = 1.0
        else:
            labeling = 0.0

        # dedup_awareness: rejected_duplicate or cross-refs
        rej_dup = _safe_int(meta.get("rejected_duplicate"))
        cross_ref = 0
        for i in issues:
            title = str(i.get("title", ""))
            if "#" in title:
                cross_ref += 1
        if rej_dup > 0 or cross_ref >= len(issues) // 2:
            dedup_awareness = 2.0
        elif rej_details and any(
            "duplicate" in str(v).lower() for v in rej_details.values()
        ):
            dedup_awareness = 1.0
        else:
            dedup_awareness = 0.0

        return {
            "priority_distribution": priority_distribution,
            "self_critique": self_critique,
            "labeling": labeling,
            "dedup_awareness": dedup_awareness,
        }

    # ── Introspection ─────────────────────────────────────────────────

    def _score_introspection(self, data: Any) -> Dict[str, float]:
        d = _as_dict(data) if data is not None else {}

        # session_coverage
        scanned = _safe_int(d.get("sessions_scanned"))
        window = _safe_int(d.get("window_days", 1))
        expected = window * 3  # rough proxy: ~3 sessions per day per profile
        if scanned >= expected:
            session_coverage = 2.0
        elif scanned > 0:
            session_coverage = 1.0
        else:
            session_coverage = 0.0

        # signal_quality: distinct signals with rich observations
        signals = _as_dict(d.get("signals"))
        good_signals = sum(
            1
            for s in signals.values()
            if isinstance(s, dict) and len(str(s.get("observation", ""))) > 80
        )
        signal_quality = min(3.0, good_signals)

        # cross_referencing: signals refer to tracked issues
        ref_count = sum(
            1
            for s in signals.values()
            if isinstance(s, dict) and _safe_int(s.get("tracked_in_issue"))
        )
        total_signals = len(signals)
        if total_signals > 0 and ref_count == total_signals:
            cross_referencing = 2.0
        elif ref_count > 0:
            cross_referencing = 1.0
        else:
            cross_referencing = 0.0

        # action_proposals: new_issues_proposed with impact/effort
        proposals = d.get("new_issues_proposed") or []
        scored_props = sum(
            1
            for p in proposals
            if isinstance(p, dict) and p.get("priority_score") is not None
        )
        action_proposals = min(3.0, scored_props)

        return {
            "session_coverage": float(session_coverage),
            "signal_quality": float(signal_quality),
            "cross_referencing": float(cross_referencing),
            "action_proposals": float(action_proposals),
        }

    # ── Implementation ────────────────────────────────────────────────

    def _score_implementation(self, text: str | None) -> Dict[str, float]:
        tx = text or ""

        # scope_discipline: mentions issue # / PR #
        issue_refs = _count_matches(
            tx, r"(?i)(?:issue|fix|closes|implements|PR[:\s]*#)\s*#?\d+"
        )
        scope_discipline = min(3.0, issue_refs)

        # test_presence: mentions tests
        test_mentions = _count_matches(tx, r"(?i)\b(?:test|tests|testing|pytest)\b")
        test_presence = (
            2.0 if test_mentions >= 2 else (1.0 if test_mentions >= 1 else 0.0)
        )

        # documentation: mentions documentation
        doc_mentions = _count_matches(
            tx, r"(?i)\b(?:doc|docs|documentation|documented)\b"
        )
        documentation = (
            2.0 if doc_mentions >= 2 else (1.0 if doc_mentions >= 1 else 0.0)
        )

        # diff_quality: diff size is clean and reported
        diff_mentions = _count_matches(
            tx, r"(?i)(?:\d+\s+(?:insertions?|deletions?|files?\s+changed))"
        )
        diff_total = 0
        for m in re.findall(r"(\d+)\s+(?:insertions?|deletions?)", tx):
            diff_total += int(m)
        if diff_mentions >= 2 and diff_total < 100:
            diff_quality = 3.0
        elif diff_mentions >= 1:
            diff_quality = 2.0 if diff_total < 500 else 1.0
        else:
            diff_quality = 0.0

        return {
            "scope_discipline": float(scope_discipline),
            "test_presence": test_presence,
            "documentation": documentation,
            "diff_quality": diff_quality,
        }

    # ── Integration ───────────────────────────────────────────────────

    def _score_integration(self, data: Dict[str, Any] | None) -> Dict[str, float]:
        d = _as_dict(data) if data else {}
        tx = json.dumps(d)

        # ci_verification: CI/checks mentioned as green
        ci_green = _count_matches(
            tx, r"(?i)\b(?:green|passing|checks?\s+pass|ci\s+ok)\b"
        )
        ci_verification = 2.0 if ci_green >= 2 else (1.0 if ci_green >= 1 else 0.0)

        # merge_discipline: merges limited, evolution/* pattern
        branch_pattern = _count_matches(tx, r"(?i)evolution/")
        limit_mentions = _count_matches(tx, r"(?i)\b(?:max|limit)\s+\d+\s+(?:merge|pr)")
        score = 0
        if branch_pattern >= 1:
            score += 1
        if limit_mentions >= 1 or _safe_int(d.get("merged_count", 0)) <= 5:
            score += 1
        merge_discipline = float(score)

        # self_update: hermes update mentioned
        self_update = 2.0 if _count_matches(tx, r"(?i)hermes\s+update") >= 1 else 0.0

        # conflict_handling: conflicts resolved gracefully
        conflict_mentions = _count_matches(
            tx, r"(?i)\b(?:conflict|merge\s+conflict|rebase|resolved)\b"
        )
        conflict_handling = min(2.0, conflict_mentions)

        return {
            "ci_verification": ci_verification,
            "merge_discipline": merge_discipline,
            "self_update": self_update,
            "conflict_handling": conflict_handling,
        }

    # ── Pipeline Health ───────────────────────────────────────────────

    def _score_pipeline_health(
        self,
        date: str,
        evolution_dir: Path,
    ) -> Dict[str, float]:
        stages = {
            "research": "research",
            "issues": "issues",
            "introspection": "introspection",
            "implementation": "implementation",
        }
        # integration and analysis are excluded from stage_completeness since
        # they may not produce output every cycle (analysis writes structured
        # JSON but depends on issues existing; integration depends on PRs to
        # merge).

        present = 0
        total = len(stages)
        for name, subdir in stages.items():
            candidate = evolution_dir / subdir / f"{date}.md"
            if candidate.is_file():
                present += 1
                continue
            candidate = evolution_dir / subdir / f"{date}.json"
            if candidate.is_file():
                present += 1

        ratio = present / total if total > 0 else 0.0
        if ratio >= 0.75:
            stage_completeness = 2.0
        elif ratio >= 0.5:
            stage_completeness = 1.0
        else:
            stage_completeness = 0.0

        # freshness: sample a couple of outputs and check their dates
        research_md = _load_md(evolution_dir / "research" / f"{date}.md") or ""
        issues_json = _load_json(evolution_dir / "issues" / f"{date}.json")
        introspection_json = _load_json(
            evolution_dir / "introspection" / f"{date}.json"
        )
        date_hits = 0
        date_checks = 0
        for source_name, data in [
            ("research", research_md),
            ("issues", issues_json),
            ("introspection", introspection_json),
        ]:
            if isinstance(data, str) and date in data:
                date_hits += 1
                date_checks += 1
            elif isinstance(data, dict):
                for val in data.values():
                    if isinstance(val, str) and date in str(val):
                        date_hits += 1
                        break
                date_checks += 1
        if date_checks == 0:
            freshness = 0.0
        elif date_hits == date_checks:
            freshness = 2.0
        elif date_hits > 0:
            freshness = 1.0
        else:
            freshness = 0.0

        # failure_awareness: any output mentions failure rates
        combined = (
            research_md
            + json.dumps(issues_json or {})
            + json.dumps(introspection_json or {})
        )
        failure_mentions = _count_matches(
            combined,
            r"(?i)\b(?:fail|failures?|error|retry|timeout|failure.rate)\b",
        )
        failure_awareness = min(2.0, failure_mentions // 3)

        return {
            "stage_completeness": stage_completeness,
            "freshness": freshness,
            "failure_awareness": float(failure_awareness),
        }

    # ── Score all dimensions ─────────────────────────────────────────

    def score(self, date: str, evolution_dir: Path | None = None) -> Dict[str, Any]:
        if evolution_dir is None:
            evolution_dir = _hot_path(Path("."))

        # Load all stage outputs (gracefully — missing = empty)
        research_md = _load_md(evolution_dir / "research" / f"{date}.md")
        issues_json = _as_dict(_load_json(evolution_dir / "issues" / f"{date}.json"))
        introspection_data = _load_json(
            evolution_dir / "introspection" / f"{date}.json"
        )
        implementation_md = _load_md(evolution_dir / "implementation" / f"{date}.md")
        integration_json = _as_dict(
            _load_json(evolution_dir / "integration" / f"{date}.json")
        )

        # Score each dimension
        research_scores = self._score_research(research_md)
        issues_scores = self._score_issues(issues_json)
        introspection_scores = self._score_introspection(introspection_data)
        implementation_scores = self._score_implementation(implementation_md)
        integration_scores = self._score_integration(integration_json)
        health_scores = self._score_pipeline_health(date, evolution_dir)

        # RubricForge (#2780): if candidate rubrics + a labeled set exist in
        # the evolution profile, the agreement-maximizing candidate's
        # dimension-max overrides are the ACTIVE rubric for this scoring run.
        active_rubric = resolve_active_rubric(evolution_dir)
        max_overrides = (active_rubric or {}).get("max_overrides") or {}

        # Build dimension records
        def _build_dimension(dim_name: str, scores: Dict[str, float]) -> Dict[str, Any]:
            dim_def = RUBRIC_DIMENSIONS[dim_name]
            total = sum(scores.values())
            return {
                "score": round(total, 1),
                "max": float(max_overrides.get(dim_name, dim_def["max"])),
                "criteria": scores,
            }

        dimensions = {
            "research": _build_dimension("research", research_scores),
            "issues": _build_dimension("issues", issues_scores),
            "introspection": _build_dimension("introspection", introspection_scores),
            "implementation": _build_dimension("implementation", implementation_scores),
            "integration": _build_dimension("integration", integration_scores),
            "pipeline_health": _build_dimension("pipeline_health", health_scores),
        }

        total_score = sum(d["score"] for d in dimensions.values())
        total_max = sum(d["max"] for d in dimensions.values())
        pct = round((total_score / total_max) * 100, 1) if total_max > 0 else 0.0

        raw_claims = extract_claims(
            ("research", research_md),
            ("implementation", implementation_md),
            ("integration", json.dumps(integration_json)),
        )
        claims = triage_claims(raw_claims)
        claims = assess_stat_validity_claims(claims)
        claim_stats = compute_claim_score(claims, total_max)
        stat_stats = compute_stat_validity_stats(claims)

        # Derive aggregate score from claim verdicts when claims are present
        if claims:
            total_score = claim_stats["score"]
            pct = claim_stats["overall_percentage"]

        # Generate flags for concerning signals
        flags: List[str] = []
        if pct < 30:
            flags.append("CRITICAL: overall quality < 30% — most stages failing")
        elif pct < 50:
            flags.append("LOW_QUALITY: overall quality < 50% — significant gaps")
        elif pct < 70:
            flags.append("MODERATE: overall quality < 70% — room for improvement")
        for dim_name, dim_data in dimensions.items():
            dim_pct = (
                (dim_data["score"] / dim_data["max"]) * 100
                if dim_data["max"] > 0
                else 0
            )
            if dim_pct < 20:
                flags.append(
                    f"{dim_name.upper()}_DIM: {dim_data['score']}/{dim_data['max']} "
                    f"({dim_pct:.0f}%) — stage nearly absent or very poor"
                )

        # Slice C: Rejection-bias (post-hoc rationalization) detection
        rejection_bias_detections = detect_rejection_bias([
            ("research", research_md),
            ("implementation", implementation_md),
            ("integration", json.dumps(integration_json)),
        ])
        rejection_bias_flag = len(rejection_bias_detections) > 0
        if rejection_bias_flag:
            flags.append(
                f"REJECTION_BIAS: {len(rejection_bias_detections)} post-hoc rationalization pattern(s) detected"
            )

        if stat_stats["suspect"]:
            flags.append(
                f"STAT_VALIDITY: {stat_stats['suspect']} suspect quantitative claim(s)"
            )

        return {
            "cycle_date": date,
            "grader": "strict",
            "dimensions": dimensions,
            "total_score": total_score,
            "total_max": total_max,
            "overall_percentage": pct,
            "flags": flags,
            "rubric_forge": active_rubric,
            "claims": claims,
            "claim_verdicts": claim_stats["verdict_counts"] if claims else {},
            "stat_validity": stat_stats,
            "rejection_bias": rejection_bias_detections,
            "rejection_bias_flag": rejection_bias_flag,
        }


# ──────────────────────────────────────────────────────────────────────
# AgentJudgeGrader — LLM-based qualitative assessment
# ──────────────────────────────────────────────────────────────────────


class AgentJudgeGrader:
    """LLM-backed grader that loads the strict scores, reads the raw outputs,
    and produces narrative commentary plus adjusted scores.

    This grader is designed to be invoked FROM an LLM session — not as a
    no_agent script.  The LLM prompt should:

      1. Load this module.
      2. Instantiate ``AgentJudgeGrader(evolution_dir, date)``.
      3. Read the stage outputs via ``load_outputs()``.
      4. Call ``narrative_assessment()`` with the raw outputs to generate
         subjective commentary per dimension.
      5. Call ``adjust_scores(strict_scores, commentary)`` to produce final
         scored + narrative judgment.

    The class provides structured methods so the LLM has a clear contract
    to follow, rather than writing free-form prose.
    """

    def __init__(
        self,
        evolution_dir: Path,
        cycle_date: str,
    ):
        self.evolution_dir = evolution_dir
        self.cycle_date = cycle_date
        self.strict_grader = StrictRubricJudgeGrader()

    def load_outputs(self) -> Dict[str, Any]:
        """Return raw output content for every stage, fully loaded, for a
        given date.  Missing stages return None so the LLM can still comment
        on the absence."""
        date = self.cycle_date
        dir = self.evolution_dir
        return {
            "research": _load_md(dir / "research" / f"{date}.md"),
            "issues": _load_json(dir / "issues" / f"{date}.json"),
            "introspection": _load_json(dir / "introspection" / f"{date}.json"),
            "implementation": _load_md(dir / "implementation" / f"{date}.md"),
            "integration": _load_json(dir / "integration" / f"{date}.json"),
            "analysis": _load_json(dir / "analysis" / f"{date}.json"),
        }

    def get_strict_baseline(self) -> Dict[str, Any]:
        """Get the deterministic strict scores as a baseline for the
        LLM to adjust."""
        return self.strict_grader.score(self.cycle_date, self.evolution_dir)

    def narrative_assessment(self, outputs: Dict[str, Any]) -> Dict[str, str]:
        """Template for LLM output: per-dimension narrative commentary.

        The LLM should fill this dict by reading each stage's output and
        providing a short, specific assessment (2-5 sentences per dimension).
        """
        return {
            "research": "",
            "issues": "",
            "introspection": "",
            "implementation": "",
            "integration": "",
            "pipeline_health": "",
        }

    def adjust_scores(
        self,
        strict: Dict[str, Any],
        narratives: Dict[str, str],
    ) -> Dict[str, Any]:
        """After the LLM sets ``narratives`` keys, call this to merge them
        into the final scorecard.  The LLM can optionally tweak per-criterion
        scores via the narrative (the adjustment logic here is minimal by
        design — the strict scores are the backbone; the LLM adds context).

        Returns a scorecard identical in shape to ``StrictRubricJudgeGrader.score()``
        but with ``grader: "agent"`` and a ``narratives`` key added.
        """
        scorecard = dict(strict)  # copy
        scorecard["grader"] = "agent"
        scorecard["narratives"] = narratives
        return scorecard


# ──────────────────────────────────────────────────────────────────────
# Claim extraction — machine-readable, deterministic (#2482 Slice A)
# ──────────────────────────────────────────────────────────────────────

# Outcome signals marking a sentence as a claim (an asserted achievement /
# enablement / measured result) rather than neutral prose. Kept to strong,
# measurable words so extraction is stable and doesn't over-fire.
_CLAIM_SIGNALS = (
    "improve",
    "reduced",
    "reduce",
    "increase",
    "faster",
    "speedup",
    "latency",
    "enables",
    "enabled",
    "achiev",
    "fixes",
    "fixed",
    "resolves",
    "merged",
    "%",
    "percent",
    "seconds",
    "tokens",
    "cost",
)

_CLAIM_URL_RE = re.compile(r"https?://\S+")


def _markdown_sections(text: str) -> List[Tuple[str, str]]:
    """Split markdown into ``(section_header, body)`` blocks in order;
    text before the first ``#`` header is an unnamed preamble block."""
    blocks: List[Tuple[str, str]] = []
    current: List[str] = []
    current_header = ""
    for line in text.splitlines():
        if line.startswith("#") and line.strip("# "):
            if current:
                blocks.append((current_header, "\n".join(current)))
            current_header = line.strip("# ").strip()
            current = []
        else:
            current.append(line)
    if current:
        blocks.append((current_header, "\n".join(current)))
    return [(h.strip(), b) for h, b in blocks if b.strip()]


def _split_sentences(block: str) -> List[str]:
    """Deterministic per-line sentence splitter for markdown prose."""
    sentences: List[str] = []
    for raw in block.splitlines():
        raw = raw.strip().lstrip("-* \t")
        if not raw:
            continue
        for p in re.split(r"(?<=[.!?])\s+(?=[A-Z\"#(])", raw):
            p = p.strip().rstrip(".").strip()
            if len(p) >= 20:
                sentences.append(p)
    return sentences


def _is_claim_sentence(sentence: str) -> bool:
    low = sentence.lower()
    return any(sig in low for sig in _CLAIM_SIGNALS)


def extract_claims(
    *artifacts: Tuple[str, Optional[str]],
    max_per_source: int = 12,
) -> List[Dict[str, Any]]:
    """Deterministically extract discrete claims from judged artifacts.

    A claim is a sentence asserting an outcome (improvement, reduction,
    enablement, a merged/fixed issue, a measured result), tagged with its
    source stage + section and any supporting URL. Returns a machine-readable
    list of ``{"claim", "source", "evidence_url"}`` dicts, deduplicated and
    sorted so identical input always yields identical output (Slice A
    determinism). ``artifacts`` are ``(stage, markdown_text)`` pairs.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for stage, text in artifacts:
        if not text:
            continue
        per_source = 0
        for header, block in _markdown_sections(text):
            for sentence in _split_sentences(block):
                if per_source >= max_per_source:
                    break
                if not _is_claim_sentence(sentence):
                    continue
                urls = _CLAIM_URL_RE.findall(sentence)
                key = (stage, header, sentence)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "claim": sentence,
                    "source": {
                        "stage": stage,
                        "section": header or "preamble",
                    },
                    "evidence_url": urls[0] if urls else None,
                })
                per_source += 1
    out.sort(key=lambda c: (c["source"]["stage"], c["source"]["section"], c["claim"]))
    return out


# ──────────────────────────────────────────────────────────────────────
# Claim triage taxonomy — Slice B (#2482 / #2514)
# ──────────────────────────────────────────────────────────────────────

CLAIM_VERDICTS: Tuple[str, ...] = ("verified", "falsified", "toy-scale", "no-evidence")

CLAIM_VERDICT_WEIGHTS: Dict[str, float] = {
    "verified": 1.0,
    "toy-scale": 0.3,
    "no-evidence": 0.0,
    "falsified": 0.0,
}

_FALSIFIED_RE = re.compile(
    r"\b(failed|falsified|broken|regression|reverted|0\s*passed|error|conflict|timeout|crash|fails)\b",
    re.IGNORECASE,
)
_TOY_SCALE_RE = re.compile(
    r"\b(toy|mock|stub|synthetic|dummy|trivial|placeholder|sample\s*only|minimal\s*test|toy-scale)\b",
    re.IGNORECASE,
)
_VERIFIED_METRIC_RE = re.compile(
    r"(\d+(?:\.\d+)?%|\d+\s*ms|\d+\s*s\b|\d+\s*seconds|\d+\s*tokens?|\d+\s*lines?|\d+x\b|issue\s*#\d+|pr\s*#\d+|#\d+|\bmerged\b|\bfixed\b|\bresolves\b)",
    re.IGNORECASE,
)


def triage_claim(claim_item: Dict[str, Any]) -> Dict[str, Any]:
    """Classify an extracted claim into the 4-verdict triage taxonomy (#2514).

    Verdicts:
      - ``falsified``: claim asserts a failure, regression, conflict, or refuted outcome.
      - ``toy-scale``: claim relies on synthetic, mock, or toy-scale demonstration without production proof.
      - ``verified``: claim is supported by an evidence URL, tracked PR/issue, or concrete measured metrics.
      - ``no-evidence``: assertive outcome statement without backing URL, commit, or numerical evidence.

    Returns a shallow copy of ``claim_item`` with ``verdict`` and ``justification`` fields.
    """
    out = dict(claim_item)
    text = out.get("claim", "")
    url = out.get("evidence_url")

    if _FALSIFIED_RE.search(text):
        verdict = "falsified"
        justification = (
            "Claim asserts a failure, regression, conflict, or refuted outcome."
        )
    elif _TOY_SCALE_RE.search(text):
        verdict = "toy-scale"
        justification = "Claim relies on synthetic, mock, or toy-scale demonstration without production-scale evidence."
    elif url or _VERIFIED_METRIC_RE.search(text):
        verdict = "verified"
        justification = "Claim is backed by external evidence URL, tracked PR/issue, or concrete measured metrics."
    else:
        verdict = "no-evidence"
        justification = "Claim asserts an outcome without concrete numerical metrics, commit/PR links, or external evidence."

    out["verdict"] = verdict
    out["justification"] = justification
    return out


def triage_claims(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Triage a list of extracted claims, classifying each into the 4-verdict taxonomy."""
    return [triage_claim(c) for c in claims]


def compute_claim_score(
    claims: List[Dict[str, Any]],
    dimension_max: float = 52.0,
) -> Dict[str, Any]:
    """Compute aggregate quality score and statistics from triaged claim verdicts."""
    counts = {
        "verified": 0,
        "falsified": 0,
        "toy-scale": 0,
        "no-evidence": 0,
    }
    for c in claims:
        v = c.get("verdict", "no-evidence")
        if v in counts:
            counts[v] += 1
        else:
            counts["no-evidence"] += 1

    total_claims = len(claims)
    if total_claims == 0:
        return {
            "score": 0.0,
            "max": dimension_max,
            "overall_percentage": 0.0,
            "verdict_counts": counts,
        }

    raw_points = sum(
        CLAIM_VERDICT_WEIGHTS.get(c.get("verdict", ""), 0.0) for c in claims
    )
    percentage = round((raw_points / total_claims) * 100, 1)
    derived_score = round((percentage / 100.0) * dimension_max, 1)

    return {
        "score": derived_score,
        "max": dimension_max,
        "overall_percentage": percentage,
        "verdict_counts": counts,
    }


# ──────────────────────────────────────────────────────────────────────
# Statistical-validity gate — Slice D (#2696, P-Bench lesson)
# ──────────────────────────────────────────────────────────────────────
# P-Bench: agents emit wrong-but-fluent quantitative claims (correct
# execution, invalid inference). A named test clears method-level flags.

_QUANT_CLAIM_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|x\b|×|ms\b|s\b|sec\b|seconds?\b|tokens?\b|bytes?\b|kb\b|mb\b|gb\b|rpm\b|tps\b)"
    r"|\bp\s*[<>=]\s*0?\.\d+\b"
    r"|\b(?:accuracy|f1|latency|throughput|cost|score)\s*[:=]\s*\d",
    re.IGNORECASE,
)

_TEST_NAME_RE = re.compile(
    r"\b(?:t[- ]?test|chi[- ]?squared|anova|regression|wilcoxon|mann[- ]?whitney|"
    r"kruskal|bootstrap|permutation|hdi\b|credible|bayesian|likelihood|cohen|durbin|shapiro)\b",
    re.IGNORECASE,
)

# (flag, pattern) — one pattern per inferential violation.
_STAT_VALIDITY_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    (
        "significance-without-test",
        re.compile(
            r"\b(?:statistically\s+)?significant(?:ly)?\b(?!.*\b(?:test|p\s*[<>=]|interval|hdi)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "pvalue-without-method",
        re.compile(
            r"\bp\s*[<>=]\s*0?\.\d+\b(?!.*\b(?:test|regression|anova|chi|wilcoxon|mann)\b)"
        ),
    ),
    (
        "unbacked-precision",
        re.compile(
            r"\d+\.\d+\s*%(?!.*\b(?:n\s*[=:]|sample|baseline|compared|vs\.?|from|to|source|http|#\d+)\b)"
        ),
    ),
    (
        "missing-baseline",
        re.compile(
            r"\b(?:improved|reduced|increased|decreased|cut|boosted|slashed|fell|dropped|rose|grew)\b[^.]*"
            r"\bby\s+\d+(?:\.\d+)?\s*%(?!.*\b(?:from|to|vs\.?|compared|baseline|previous|before|http|#\d+)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "pvalue-ungrounded",
        re.compile(
            r"\bp\s*[<>=]\s*0?\.\d+\b(?!.*\b(?:n\s*[=:]|df\s*[=:]|statistic|http|#\d+)\b)"
        ),
    ),
    (
        "causal-from-correlation",
        re.compile(
            r"\b(?:correlat\w+|associat\w+)\b[^.]*\b(?:caused?|due to|leads? to|because of|results? from)\b",
            re.IGNORECASE,
        ),
    ),
]

_DECISIVE_RE = re.compile(
    r"\b(?:proves?|conclusively|demonstrat\w+|definitively|certainly)\b", re.IGNORECASE
)
_WEAK_EVIDENCE_RE = re.compile(
    r"\b(?:preliminary|initial|single run|one example|anecdotal|suggests?|may|might|could|pilot)\b",
    re.IGNORECASE,
)

_GROUNDING_FLAGS = {
    "unbacked-precision",
    "missing-baseline",
    "causal-from-correlation",
    "decisive-from-weak",
}


def assess_stat_validity(claim_item: Dict[str, Any]) -> Dict[str, Any]:
    """Attach a deterministic inferential-sanity verdict to a claim.

    ``non-quantitative`` is skipped; ``ok`` passes; ``suspect`` lists
    violation flag names under ``stat_validity.flags``.
    """
    out = dict(claim_item)
    text = out.get("claim", "")
    if not _QUANT_CLAIM_RE.search(text):
        out["stat_validity"] = {"verdict": "non-quantitative", "flags": []}
        return out
    flags = [flag for flag, pat in _STAT_VALIDITY_PATTERNS if pat.search(text)]
    if _DECISIVE_RE.search(text) and _WEAK_EVIDENCE_RE.search(text):
        flags.append("decisive-from-weak")
    if _TEST_NAME_RE.search(text):
        flags = [f for f in flags if f in _GROUNDING_FLAGS]
    out["stat_validity"] = {
        "verdict": "suspect" if flags else "ok",
        "flags": sorted(set(flags)),
    }
    return out


def assess_stat_validity_claims(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Batch variant: attach ``stat_validity`` to every extracted claim."""
    return [assess_stat_validity(c) for c in claims]


def compute_stat_validity_stats(claims: List[Dict[str, Any]]) -> Dict[str, int]:
    """Scorecard aggregate: ``checked`` = quantitative, ``suspect`` = flagged."""
    verdicts = [c.get("stat_validity", {}).get("verdict") for c in claims]
    return {
        "checked": sum(v != "non-quantitative" for v in verdicts),
        "suspect": sum(v == "suspect" for v in verdicts),
    }


# ──────────────────────────────────────────────────────────────────────
# Rejection-bias (post-hoc rationalization) detection — Slice C (#2482 / #2515)
# ──────────────────────────────────────────────────────────────────────

_RATIONALIZATION_PATTERNS: List[Tuple[str, str, str, re.Pattern[str]]] = [
    (
        "dismissed_failure",
        "high",
        "Dismisses test failures or build errors as expected, non-blocking, or harmless.",
        re.compile(
            r"(?i)\b(?:although|despite|even though|however)\b.*?\b(?:failed|failures?|errors?|breakages?)\b.*?\b(?:expected|negligible|minor|harmless|acceptable|normal|intended|ignored|non-blocking)\b"
        ),
    ),
    (
        "dismissed_failure_inline",
        "high",
        "Asserts that failures can be safely ignored without remediation.",
        re.compile(
            r"(?i)\b(?:failed|failures?|errors?)\b.*?\b(?:is expected|can be ignored|not an issue|not a blocker|minor glitch|disregarded|harmless)\b"
        ),
    ),
    (
        "unproven_tradeoff",
        "medium",
        "Asserts performance regression or accuracy loss is an acceptable tradeoff without empirical backing.",
        re.compile(
            r"(?i)\b(?:regression|slowdown|latency increase|drop in (?:accuracy|performance))\b.*?\b(?:is harmless|tradeoff worth making|irrelevant|negligible|marginal|acceptable|intended)\b"
        ),
    ),
    (
        "theoretical_rationalization",
        "high",
        "Claims design is theoretically sound or valid despite broken execution.",
        re.compile(
            r"(?i)\b(?:failed|error|broken|failing)\b.*?\b(?:works? in principle|theoretically sound|conceptually valid|architecture holds)\b"
        ),
    ),
    (
        "environment_blaming",
        "medium",
        "Blames test environment or flakiness rather than addressing bug.",
        re.compile(
            r"(?i)\b(?:failure|fail|failed|error)\b.*?\b(?:due to (?:the )?(?:environment|test suite|flakiness|runner))\b.*?\b(?:not (?:our|the) code|not a real bug|ignore)\b"
        ),
    ),
]


def detect_rejection_bias(
    artifacts: List[Tuple[str, Optional[str]]],
    max_detections: int = 10,
) -> List[Dict[str, Any]]:
    """Scan judged artifacts for post-hoc rationalization / rejection-bias patterns.

    Detects structural over-scoring patterns where negative results or failures
    are rationalized away without empirical justification (Slice C / #2515).
    """
    detections: List[Dict[str, Any]] = []
    seen: set = set()

    for stage, text in artifacts:
        if not text:
            continue
        for header, block in _markdown_sections(text):
            for sentence in _split_sentences(block):
                if len(detections) >= max_detections:
                    break
                for cat, severity, reason, pattern in _RATIONALIZATION_PATTERNS:
                    if pattern.search(sentence):
                        key = (stage, header, cat, sentence)
                        if key in seen:
                            continue
                        seen.add(key)
                        detections.append({
                            "category": cat,
                            "statement": sentence,
                            "source": {
                                "stage": stage,
                                "section": header or "preamble",
                            },
                            "severity": severity,
                            "reason": reason,
                        })
                        break

    detections.sort(
        key=lambda d: (
            d["source"]["stage"],
            d["source"]["section"],
            d["category"],
            d["statement"],
        )
    )
    return detections


# ──────────────────────────────────────────────────────────────────────
# Persistence — read / append rubric scorecards (like metrics.jsonl)
# ──────────────────────────────────────────────────────────────────────


def load_scorecards(scorecard_file: Path) -> List[Dict[str, Any]]:
    """Read all rubric scorecards, oldest-first, skipping malformed lines."""
    out: List[Dict[str, Any]] = []
    if not scorecard_file.exists():
        return out
    for ln in scorecard_file.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def append_scorecard(
    scorecard_file: Path,
    record: Dict[str, Any],
) -> None:
    """Append one JSON line, idempotently: replace any existing line for the
    same date + grader combination so re-runs don't duplicate."""
    lines: List[str] = []
    key = (record["cycle_date"], record.get("grader", "strict"))
    if scorecard_file.exists():
        for ln in scorecard_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            obj_key = (obj.get("cycle_date", ""), obj.get("grader", "strict"))
            if obj_key != key:
                lines.append(json.dumps(obj, sort_keys=True))
    lines.append(json.dumps(record, sort_keys=True))
    scorecard_file.parent.mkdir(parents=True, exist_ok=True)
    scorecard_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def summarize_scorecards(
    records: List[Dict[str, Any]], last: int = 7
) -> Dict[str, Any]:
    """Aggregate the last ``last`` strict scorecards into a quality trend summary."""
    strict_records = [r for r in records if r.get("grader") == "strict"]
    recent = strict_records[-last:] if last and last > 0 else list(strict_records)

    pcts = [
        r.get("overall_percentage", 0)
        for r in recent
        if r.get("overall_percentage") is not None
    ]
    avg_pct = sum(pcts) / len(pcts) if pcts else 0.0

    # Trend: improving / flat / declining
    trend = "n/a"
    if len(pcts) >= 4:
        midpoint = len(pcts) // 2
        first_half = sum(pcts[:midpoint]) / midpoint
        second_half = sum(pcts[midpoint:]) / (len(pcts) - midpoint)
        if second_half > first_half * 1.10:
            trend = "improving"
        elif second_half < first_half * 0.90:
            trend = "declining"
        else:
            trend = "flat"

    # Collect all flags
    all_flags: List[str] = []
    for r in recent:
        all_flags.extend(r.get("flags") or [])

    return {
        "cycles": len(recent),
        "avg_overall_pct": round(avg_pct, 1),
        "min_pct": round(min(pcts), 1) if pcts else 0.0,
        "max_pct": round(max(pcts), 1) if pcts else 0.0,
        "trend": trend,
        "persistent_flags": list(set(all_flags)),
    }


def format_summary(summary: Dict[str, Any]) -> str:
    """One-line rendering for no_agent cron output."""
    tail = (
        " | ".join(summary["persistent_flags"][:3])
        if summary["persistent_flags"]
        else "no flags"
    )
    return (
        f"[rubric-judge] last {summary['cycles']} cycles: "
        f"avg={summary['avg_overall_pct']}% min={summary['min_pct']}% "
        f"max={summary['max_pct']}% trend={summary['trend']} | {tail}"
    )


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


def cycle_date(now: datetime | None = None) -> str:
    """Same logic as evolution_funnel.cycle_date: before 08:00, use yesterday."""
    if now is None:
        now = datetime.now()
    from datetime import timedelta

    day = now.date() if now.hour >= 8 else (now - timedelta(days=1)).date()
    return day.isoformat()


def main(argv: List[str]) -> int:
    evolution_dir = _hot_path(Path(".") if not argv else None)
    args = argv[1:]

    if "--help" in args or "-h" in args:
        print(
            "Usage: evolution_rubric_judge.py [--score DATE] [--summary [--last N]]\n"
            "\n"
            "  --score DATE    Score a specific date's cycle (default: today/yesterday)\n"
            "  --summary       Summarize recent strict scorecards\n"
            "  --last N        Window for summary (default: 7)\n"
            "  --grader TYPE   'strict' (default) or 'agent'\n"
        )
        return 0

    if "--summary" in args:
        last = 7
        if "--last" in args:
            i = args.index("--last")
            if i + 1 < len(args):
                try:
                    last = int(args[i + 1])
                except ValueError:
                    last = 7
        records = load_scorecards(evolution_dir / "rubric-scorecard.jsonl")
        print(format_summary(summarize_scorecards(records, last)))
        return 0

    date = ""
    if "--score" in args:
        i = args.index("--score")
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            date = args[i + 1]
    if not date and len(argv) > 1 and not argv[1].startswith("-"):
        date = argv[1]
    date = date or os.environ.get("RUBRIC_JUDGE_DATE", "")
    if not date:
        try:
            from hermes_time import now as _now  # type: ignore

            date = cycle_date(_now())
        except Exception:
            date = cycle_date()

    grader_type = "strict"
    if "--grader" in args:
        i = args.index("--grader")
        if i + 1 < len(args):
            grader_type = args[i + 1]

    if grader_type == "strict":
        grader = StrictRubricJudgeGrader()
    else:
        grader = StrictRubricJudgeGrader()  # AgentGrader needs LLM — fall back

    scorecard = grader.score(date, evolution_dir)

    # Persist
    append_scorecard(evolution_dir / "rubric-scorecard.jsonl", scorecard)

    # Refresh sidecar summary for file-toolset stages
    try:
        (evolution_dir / "rubric-summary.txt").write_text(
            format_summary(
                summarize_scorecards(
                    load_scorecards(evolution_dir / "rubric-scorecard.jsonl"), 7
                )
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    # Compact oneline for cron log (no_agent job)
    pct = scorecard["overall_percentage"]
    flags = " | ".join(scorecard["flags"][:2]) if scorecard["flags"] else "ok"
    print(
        f"[rubric-judge] {date}: {pct}% ({scorecard['total_score']}/"
        f"{scorecard['total_max']}) | {flags}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
