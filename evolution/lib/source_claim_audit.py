"""Post-hoc claim audit for the evolution research report (issue #3521).

Adopts the RIDAR finding (Siow et al., arXiv:2610.31372) that a research
artifact must be re-checked against the sources it already cites *before*
downstream stages treat its sentences as facts — not by adding a detector
model inside the agent loop, but by one cheap deterministic pass over the
finished draft.

The audit needs two inputs:

* the draft report (``research/YYYY-MM-DD.md``), and
* the **evidence sidecar** the research stage writes next to it
  (``research/YYYY-MM-DD.sources.json``): ``{url: verbatim excerpt}`` for every
  URL the report cites, holding the exact sentences the figures were taken
  from.

For every *checkable figure* (a count, percentage or date) in a finding that
cites a URL, the audit asks one question: **does that figure occur in the
excerpt the report itself cites for it?** A figure that does not is tagged
``unverified`` and fails the gate — that is a transcription drift or a
fabricated number, and it is exactly the failure class this pipeline has
already shipped (a "count" that was never in the page it came from). A figure
with no citation in its finding is tagged ``unattributed`` but does not fail
the gate; the report schema computes plenty of numbers (priority scores,
impact) that have no external source by design.

Everything here is pure, deterministic, offline: no network, no LLM, no
dependency. When the sidecar is absent the audit reports ``skipped`` so the
stage keeps working while research has not yet started writing sidecars.

The thin CLI is :mod:`scripts.evolution_claim_audit`; the wiring is the
``evolution-issues`` stage, the first stage with a terminal that consumes the
report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "Claim",
    "ClaimAuditResult",
    "audit_report",
    "extract_figures",
    "load_sources",
    "render_summary",
]

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

_URL_RE = re.compile(r"https?://[^\s<>\)\]\"']+")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TEXT_DATE_RE = re.compile(
    rf"\b(\d{{1,2}})\s+({_MONTH_RE})\.?,?\s*(\d{{4}})\b|\b({_MONTH_RE})\.?\s+(\d{{1,2}}),?\s*(\d{{4}})\b",
    re.IGNORECASE,
)

# What a count needs to look like a claim about the world rather than a section
# number: an explicit unit ("85.2%", "412 ms"), or a count noun right after it
# ("60 trajectories", "300 wild trajectories", "12 open issues").
_UNITS = (
    r"ms|s|sec|seconds?|min(?:ute)?s?|hours?|tokens?|usd|\\\$|bytes?|kb|mb|gb|"
    r"ghz|mhz|x|times"
)
_COUNT_NOUNS = (
    r"trajector(?:y|ies)|runs?|files?|issues?|prs?|pull requests?|repositor(?:y|ies)|"
    r"repos?|stars?|commits?|models?|papers?|agents?|users?|sessions?|tokens?|lines?|"
    r"tests?|days?|weeks?|months?|years?|calls?|requests?|benchmarks?|samples?|"
    r"examples?|projects?|skills?|findings?|candidates?|proposals?|installations?"
)
_UNIT_RE = re.compile(
    r"(?<![\w.#:/])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"((?:\s*%|\s*percent\b|\s*×)"
    r"|(?:\s+(?:" + _UNITS + r")\b)"
    r"|(?:[-\s]+(?:(?!\b(?:of|the|a|an|in|at|to|from|for|and|or|on|by|with|is|are)\b)\w+[-\s]+){0,2}(?:"
    + _COUNT_NOUNS
    + r")\b))?",
    re.IGNORECASE,
)

# Figures the pipeline computes for itself (priority maths, impact/effort
# ratings) are not claims about the outside world and are never checked
# against a source.
_COMPUTED_LINE_RE = re.compile(
    r"^\s*[-*]?\s*\*\*(priority score|impact|effort)\*\*", re.IGNORECASE
)
_BLOCK_SPLIT_RE = re.compile(r"^#{2,6}\s", re.MULTILINE)
# Internal references ("issue #3514", "v1.2", "arXiv:2610.31372") identify our
# own artefacts or the citation itself, not a fact from the cited page.
_NOT_A_FIGURE_RE = re.compile(r"(?i)(?:arxiv:|doi:|issue\s*|pr\s*|#|v)\s*$")


@dataclass
class Claim:
    """One checkable figure and what the report says supports it."""

    figure: str
    kind: str  # "number" | "date"
    url: Optional[str]
    verdict: str  # "verified" | "unverified" | "unattributed"
    line: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "figure": self.figure,
            "kind": self.kind,
            "url": self.url,
            "verdict": self.verdict,
            "line": self.line,
        }


@dataclass
class ClaimAuditResult:
    """Verdict for one report."""

    claims: list[Claim] = field(default_factory=list)
    skipped: Optional[str] = None
    sources_available: int = 0

    @property
    def verified(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict == "verified"]

    @property
    def unverified(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict == "unverified"]

    @property
    def unattributed(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict == "unattributed"]

    @property
    def passed(self) -> bool:
        return not self.unverified

    def as_dict(self) -> dict[str, Any]:
        return {
            "skipped": self.skipped,
            "sources_available": self.sources_available,
            "passed": None if self.skipped else self.passed,
            "counts": {
                "claims": len(self.claims),
                "verified": len(self.verified),
                "unverified": len(self.unverified),
                "unattributed": len(self.unattributed),
            },
            "claims": [c.as_dict() for c in self.claims],
        }


def _strip_thousands(text: str) -> str:
    """``1,234`` -> ``1234`` (a comma followed by exactly three digits)."""
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)


def _normalize(text: str) -> str:
    """Normalize the noise that legitimately differs between a page and a draft."""
    out = (
        (text or "")
        .replace("\u2212", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    out = out.replace("\u00a0", " ").replace("\u2009", " ").replace("\u202f", " ")
    out = _strip_thousands(out)
    return re.sub(r"\s+", " ", out).strip().casefold()


def _date_forms(year: int, month: int, day: int) -> list[str]:
    """Every surface form a source may legitimately use for one date."""
    names = [k for k, v in _MONTHS.items() if v == month]
    short = min(names, key=len)
    long_name = max(names, key=len)
    forms = [f"{year:04d}-{month:02d}-{day:02d}"]
    for name in {short, long_name}:
        forms += [
            f"{name} {day}, {year}",
            f"{name} {day} {year}",
            f"{day} {name} {year}",
        ]
    return forms


def _figure_present(figure: str, kind: str, haystack: str) -> bool:
    """Is ``figure`` present in the (already normalized) evidence text?"""
    needle = _normalize(figure)
    if kind == "date":
        m = _ISO_DATE_RE.fullmatch(needle)
        forms = (
            _date_forms(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if m
            else [needle]
        )
        return any(_normalize(f) in haystack for f in forms)
    # Numbers are compared by their digits so "1,234" and "1234" agree, while a
    # longer number can never satisfy a shorter claim (23 must not match 1234).
    digits = re.sub(r"[^\d.]", "", needle).rstrip(".")
    if not digits:
        return False
    return (
        re.search(r"(?<![\d.])" + re.escape(digits) + r"(?![\d])", haystack) is not None
    )


def extract_figures(line: str) -> list[tuple[str, str]]:
    """Figures on one line, as ``(figure, kind)`` in reading order."""
    found: list[tuple[int, str, str]] = []
    # Digits inside a URL are the citation itself, not a claim about the world
    # ("2610.31372" in an arXiv link is not a figure).
    skip_spans: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _URL_RE.finditer(line)
    ]

    def _skipped(at: int) -> bool:
        return any(start <= at < end for start, end in skip_spans)

    for m in _ISO_DATE_RE.finditer(line):
        if _skipped(m.start()):
            continue
        found.append((m.start(), m.group(0), "date"))
        skip_spans.append((m.start(), m.end()))
    for m in _TEXT_DATE_RE.finditer(line):
        if _skipped(m.start()):
            continue
        day = int(m.group(1) or m.group(5))
        name = (m.group(2) or m.group(4)).lower().rstrip(".")
        year = int(m.group(3) or m.group(6))
        month = _MONTHS.get(name)
        if month and 1 <= day <= 31:
            found.append((m.start(), f"{year:04d}-{month:02d}-{day:02d}", "date"))
            skip_spans.append((m.start(), m.end()))
    for m in _UNIT_RE.finditer(line):
        if _skipped(m.start()):
            continue
        if _NOT_A_FIGURE_RE.search(line[max(0, m.start() - 8) : m.start()]):
            continue
        raw, unit = m.group(1), m.group(2)
        if unit is None and "." not in raw and "," not in raw:
            # A bare integer with no unit and no count noun is a step number or
            # a section index, not a claim.
            continue
        found.append((m.start(), raw, "number"))
    found.sort(key=lambda t: t[0])
    return [(figure, kind) for _, figure, kind in found]


def load_sources(path: Any) -> dict[str, str]:
    """Load the evidence sidecar; ``{}`` when absent or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}
    if isinstance(data, dict):
        pairs = list(data.items())
    elif isinstance(data, list):
        pairs = [
            (entry.get("url", ""), entry.get("text", ""))
            for entry in data
            if isinstance(entry, dict)
        ]
    else:
        return {}
    return {str(k).strip(): str(v or "") for k, v in pairs if str(k or "").strip()}


