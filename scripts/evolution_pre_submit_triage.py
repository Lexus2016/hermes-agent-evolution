#!/usr/bin/env python3
"""Pre-submission triage gate for the evolution `issues` stage (#336).

WHY THIS EXISTS — shift triage LEFT. The evolution pipeline historically opened
GitHub issues FIRST and triaged SECOND (the `analysis` stage labels `rejected`
and closes). Because `research` optimizes for coverage and every fork runs its
own research cycle against the shared origin, GitHub fills with duplicate /
already-covered proposals that a human or the analysis stage must clear
afterward. This gate moves the duplicate check BEFORE `gh issue create`: for a
DRAFT proposal it decides CREATE vs SKIP-duplicate against the currently-OPEN
issues, so only non-duplicate proposals are ever filed.

CONSERVATISM IS THE WHOLE POINT (anti-fabrication guard). The project has a
documented, expensive failure mode: triage FABRICATED a rejection and wrongly
CLOSED a real issue (#83, the #101 class). The lesson is asymmetric-cost: a
wrongful SKIP suppresses a genuine proposal (silent, hard to notice); a
needless CREATE merely adds one issue that later triage can still close. So we
SKIP **only** on a HIGH-confidence title/signature overlap (Dice >= a high
threshold) and DEFAULT TO CREATE on any doubt — empty open-issues, a weak or
ambiguous overlap, a missing draft title. Never skip on a weak match.

SCOPE (this slice): deterministic dedup against OPEN issues only. The
coverage/LLM-confidence check and per-fork isolation are explicit follow-ups
(see #336), NOT implemented here. Closed/rejected-issue handling stays in
skills/evolution/evolution-issues/SKILL.md.

The similarity idiom is REUSED from scripts/evolution_dedup.py
(`normalize_title`): same tag-stripping + lowercase + punctuation-collapse, so
this gate and the local dedup cache canonicalize titles identically. We extend
it from an exact-key hash to a graded token overlap so a HIGH threshold can be
applied.

The open-issues fetch is behind an INJECTABLE SEAM (`fetch_open_issues`) so the
pure `decide()` runs fully offline in tests; only the CLI touches the network
via `gh issue list`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

# Reuse the EXACT normalization idiom from the local dedup cache so both stages
# canonicalize titles identically (tag-strip + lowercase + punctuation-collapse).
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from evolution_dedup import normalize_title  # noqa: E402

# HIGH by design: skip ONLY a near-exact title overlap. Lowering this re-opens
# the fabricated-rejection failure mode (#83/#101) — keep the bar high.
DEFAULT_THRESHOLD = 0.85


def _tokens(title: str) -> List[str]:
    """Normalized, de-duplicated word tokens of a title (order-independent)."""
    norm = normalize_title(title)
    # normalize_title already lowercases and reduces non-word runs to single
    # spaces, so a plain split yields the canonical token set.
    return sorted(set(norm.split()))


def similarity(a: str, b: str) -> float:
    """Graded title overlap in [0.0, 1.0] via the Sørensen–Dice coefficient on
    normalized token SETS: ``2 * |A ∩ B| / (|A| + |B|)``.

    Deterministic, symmetric, pure. 1.0 = identical canonical token sets (e.g.
    cosmetic ``[TAG]``/case/punctuation variants), 0.0 = disjoint or empty. We
    use token sets (not sequences) so word-order differences don't mask a true
    duplicate, and Dice (not raw intersection) so adding a couple of words to an
    otherwise-identical title still scores high but a single shared generic word
    ("metrics") scores low — exactly the gradient the conservative threshold
    relies on.
    """
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return (2.0 * inter) / (len(ta) + len(tb))


@dataclass(frozen=True)
class GateDecision:
    """Structured gate object: {decision, matched_issue, score, reason}.

    ``decision`` is ``"create"`` or ``"skip_duplicate"``. ``matched_issue`` is
    the OPEN issue number a skip is justified by (``None`` on create).
    ``score`` is the best similarity observed (for auditability — present even
    on create). ``reason`` is a human-readable justification for the report/log.
    """

    decision: str
    score: float
    reason: str
    matched_issue: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "matched_issue": self.matched_issue,
            "score": round(self.score, 4),
            "reason": self.reason,
        }


def _best_open_match(
    draft_title: str, open_issues: Sequence[Dict[str, Any]]
) -> tuple[float, Optional[Dict[str, Any]]]:
    """Best (score, issue) over OPEN issues only. Closed issues are out of scope
    for this gate (handled in SKILL.md), so they never produce a skip signal."""
    best_score = 0.0
    best_issue: Optional[Dict[str, Any]] = None
    for issue in open_issues:
        state = str(issue.get("state", "open")).lower()
        if state != "open":
            continue
        title = issue.get("title")
        if not title:
            continue
        score = similarity(draft_title, str(title))
        if score > best_score:
            best_score = score
            best_issue = issue
    return best_score, best_issue


def decide(
    draft: Dict[str, Any],
    open_issues: Sequence[Dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> GateDecision:
    """Decide CREATE vs SKIP-duplicate for ``draft`` against the OPEN issues.

    CONSERVATIVE: returns ``skip_duplicate`` ONLY when the best OPEN-issue title
    overlap is ``>= threshold`` (a HIGH, near-exact bar). Every other outcome —
    no draft title, empty open-issues, a weak or merely-partial overlap — returns
    ``create``. This create-on-doubt default is the anti-fabrication guard: a
    wrongful skip silently suppresses a real proposal (the #83/#101 failure
    mode), whereas a needless create is cheaply closed by later triage. We never
    skip on a weak match.

    Pure: ``open_issues`` is injected by the caller (the CLI fetches it via
    ``gh``); this function performs no IO and is fully offline-testable.
    """
    draft_title = str(draft.get("title") or "").strip()
    if not draft_title:
        return GateDecision(
            decision="create",
            score=0.0,
            reason="draft has no usable title; cannot prove a duplicate — create",
            matched_issue=None,
        )

    best_score, best_issue = _best_open_match(draft_title, open_issues)

    if best_issue is not None and best_score >= threshold:
        return GateDecision(
            decision="skip_duplicate",
            score=best_score,
            reason=(
                f"high-confidence duplicate of OPEN issue "
                f"#{best_issue.get('number')} (score {best_score:.3f} "
                f">= threshold {threshold:.3f})"
            ),
            matched_issue=_as_int(best_issue.get("number")),
        )

    # Create-on-doubt. Surface the near-miss for auditability without acting on it.
    if best_issue is not None:
        reason = (
            f"best OPEN match #{best_issue.get('number')} score {best_score:.3f} "
            f"< threshold {threshold:.3f} — too weak to skip, defaulting to create"
        )
    else:
        reason = "no overlapping OPEN issue — create"
    return GateDecision(
        decision="create", score=best_score, reason=reason, matched_issue=None
    )


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── IO boundary (injectable seam) ───────────────────────────────────────────────
def fetch_open_issues(
    repo: str, *, limit: int = 300, runner: Optional[Callable[[List[str]], str]] = None
) -> List[Dict[str, Any]]:
    """Fetch currently-OPEN issues via ``gh`` (the only networked path).

    ``runner`` is injectable so tests can supply canned JSON without a real
    ``gh``; production passes the default subprocess runner. Returns a list of
    ``{number, title, state}`` dicts. On any failure returns ``[]`` — and an
    empty open-issues list makes ``decide`` return CREATE, i.e. a fetch failure
    fails OPEN (never a wrongful skip), consistent with the conservative rule.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        "number,title,state",
    ]
    run = runner or _default_runner
    try:
        raw = run(cmd)
        data = json.loads(raw) if raw else []
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    issues: List[Dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and item.get("title"):
            issues.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "state": item.get("state", "open"),
                }
            )
    return issues


