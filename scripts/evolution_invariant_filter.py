#!/usr/bin/env python3
"""Immutable invariant filter for forge rewrites (issue #68, slices 1+2).

Screens every skill/prompt/harness rewrite against a versioned set of
Cerberus/household constraints BEFORE it reaches the judge. A rewrite that
violates any hard invariant is short-circuited at zero fitness — it never
reaches evaluation or application. This is the "cannot violate" floor that
complements the existing verifier-gated edits (SIA) and the prompt-injection
scan (#1808).

Slices shipped here:

  * **Slice 1** — the constraint set lives as DATA in
    ``evolution_invariant_rules.json`` (a versioned, human-readable file), so
    the rules are reviewable, diffable, and extensible without touching code.
  * **Slice 2** — this deterministic filter loads that data and screens a
    rewrite dict against it. Wired into ``evolution_harness_gate.run_gate`` and
    ``run_cron_pass`` so a violating harness proposal is rejected with
    ``zero_fitness`` before the sandbox/regression gate ever runs.
  * **Slice 3** (staged rollout) — ``classify_stakes`` buckets a proposal into
    low/medium/high from its explicit ``stakes`` field (else its target path).
    Each rule may declare an ``enforce_at`` floor; below that floor a finding is
    demoted to ADVISORY instead of blocking. A rule with no ``enforce_at``
    defaults to high and blocks everything (unchanged behavior), so a new strict
    rule can be rolled out staged — advisory on high-stakes targets first,
    blocking on low-stakes ones — with zero behavior change for existing rules.
  * **Slice 5** (#3518, pinned invariant core) — the default load reads the
    rules from a pinned copy OUTSIDE the agent-writable tree
    (``~/.hermes/evolution/pinned/``, override: ``$EVOLUTION_PINNED_RULES``),
    seeded once at deploy time via ``--seed-pinned-rules``. The filter refuses
    to run when that surface is missing or shadowed by an in-tree copy, so a
    self-edit cannot weaken the rules it is judged against.
  * **Slice 4** (pinned safe-fallback set) — ``load_safe_fallbacks`` /
    ``resolve_pinned_fallback`` expose a versioned ``safe_fallbacks`` set of
    human-vetted, pinned alternative targets. When a proposal is blocked on a
    forbidden path that has a pinned fallback, the filter attaches
    ``safe_fallback`` to the result so the rejection is actionable, not a dead
    end. Additive: a rules file that predates slice 4 simply yields no fallbacks.

The filter is deliberately CONSERVATIVE: it catches only unambiguous
violations (low false-positive floor), and it checks only top-level scalar
fields of a rewrite dict. It is a deterministic floor, not a semantic judge —
the LLM judge still does the open-ended evaluation; this module just vetoes
the candidates that must never get that far.

Design mirrors the other ``scripts/evolution_*.py`` helpers: pure, typed,
import-safe functions + a thin CLI. No LLM, no network, no clock, no IO beyond
reading the rules file.

CLI:

    evolution_invariant_filter.py proposal.json
    cat proposal.json | evolution_invariant_filter.py

Prints one JSON object ``{"ok", "violations", ...}`` and exits 0 when the
rewrite passes every invariant, 1 when it violates one — so a shell caller can
short-circuit on ``$?`` before invoking the judge.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_RULES_FILENAME = "evolution_invariant_rules.json"

# Slice 5 (#3518): the rules the filter judges against live OUTSIDE the tree
# the agent commits to. A self-edit can rewrite the in-tree
# ``evolution_invariant_rules.json`` (the shipped seed), but it cannot reach the
# pinned copy, so it cannot weaken the checks that govern it. The pinned file
# is seeded exactly once from the in-tree copy and never overwritten after.
_PINNED_RULES_DIR = Path.home() / ".hermes" / "evolution" / "pinned"
_SHADOW_NAMES = (
    "evolution_invariant_rules.in-tree.json",
    "evolution_invariant_rules.override.json",
)


def pinned_rules_path() -> Path:
    """Where the read-only pinned rules live.

    ``EVOLUTION_PINNED_RULES`` overrides the location (tests, alternate
    profiles). The default sits under the hermes home, outside any checkout.
    """
    override = os.environ.get("EVOLUTION_PINNED_RULES", "").strip()
    if override:
        return Path(override)
    return _PINNED_RULES_DIR / _RULES_FILENAME


def _assert_not_shadowed(pinned: Path) -> None:
    """Refuse to run when an in-tree copy shadows the pinned surface.

    A rules-override file planted beside the pinned file means something tried
    to install a second rules source where the loader looks. Fail closed
    instead of guessing which one wins.
    """
    for name in _SHADOW_NAMES:
        shadow = pinned.parent / name
        if shadow.is_file():
            raise ValueError(
                f"pinned invariant surface shadowed by in-tree copy {shadow}"
            )


def _resolve_rules_file() -> Path:
    """Return the pinned rules file. Fail closed when it is missing.

    The pinned surface is seeded EXPLICITLY (``seed_pinned_rules`` at deploy
    time), never implicitly at load time: an implicit seed would make the
    guarantee depend on whatever the agent-writable tree contains at that
    moment, which is exactly the thing being defended against.
    """
    pinned = pinned_rules_path()
    _assert_not_shadowed(pinned)
    if not pinned.is_file():
        raise ValueError(
            f"pinned invariant surface missing at {pinned} - seed it once "
            f"with: evolution_invariant_filter.py --seed-pinned-rules"
        )
    return pinned


def seed_pinned_rules() -> Path:
    """Deploy-time bootstrap: copy the in-tree rules to the pinned surface.

    Run once by the installer/CI (or an operator). Never called by the load
    path, so a missing surface fails closed instead of silently regrowing
    from tree content a self-edit could have rewritten.
    """
    pinned = pinned_rules_path()
    _assert_not_shadowed(pinned)
    source = _rules_path()
    payload = source.read_text(encoding="utf-8")
    if not payload.strip():
        raise ValueError(f"in-tree seed {source} is empty")
    pinned.parent.mkdir(parents=True, exist_ok=True)
    pinned.write_text(payload, encoding="utf-8")
    try:  # best-effort read-only; enforcement is the operator's mount, not us
        pinned.chmod(0o444)
    except OSError:
        pass
    return pinned


# Check types the rules file may declare. Anything outside this vocabulary is
# ignored (never a crash) so a malformed/older rules file degrades safely.
_CHECKS = ("human_gating", "forbidden_paths", "forbidden_patterns")

# Stakes ladder (slice 3 — staged rollout). A proposal's ``stakes`` (explicit
# field, else inferred from its target path) selects how aggressively the
# immutable floor is applied. Rules declare ``enforce_at`` — the minimum stakes
# at which the rule BLOCKS. A violation of a rule whose threshold is above the
# proposal's stakes is demoted to an ADVISORY (non-blocking) finding, so a new
# strict rule can be rolled out staged (advisory on high-stakes targets first,
# blocking on low-stakes ones) without an abrupt all-or-nothing flip.
_STAKES_ORDER = ("low", "medium", "high")
_DEFAULT_STAKES = "high"  # fail safe: an unclassifiable proposal is high-stakes
# Backward-compatible default for a rule's ``enforce_at`` floor: a rule with no
# ``enforce_at`` always blocks (threshold = low — the easiest to meet). This
# preserves the pre-slice-3 behavior where every hard rule blocked every
# proposal. Staged rollout is OPT-IN: a rule that wants advisory-on-high-stakes
# explicitly declares ``enforce_at: high`` (or ``medium``).
_DEFAULT_ENFORCE_AT = "low"

# Default text fields scanned by ``forbidden_patterns`` when a rule does not
# declare its own ``text_fields``.
_DEFAULT_TEXT_FIELDS = (
    "delta",
    "text",
    "rationale",
    "title",
    "body",
    "content",
    "summary",
)

# Default path-bearing fields scanned by ``forbidden_paths``.
_DEFAULT_PATH_FIELDS = ("path", "file", "target", "target_file", "files")


def _rules_path() -> Path:
    """Locate the rules data file next to this module."""
    return Path(__file__).resolve().parent / _RULES_FILENAME


def load_invariant_rules(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load + validate the versioned invariant rule set from disk.

    Returns ``{"version": int, "rules": [...]}``. Raises ``ValueError`` on a
    missing/invalid file, so a caller can fail closed (a missing rules file is
    an operator error, not a silent pass).
    """
    # Slice 5: an explicit path (tests, one-off audits) is read as-is. The
    # default load resolves the PINNED surface outside the agent-writable tree
    # and fails closed when that surface is missing or shadowed.
    if path is not None:
        p = path
    else:
        p = _resolve_rules_file()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read invariant rules {p}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"invariant rules {p} are not valid JSON: {exc}") from exc
    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError(f"invariant rules {p} missing a positive integer 'version'")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"invariant rules {p} has no 'rules' list")
    loaded: Dict[str, Any] = {
        "version": version,
        "rules": rules,
        "rules_source": str(p),
    }
    # Slice 4: carry the versioned safe-fallback set forward verbatim (if the
    # rules file declares one) so downstream consumers can resolve pinned
    # alternatives. Absent in older files -> the key is simply omitted.
    if "safe_fallbacks" in data:
        loaded["safe_fallbacks"] = data["safe_fallbacks"]
    return loaded


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _walk_text_fields(obj: Any, fields: Tuple[str, ...]) -> Dict[str, str]:
    """Collect the named scalar text fields (top-level) from a dict."""
    if not isinstance(obj, dict):
        return {}
    return {f: _as_text(obj[f]) for f in fields if f in obj}


