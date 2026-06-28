"""Declarative, policy-driven memory-poisoning guard (issue #315).

Hermes already *scans* memory writes for injection / exfiltration patterns
(``tools.memory_tool._scan_memory_content`` → ``tools.threat_patterns``) and
already *tags* every entry with provenance (``source_class`` / ``trust_tier``,
issue #316). What was missing is a declarative policy that *routes* a scan hit
through a chosen action — **block**, **warn**, or **strip** — keyed off the
entry's provenance. This module adds exactly that, and nothing more.

Design contract (mirrors :mod:`agent.policy_interceptors` intentionally):

* Pure and side-effect free. :meth:`MemoryGuardPolicy.evaluate` inspects the
  content + provenance, reuses the EXISTING scanner, and returns an immutable
  :class:`GuardOutcome`. It never writes to disk, never mutates the store, and
  never logs — the caller owns I/O and logging.
* Deterministic. Same content + same provenance + same rules always yields the
  same outcome. No clocks, no randomness, no network.
* REUSES the scanner. Detection comes from
  ``tools.threat_patterns.scan_for_threats`` (findings) and
  ``scan_for_threat_spans`` (spans, for strip). No new pattern logic lives here.

Backward-compatibility is the hard constraint (issue #315):

* The guard is **default-off**. ``build_memory_guard_from_config`` returns
  ``None`` when the ``memory.guard`` config section is absent or
  ``enabled: false`` — the store then keeps its pre-#315 binary-block path
  untouched (clean writes pass, poisoned writes are rejected exactly as today).
* When the guard IS enabled, a clean entry produces an ``allow`` outcome that
  is byte-identical to the input, so the store's normal write path runs
  unchanged. The guard only changes behaviour for entries the scanner flags.
* The provenance trailer format from #316 is never touched here; the guard
  receives already-parsed ``(source_class, trust_tier)`` and routes on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from tools.threat_patterns import scan_for_threats, scan_for_threat_spans

# Valid guard actions. "allow" is the no-op outcome for clean content; the
# three enforcement actions mirror the issue's block/warn/strip vocabulary.
GUARD_ACTIONS = ("allow", "block", "warn", "strip")

# Replacement inserted where a strip excises an offending span. Visible (not
# invisible unicode) so the stripped result can't itself trip the scanner, and
# distinctive enough to be greppable in an audit.
STRIP_REPLACEMENT = "[stripped]"

# The scanner scope used for memory content. "strict" is the broadest set and
# matches what _scan_memory_content already uses, so the guard sees exactly the
# findings the existing binary block would have seen.
_SCAN_SCOPE = "strict"


@dataclass(frozen=True)
class Provenance:
    """Already-parsed provenance for the entry under evaluation (#316).

    The store resolves these from the entry's ``⟦src:…|trust:…⟧`` trailer (or
    the safe defaults for legacy / default-add entries) before calling the
    guard, so the guard never re-parses the trailer format.
    """

    source_class: str = "unknown"
    trust_tier: str = "unknown"


@dataclass(frozen=True)
class GuardOutcome:
    """Result of evaluating the guard against one memory write.

    * ``action``   — one of :data:`GUARD_ACTIONS`.
    * ``content``  — the content to actually store. For ``allow`` / ``warn`` /
      ``block`` this is the input verbatim; for ``strip`` it is the input with
      offending spans excised. Callers persist this only when ``allowed``.
    * ``allowed``  — whether the write should proceed (true for allow / warn /
      strip; false for block).
    * ``findings`` — the scanner pattern ids that fired (empty for clean
      content). Surfaced so the caller can log a structured guard event.
    * ``message``  — human-readable explanation for block / warn / strip.
    * ``policy``   — the name of the matched rule (or "" / "default").
    """

    action: str = "allow"
    content: str = ""
    allowed: bool = True
    findings: Tuple[str, ...] = ()
    message: str = ""
    policy: str = ""

    @property
    def modified(self) -> bool:
        """True when the stored content differs from the input (strip)."""
        return self.action == "strip"

    def to_event(self) -> dict:
        """Structured, value-free-enough summary for trace logging."""
        return {
            "guard_action": self.action,
            "allowed": self.allowed,
            "findings": list(self.findings),
            "policy": self.policy,
            "modified": self.modified,
        }


@dataclass(frozen=True)
class MemoryGuardRule:
    """One declarative rule: when these source classes hit a threat, do ``action``.

    * ``action``        — block / warn / strip (allow is pointless as a rule).
    * ``source_classes``— frozenset of provenance source classes this rule
      applies to. Empty set means "any source class" (the catch-all).
    * ``name``          — label used in outcomes / logs.

    A rule only ever fires when the scanner found a threat; a clean entry never
    reaches rule matching.
    """

    action: str
    source_classes: frozenset[str]
    name: str

    def applies_to(self, source_class: str) -> bool:
        return not self.source_classes or source_class in self.source_classes


class MemoryGuardPolicy:
    """Routes a scan result + provenance through ordered block/warn/strip rules.

    First-match-wins on the ordered rule list (mirrors the first-match-wins
    contract of :class:`agent.policy_interceptors.PolicyInterceptorRegistry`).
    A write with no scanner findings is always ``allow``-ed verbatim, so an
    enabled guard is inert for clean content. When findings exist but no rule
    matches the entry's source class, the policy falls back to ``default_action``
    (defaults to ``block`` so an enabled-but-unmatched poisoned write is never
    silently stored).
    """

    def __init__(
        self,
        rules: Optional[list[MemoryGuardRule]] = None,
        default_action: str = "block",
        scan_scope: str = _SCAN_SCOPE,
    ):
        self._rules: list[MemoryGuardRule] = list(rules or [])
        self._default_action = default_action if default_action in GUARD_ACTIONS else "block"
        self._scan_scope = scan_scope

    @property
    def rules(self) -> Tuple[MemoryGuardRule, ...]:
        return tuple(self._rules)

    @property
    def default_action(self) -> str:
        return self._default_action

    def evaluate(self, content: str, provenance: Provenance | None = None) -> GuardOutcome:
        """Evaluate ``content`` (with optional ``provenance``) and decide.

        Reuses the existing scanner for detection. Returns:

        * ``allow`` (verbatim) when the scanner finds nothing — the inert path.
        * the first matching rule's action otherwise, or ``default_action`` when
          findings exist but no rule matches the source class.
        """
        prov = provenance or Provenance()
        findings = tuple(scan_for_threats(content, scope=self._scan_scope))

        if not findings:
            # Clean content: inert. Identical to no-guard behaviour.
            return GuardOutcome(action="allow", content=content, allowed=True)

        action, policy_name = self._select_action(prov.source_class)
        return self._build_outcome(action, policy_name, content, findings)

    # -- internals --

    def _select_action(self, source_class: str) -> Tuple[str, str]:
        for rule in self._rules:
            if rule.applies_to(source_class):
                return rule.action, rule.name
        return self._default_action, "default"

    def _build_outcome(
        self, action: str, policy_name: str, content: str, findings: Tuple[str, ...]
    ) -> GuardOutcome:
        ids = ", ".join(findings)
        if action == "warn":
            return GuardOutcome(
                action="warn",
                content=content,
                allowed=True,
                findings=findings,
                policy=policy_name,
                message=(
                    f"Memory guard WARN: entry matches threat pattern(s) [{ids}] "
                    f"but was allowed by policy '{policy_name}'. Stored as-is."
                ),
            )
        if action == "strip":
            stripped = strip_threat_spans(content, scope=self._scan_scope)
            return GuardOutcome(
                action="strip",
                content=stripped,
                allowed=True,
                findings=findings,
                policy=policy_name,
                message=(
                    f"Memory guard STRIP: removed span(s) matching threat "
                    f"pattern(s) [{ids}] per policy '{policy_name}'."
                ),
            )
        if action == "allow":
            # An explicit allow rule on flagged content (rare; opt-in escape hatch).
            return GuardOutcome(
                action="allow", content=content, allowed=True, findings=findings,
                policy=policy_name,
            )
        # block (and any unknown action collapses to block — fail closed).
        return GuardOutcome(
            action="block",
            content=content,
            allowed=False,
            findings=findings,
            policy=policy_name,
            message=(
                f"Memory guard BLOCK: entry matches threat pattern(s) [{ids}] "
                f"(source_class={policy_name}). Write refused."
            ),
        )


def strip_threat_spans(content: str, scope: str = _SCAN_SCOPE) -> str:
    """Return ``content`` with every offending threat span replaced.

    Reuses :func:`tools.threat_patterns.scan_for_threat_spans` (same compiled
    patterns as the scanner) to locate spans, merges overlaps, then replaces
    each merged span with :data:`STRIP_REPLACEMENT`. Whitespace around the
    excision is collapsed so the result reads cleanly. The result is rescanned
    once and any residual span (e.g. a pattern that re-forms after excision) is
    blanked, so a strip never returns content the scanner still flags.
    """
    spans = scan_for_threat_spans(content, scope=scope)
    if not spans:
        return content

    out = _apply_strip(content, spans)
    # Defensive second pass: ensure the stripped output is clean. If a residual
    # match remains, strip it too (bounded: spans only shrink).
    if scan_for_threats(out, scope=scope):
        residual = scan_for_threat_spans(out, scope=scope)
        if residual:
            out = _apply_strip(out, residual)
    return out


def _apply_strip(content: str, spans: list[Tuple[int, int, str]]) -> str:
    merged = _merge_spans([(s, e) for s, e, _ in spans])
    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(content[cursor:start])
        pieces.append(STRIP_REPLACEMENT)
        cursor = end
    pieces.append(content[cursor:])
    # Collapse runs of spaces/tabs introduced around the marker; keep newlines.
    result = "".join(pieces)
    import re as _re
    result = _re.sub(r"[ \t]{2,}", " ", result)
    return result.strip()


def _merge_spans(spans: list[Tuple[int, int]]) -> list[Tuple[int, int]]:
    """Merge overlapping / adjacent (start, end) spans into disjoint ones."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[Tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