def _default_runner(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return proc.stdout


def main(argv: List[str]) -> int:
    """CLI for the SKILL.md hook:

        evolution_pre_submit_triage.py decide "<draft title>" [--repo R] [--threshold T]

    Prints the gate object as one JSON line and sets the exit code so the skill
    can branch from the terminal:  exit 0 = CREATE, exit 10 = SKIP-duplicate,
    exit 2 = usage error. The non-zero SKIP code is deliberately distinct from a
    generic failure so a crashed gate (any other non-zero) is NOT mistaken for a
    skip — fail-open toward CREATE.
    """
    args = list(argv[1:])
    if not args or args[0] != "decide":
        print('usage: evolution_pre_submit_triage.py decide "<title>" '
              "[--repo OWNER/REPO] [--threshold T]", file=sys.stderr)
        return 2
    rest = args[1:]
    title: Optional[str] = None
    repo = "Lexus2016/hermes-agent-evolution"
    threshold = DEFAULT_THRESHOLD
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--repo" and i + 1 < len(rest):
            repo = rest[i + 1]
            i += 2
        elif tok == "--threshold" and i + 1 < len(rest):
            try:
                threshold = float(rest[i + 1])
            except ValueError:
                print(f"invalid --threshold: {rest[i + 1]}", file=sys.stderr)
                return 2
            i += 2
        elif title is None:
            title = tok
            i += 1
        else:
            i += 1
    if not title:
        print("missing draft title", file=sys.stderr)
        return 2

    open_issues = fetch_open_issues(repo)
    decision = decide({"title": title}, open_issues, threshold=threshold)
    print(json.dumps(decision.to_dict()))
    return 10 if decision.decision == "skip_duplicate" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