def _walk_paths(obj: Any, fields: Tuple[str, ...]) -> List[str]:
    """Collect path-bearing fields (str or list-of-str) from a dict."""
    out: List[str] = []
    if not isinstance(obj, dict):
        return out
    for f in fields:
        if f not in obj:
            continue
        value = obj[f]
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(_as_text(x) for x in value)
    return out


# -- Slice 3: stakes classification (staged rollout) --------------------------


def _stakes_rank(stakes: Any) -> int:
    """Rank a stakes label on the ``low < medium < high`` ladder.

    Unknown values are coerced to the safe default (high). Returns an index
    into ``_STAKES_ORDER`` so callers can compare with ``>=``.
    """
    label = str(stakes or "").strip().lower()
    if label in _STAKES_ORDER:
        return _STAKES_ORDER.index(label)
    return _STAKES_ORDER.index(_DEFAULT_STAKES)


def classify_stakes(obj: Any) -> Tuple[str, str]:
    """Classify a proposal's stakes as ``(label, source)``.

    ``label`` is one of ``low``/``medium``/``high``. ``source`` is ``"explicit"``
    (the proposal carried its own ``stakes`` field), ``"inferred"`` (from its
    target path), or ``"default"`` (fail-safe high for a pathless/unclassifiable
    proposal). Pure + deterministic — no IO, no clock.
    """
    if isinstance(obj, dict) and isinstance(obj.get("stakes"), str):
        label = obj["stakes"].strip().lower()
        if label in _STAKES_ORDER:
            return label, "explicit"
        # An explicit but unknown label is not silently trusted — fail safe.
        return _DEFAULT_STAKES, "default"
    if isinstance(obj, dict):
        # Infer from the target path. Distinguish an inferred-HIGH (a path hit
        # the high-risk fragments) from a fail-safe default-HIGH (no path at
        # all): both return "high", but only the former is source="inferred".
        paths = _walk_paths(obj, _DEFAULT_PATH_FIELDS)
        if not paths:
            return _DEFAULT_STAKES, "default"
        high_fragments = (
            "evolution_merge_gate",
            "evolution_harness_gate",
            "register_evolution_cron",
            "evolution_orchestrator",
            "tools/approval",
            "evolution_*_gate.py",
        )
        for p in paths:
            pl = p.lower()
            if any(frag in pl for frag in high_fragments):
                return "high", "inferred"
        return "medium", "inferred"
    return _DEFAULT_STAKES, "default"