def _blocks(report_text: str) -> list[str]:
    """Split the report into finding blocks (a heading plus its body).

    A URL is cited once, on the finding's ``- **Source**`` line, while its
    figures live in the prose of the same finding — so attribution has to be
    per block, not per line.
    """
    marks = [m.start() for m in _BLOCK_SPLIT_RE.finditer(report_text or "")]
    if not marks:
        return [report_text or ""]
    out: list[str] = []
    if marks[0] > 0:
        out.append(report_text[: marks[0]])
    for i, at in enumerate(marks):
        stop = marks[i + 1] if i + 1 < len(marks) else len(report_text)
        out.append(report_text[at:stop])
    return out


def audit_report(report_text: str, sources: dict[str, str]) -> ClaimAuditResult:
    """Tag every checkable figure in the draft against its cited evidence."""
    result = ClaimAuditResult()
    if not sources:
        result.skipped = "no evidence sidecar"
        return result
    result.sources_available = len(sources)
    haystacks = {url: _normalize(text) for url, text in sources.items()}
    for block in _blocks(report_text):
        urls = [u.rstrip(".,;:") for u in _URL_RE.findall(block)]
        evidence = " ".join(haystacks.get(url, "") for url in urls)
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or _COMPUTED_LINE_RE.match(line):
                continue
            for figure, kind in extract_figures(line):
                if not urls or not evidence:
                    # No citation for this figure, or the report cites a URL the
                    # sidecar holds no excerpt for: the claim cannot be
                    # re-checked, which is not the same as being wrong — report
                    # it, but never call it fabricated.
                    verdict = "unattributed"
                else:
                    verdict = (
                        "verified"
                        if _figure_present(figure, kind, evidence)
                        else "unverified"
                    )
                result.claims.append(
                    Claim(figure, kind, urls[0] if urls else None, verdict, line)
                )
    return result


def render_summary(result: ClaimAuditResult) -> str:
    """One-line summary for the pipeline log."""
    if result.skipped:
        return f"[claim-audit] skipped: {result.skipped}"
    return (
        f"[claim-audit] {len(result.claims)} figures: "
        f"verified={len(result.verified)} unverified={len(result.unverified)} "
        f"unattributed={len(result.unattributed)} (sources={result.sources_available})"
    )