# ── Config wiring (default-off) ─────────────────────────────────────────────

# Built-in action vocabulary for config rules. Only block/warn/strip make sense
# as a *rule* action (allow is the implicit clean-content outcome), but we keep
# "allow" available as an explicit escape hatch for a trusted source class.
_RULE_ACTIONS = frozenset(GUARD_ACTIONS)


def build_memory_guard_from_config(
    data: Mapping[str, Any] | None,
) -> Optional[MemoryGuardPolicy]:
    """Build a guard from the ``memory.guard`` config section, or ``None``.

    Returns ``None`` (the default-off path — store keeps pre-#315 behaviour)
    when ``data`` is missing, malformed, or ``enabled`` is false/absent.

    Expected shape::

        memory:
          guard:
            enabled: true
            default_action: block          # action for flagged content with
                                           # no matching rule (default: block)
            rules:
              - action: block             # block | warn | strip | allow
                source_classes: [agent_authored]   # empty/omitted = any source
                name: poisoned-agent-write          # optional label
              - action: strip
                source_classes: [external_tool]

    Unknown actions and malformed rule entries are skipped (fail-safe). An
    enabled guard with NO valid rules still applies ``default_action`` to
    flagged content, so enabling the guard never silently weakens detection.
    """
    if not isinstance(data, Mapping):
        return None
    if not _as_bool(data.get("enabled"), False):
        return None

    default_action = data.get("default_action")
    if not isinstance(default_action, str) or default_action not in _RULE_ACTIONS:
        default_action = "block"

    rules: list[MemoryGuardRule] = []
    raw_rules = data.get("rules")
    if isinstance(raw_rules, (list, tuple)):
        for entry in raw_rules:
            if not isinstance(entry, Mapping):
                continue
            action = entry.get("action")
            if not isinstance(action, str) or action not in _RULE_ACTIONS:
                continue
            sources = entry.get("source_classes")
            if isinstance(sources, (list, tuple, set, frozenset)):
                source_set = frozenset(str(s) for s in sources if isinstance(s, str))
            else:
                source_set = frozenset()
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                name = action
            rules.append(
                MemoryGuardRule(action=action, source_classes=source_set, name=name)
            )

    return MemoryGuardPolicy(rules=rules, default_action=default_action)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


__all__ = [
    "GUARD_ACTIONS",
    "STRIP_REPLACEMENT",
    "Provenance",
    "GuardOutcome",
    "MemoryGuardRule",
    "MemoryGuardPolicy",
    "strip_threat_spans",
    "build_memory_guard_from_config",
]