def _rule_enforces_at(rule: Dict[str, Any], stakes_rank: int) -> bool:
    """True if a rule's ``enforce_at`` threshold is met at the given stakes.

    A rule with no ``enforce_at`` defaults to ``_DEFAULT_ENFORCE_AT`` (low) —
    i.e. it blocks every proposal, backwards compatible with the pre-slice-3
    behavior where every hard rule always blocked. A rule that explicitly
    declares a higher floor (e.g. ``enforce_at: high``) is rolled out staged:
    below that floor a finding becomes advisory instead of blocking.
    """
    threshold = str(rule.get("enforce_at") or _DEFAULT_ENFORCE_AT).strip().lower()
    return stakes_rank >= _stakes_rank(threshold)


# -- Slice 4: pinned safe-fallback set ----------------------------------------


def load_safe_fallbacks(rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract the versioned ``safe_fallbacks`` set from a loaded rules dict.

    Each entry is ``{"id", "target", "replacement"}``: a pinned, human-vetted
    alternative for a high-stakes target path that the forge must never rewrite
    unattended. When a proposal violates ``no-critical-auth-path-rewrite`` (or
    any rule with ``safe_fallback_key``), the filter can offer the matching
    fallback as the allowed path instead of a flat rejection — so the blocker
    becomes actionable, not a dead end. Returns ``[]`` for a rules file that
    predates slice 4 (safe fallback is additive).
    """
    if not isinstance(rules, dict):
        return []
    data = rules.get("safe_fallbacks")
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") and entry.get("replacement"):
            out.append(
                {
                    "id": str(entry["id"]),
                    "target": str(entry.get("target") or ""),
                    "replacement": str(entry["replacement"]),
                    "description": str(entry.get("description") or ""),
                }
            )
    return out


def resolve_pinned_fallback(
    proposal: Any,
    rules: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return a pinned safe fallback for a proposal that hit a forbidden path.

    Scans the versioned ``safe_fallbacks`` set for an entry whose ``target`` is
    a fragment of a path the proposal rewrites. Returns ``None`` when the
    proposal does not target a pinned path (or no fallback is pinned) — the
    caller then treats the violation as an un-cachable hard block. Pure +
    deterministic. When ``rules`` is omitted it is loaded from disk.
    """
    if rules is None:
        try:
            rules = load_invariant_rules()
        except ValueError:
            return None
    if not isinstance(proposal, dict):
        return None
    paths = _walk_paths(proposal, _DEFAULT_PATH_FIELDS)
    if not paths:
        return None
    for entry in load_safe_fallbacks(rules):
        target = entry["target"].lower()
        if target and any(target in p.lower() for p in paths):
            return entry
    return None


def _check_human_gating(
    proposal: Dict[str, Any], rule: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Flag an explicit weakening of human gating. Absence is NOT a violation —
    only a truthy ``auto_apply`` or an explicit ``requires_human_review=False``
    is."""
    violations: List[Dict[str, Any]] = []
    if proposal.get("auto_apply"):
        violations.append({"rule": rule["id"], "reason": "auto_apply is truthy"})
    if (
        "requires_human_review" in proposal
        and proposal.get("requires_human_review") is False
    ):
        violations.append(
            {
                "rule": rule["id"],
                "reason": "requires_human_review is explicitly falsy",
            }
        )
    return violations


def _check_forbidden_paths(
    proposal: Dict[str, Any], rule: Dict[str, Any]
) -> List[Dict[str, Any]]:
    fields = tuple(rule.get("path_fields") or _DEFAULT_PATH_FIELDS)
    fragments = [str(f).lower() for f in rule.get("forbidden_fragments", [])]
    violations: List[Dict[str, Any]] = []
    for p in _walk_paths(proposal, fields):
        pl = p.lower()
        for frag in fragments:
            if frag and frag in pl:
                violations.append(
                    {
                        "rule": rule["id"],
                        "reason": f"path {p!r} hits forbidden fragment {frag!r}",
                    }
                )
                break
    return violations


def _check_forbidden_patterns(
    proposal: Dict[str, Any], rule: Dict[str, Any]
) -> List[Dict[str, Any]]:
    fields = tuple(rule.get("text_fields") or _DEFAULT_TEXT_FIELDS)
    patterns = [re.compile(str(p), re.IGNORECASE) for p in rule.get("patterns", [])]
    violations: List[Dict[str, Any]] = []
    for field, text in _walk_text_fields(proposal, fields).items():
        for pat in patterns:
            if pat.search(text):
                violations.append(
                    {
                        "rule": rule["id"],
                        "reason": (
                            f"field {field!r} matches forbidden pattern {pat.pattern!r}"
                        ),
                    }
                )
                break
    return violations


def check_proposal(
    proposal: Any, rules: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Screen ONE rewrite against the invariant rules.

    Returns ``{"ok": bool, "violations": [...]}`` where each violation is
    ``{"rule", "reason"}``. A non-dict proposal passes (nothing to screen) — the
    gate's own shape checks handle malformed input. If ``rules`` is omitted it
    is loaded from disk; a missing/invalid rules file fails CLOSED (a violation,
    not a pass).
    """
    if rules is None:
        try:
            rules = load_invariant_rules()
        except ValueError as exc:
            return {
                "ok": False,
                "violations": [{"rule": "rules-unavailable", "reason": str(exc)}],
            }
    if not isinstance(proposal, dict):
        return {"ok": True, "violations": []}

    # Slice 3: classify stakes once for this proposal (explicit > inferred >
    # fail-safe default) and use each rule's ``enforce_at`` floor to decide
    # whether a finding BLOCKS or is merely advisory. Backwards compatible:
    # a rule with no ``enforce_at`` blocks every proposal, exactly as before.
    stakes, stakes_source = classify_stakes(proposal)
    stakes_rank = _stakes_rank(stakes)

    blocking: List[Dict[str, Any]] = []
    advisory: List[Dict[str, Any]] = []
    for rule in rules.get("rules", []):
        check = rule.get("check")
        if check == "human_gating":
            found = _check_human_gating(proposal, rule)
        elif check == "forbidden_paths":
            found = _check_forbidden_paths(proposal, rule)
        elif check == "forbidden_patterns":
            found = _check_forbidden_patterns(proposal, rule)
        else:  # unknown check type -> ignored (never crash)
            continue
        if not found:
            continue
        if _rule_enforces_at(rule, stakes_rank):
            blocking.extend(found)
        else:
            advisory.extend(found)

    result: Dict[str, Any] = {
        "ok": not blocking,
        "violations": blocking,
        "advisory": advisory,
        "stakes": stakes,
        "stakes_source": stakes_source,
    }

    # Slice 4: for a forbidden-path block with a pinned safe fallback, surface
    # the allowed alternative so the rejection is actionable, not a dead end.
    if blocking:
        fallback = resolve_pinned_fallback(proposal, rules=rules)
        if fallback is not None:
            result["safe_fallback"] = fallback
    return result


def filter_proposals(
    proposals: List[Any], rules: Optional[Dict[str, Any]] = None
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """Split proposals into ``(passing, rejected)``.

    Each ``rejected`` entry is ``{"proposal": <orig>, "violations": [...]}``.
    """
    passing: List[Any] = []
    rejected: List[Dict[str, Any]] = []
    for p in proposals:
        result = check_proposal(p, rules=rules)
        if result["ok"]:
            passing.append(p)
        else:
            rejected.append({"proposal": p, "violations": result["violations"]})
    return passing, rejected


def _load_proposal(path: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    except OSError as exc:
        return None, f"cannot read input: {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"input is not valid JSON: {exc}"
    return data, None


def main(argv: List[str]) -> int:
    if "--seed-pinned-rules" in argv[1:]:
        seeded = seed_pinned_rules()
        print(f"[evolution-invariant-filter] seeded pinned rules at {seeded}")
        return 0
    positional = [a for a in argv[1:] if not a.startswith("-")]
    path = positional[0] if positional else None
    proposal, err = _load_proposal(path)
    if err:
        print(f"[evolution-invariant-filter] {err}", file=sys.stderr)
        return 2
    result = check_proposal(proposal)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
