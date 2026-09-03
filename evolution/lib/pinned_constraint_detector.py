# -*- coding: utf-8 -*-
"""Producer for the pinned-constraint mechanism (Governance Decay, arXiv:2606.22528).

``agent/context_compressor.py`` already ships the *consumer* half of this
defense: Slice A (#1774) parses ``_pinned_constraint`` metadata and inline
``[PINNED_CONSTRAINT]`` markers, Slice B (#1773) validates that each pinned
constraint survived summarization and re-injects the ones that vanished.

Nothing ever set a pin.  A repo-wide grep finds assignments only in
``tests/agent/test_pinned_constraint_*.py`` and in the compressor's own
re-injection path, so in production the defense protected an empty set: a user
typing "don't push until I review it" into Telegram produced an ordinary user
message with no flag and no marker, and the summarizer was free to drop it —
which is exactly the 30-59% soft-rule loss the Governance Decay paper measures.
This module is the missing producer.

Three design constraints come from that context, and all three are load-bearing:

1. **Durability cannot live on the message.**  ``_insert_message_rows``
   (hermes_state.py) persists an explicit column list; ``_pinned_constraint``
   is not one of them, so a flag written onto an in-memory message dict is
   lost at the next ``replace_messages`` / ``archive_and_compact``.  The
   compaction path therefore re-derives constraints from the surviving user
   turns on every pass rather than trusting a stored flag.  The JSON store on
   :class:`ConstraintRegistry` is available for callers that need pins to
   outlive the transcript itself; the compressor does not use it.
2. **User bytes are never rewritten.**  Injecting the inline marker into a
   stored user turn would mutate what the user said and invalidate the
   prompt-cache prefix.  :func:`mark_pinned_messages` sets metadata only.
3. **Ambiguity resolves toward restriction.**  Every tie-break here fails
   closed: a clause that is both affirmative and negated reads as a
   prohibition, and a constraint whose referent cannot be resolved is kept as
   an action-class-wide pin carrying an explicit "confirm first" clarifier
   rather than dropped.  Dropping is the only outcome that can let the agent
   act against a live instruction.

Discrimination is deliberately narrow.  Pinning every imperative would flood
the compressor's protected region and starve the working context, so a clause
is pinned only when it is a *binding speech act* (prohibition, deferral, scope
exclusion, revocation) **and** its object is an *irreversible or out-of-turn*
action.  "Fix the tests" and "don't use recursion here" are not constraints;
"don't open the PR until I review it" is.

Two limits are deliberate and worth knowing before extending this:

* **The cap is a trade-off, not an invariant.**  "Never lose a constraint" and
  "keep the protected region bounded" cannot both hold absolutely.  Past
  :data:`MAX_ACTIVE_PINS` a new pin is refused (never an existing one evicted)
  and the refusal is logged at WARNING.  The ceiling sits far above realistic
  use; a conversation carrying that many distinct governance rules at once is
  the case to revisit.
* **Release is not inferred.**  The compaction path calls
  ``ingest(..., apply_revocations=False)``.  Four adversarial review rounds
  produced a new deletion path for every rule that tried to read approval out
  of text ("not approved to ship" parsed as approval, a PR "LGTM" clearing an
  unrelated database gate, an approval reviving after a rotation).  A gate the
  user already lifted costs one re-injected line, and the model still sees the
  approval in the transcript; a rule dropped while they still mean it does not
  bound its cost.

Known and out of scope: ``_pinned_constraint_survives`` (Slice B, predates this
module) accepts 80% token overlap, so a summary that merely reuses a
constraint's words can be judged to have preserved it.  Tightening that
heuristic is a separate change against a hot path.

Pure-python, deterministic, no LLM or network.  Injectable clock seam.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger(__name__)

__all__ = [
    "ActionClass",
    "ConstraintKind",
    "ConstraintRegistry",
    "DetectedConstraint",
    "detect_constraints",
    "get_default_registry_dir",
    "mark_pinned_messages",
]

# Mirrors agent/context_compressor.py so a caller can feed this module's output
# straight into the existing consumer without importing the compressor.
PINNED_CONSTRAINT_METADATA_KEY = "_pinned_constraint"

# A pinned clause is re-injected verbatim into a system message on every
# compaction, so an over-long one costs tokens on every rotation forever.
MAX_CLAUSE_CHARS = 240

# Appended to a pin whose referent could not be resolved.  Keeping the pin and
# forcing a question is safe; dropping it is not.
UNRESOLVED_REFERENT_HINT = " (referent unresolved — confirm with the user before acting)"


def get_default_registry_dir() -> Path:
    """Default registry storage dir (under ``HERMES_HOME``)."""
    base = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    p = Path(base) / "pinned_constraints"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


class ConstraintKind(str, Enum):
    """The binding speech act a clause performs."""

    PROHIBITION = "prohibition"  # "never force-push to main"
    DEFERRAL = "deferral"  # "don't open the PR until I review it"
    SCOPE_EXCLUSION = "scope_exclusion"  # "don't touch agent/context_folding.py"
    REVOCATION = "revocation"  # "ok, ship it"


class ActionClass(str, Enum):
    """Blast-radius family of the action a clause governs.

    Only actions that are irreversible, externally visible, or taken outside
    the user's turn qualify.  Everything else is ordinary task instruction and
    is left to normal context handling.  ``ANY`` is reserved for a bare
    approval ("go ahead") that names no action at all.
    """

    VCS_PUBLISH = "vcs_publish"
    DESTRUCTIVE_FS = "destructive_fs"
    EXTERNAL_COMMS = "external_comms"
    DEPLOY_RELEASE = "deploy_release"
    AUTONOMY = "autonomy"
    FILE_SCOPE = "file_scope"
    ANY = "any"


def _phrase_re(phrases: Iterable[str]) -> re.Pattern[str]:
    """Compile *phrases* into one alternation with word-ish boundaries.

    Plain substring tests are wrong here: "tag" hides inside "stage", "any"
    inside "many", "commit" inside "commitment".  ``(?<!\\w)``/``(?!\\w)``
    keeps multi-word and hyphenated phrases working where ``\\b`` would not.

    The apostrophe belongs in the boundary class: without it "you can" matches
    inside "you can't", turning a prohibition into an approval.
    """
    parts = sorted((re.escape(p) for p in phrases), key=len, reverse=True)
    return re.compile(
        r"(?<![\w'’])(?:" + "|".join(parts) + r")(?![\w'’])", re.IGNORECASE
    )


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_ACTION_PHRASES: List[Tuple[ActionClass, Tuple[str, ...]]] = [
    (
        ActionClass.VCS_PUBLISH,
        (
            "force-push", "force push", "open a pr", "open the pr", "open pr",
            "create a pr", "create the pr", "create pr", "pull request",
            "merge", "rebase", "cherry-pick", "publish", "push", "tag",
        ),
    ),
    (
        ActionClass.DESTRUCTIVE_FS,
        (
            "rm -rf", "drop table", "reset --hard", "delete", "remove",
            "overwrite", "truncate", "wipe", "drop",
        ),
    ),
    (
        ActionClass.EXTERNAL_COMMS,
        (
            "send an email", "send email", "send a message", "post to",
            "reply to", "announce", "notify", "email", "send", "post", "dm",
        ),
    ),
    (
        ActionClass.DEPLOY_RELEASE,
        (
            "roll out", "rollout", "npm publish", "deploy", "release",
            "restart", "migrate", "ship",
        ),
    ),
    (
        ActionClass.AUTONOMY,
        (
            "without asking", "without approval", "without my review",
            "without checking", "on your own", "self-update", "auto-update",
            "unattended", "commit",
        ),
    ),
]

_ACTION_RES: List[Tuple[ActionClass, re.Pattern[str]]] = [
    (cls, _phrase_re(phrases)) for cls, phrases in _ACTION_PHRASES
]

# A bare approval releases a pending gate.  Only unambiguous work-approval
# words belong here: a false-positive revocation DELETES a live user
# constraint, which is the one direction this module must never fail in.
# Deliberately excluded: "all clear" / "green light" (appear in dashboards and
# CI output), "never mind" / "disregard" / "cancel that" (they refer to some
# previous statement, not necessarily the constraint).
_GENERIC_REVOCATION_RE = _phrase_re(
    ("go ahead", "go for it", "approved", "i approve", "lgtm")
)
# Approvals that are only binding when they name an action ("you can push
# now", "ok to deploy").  Bare "proceed" or "you can ..." in running prose is
# not an approval — that text appears in provider error messages and prompt
# scaffolding that the runtime injects as role="user".
_SCOPED_REVOCATION_RE = _phrase_re(
    (
        "ship it", "push it", "send it", "looks good", "you can", "you may",
        "ok to", "okay to", "fine to", "proceed", "feel free",
        "ignore what i said", "ignore my earlier", "forget what i said",
    )
)

# Filler that may surround a bare approval without diluting it.
_APPROVAL_FILLER = frozenset(
    {"ok", "okay", "yes", "yep", "sure", "alright", "right", "great", "cool",
     "thanks", "please", "now", "then", "and", "so", "well", "fine"}
)
# A real approval is a short message.  A pasted CI log that happens to contain
# "LGTM" must not release a gate.
MAX_REVOCATION_MESSAGE_CHARS = 200
# Upper bound on simultaneously active pins. At the cap a NEW pin is refused;
# existing pins are never evicted, because evicting one would delete a
# constraint the user is still relying on.
MAX_ACTIVE_PINS = 32

# Bare negation, checked only around an approval phrase.  It is deliberately
# NOT in _PROHIBITION_RE: "not" is far too common to mark a clause as a
# prohibition, but "not approved" must never read as an approval.
_NEGATED_APPROVAL_RE = _phrase_re(("not", "no", "isn't", "wasn't", "won't", "never"))

_PROHIBITION_RE = _phrase_re(
    ("don't", "dont", "do not", "never", "must not", "mustn't", "cannot",
     "can't", "no longer", "stop", "avoid", "refrain from")
)

# A deferral is a prohibition with a human-gated release condition.
_DEFERRAL_GATE_RE = re.compile(
    r"(?<!\w)(?:until|before|till)(?!\w)[^.;]{0,60}?"
    r"(?<!\w)(?:i|we|you)(?!\w)[^.;]{0,40}?"
    r"(?<!\w)(?:review|reviewed|approve|approved|check|checked|look|looked|"
    r"say|said|confirm|confirmed|sign off|signed off)(?!\w)",
    re.IGNORECASE,
)
_DEFERRAL_CUE_RE = _phrase_re(
    ("hold off", "hold on", "wait until", "wait for", "wait till", "pause",
     "sit on it")
)

# An irreversible verb needs an irreversible TARGET.  Without this, ordinary
# programming talk pins: "do not push values into the buffer", "do not merge
# the lists", "do not delete the AST node" are all about in-process data.
_BOUNDARY_NOUN_RE = _phrase_re(
    ("main", "master", "remote", "origin", "upstream", "prod", "production",
     "staging", "repo", "repository", "branch", "server", "database", "db",
     "pr", "pull request", "issue", "release", "registry", "npm", "pypi",
     "customer", "customers", "user", "users", "client", "slack", "telegram",
     "email", "disk", "volume", "bucket", "secrets", "credentials", "env")
)
# Verbs whose blast radius is external by definition, so they need no target.
_INHERENTLY_BOUNDARY_RE = _phrase_re(
    ("force-push", "force push", "open a pr", "open the pr", "open pr",
     "create a pr", "create the pr", "create pr", "pull request", "publish",
     "npm publish", "deploy", "roll out", "rollout", "rebase", "self-update",
     "auto-update", "without asking", "without approval", "without my review",
     "without checking", "unattended", "rm -rf", "drop table", "reset --hard")
)

_SCOPE_CUE_RE = _phrase_re(
    ("touch", "modify", "edit", "change", "work on", "go near", "leave",
     "rewrite", "refactor")
)

# The noun that makes an unresolved scope exclusion a *boundary* constraint.
# "don't touch that file" must pin; "don't change the approach" must not.
_SCOPE_NOUN_RE = _phrase_re(
    ("file", "files", "dir", "directory", "folder", "repo", "repository",
     "branch", "config", "database", "db", "table", "migration", "secret",
     "secrets", "env", "credentials", "key", "keys")
)

# --- exclusions -------------------------------------------------------------

# "don't forget to push" is a positive instruction wearing a negation.
_FALSE_NEGATION_RE = re.compile(
    r"(?<!\w)don'?t\s+(?:forget|hesitate|worry|bother)(?!\w)", re.IGNORECASE
)

# A prohibition attributed to someone, not issued now.
_REPORTED_SPEECH_RE = re.compile(
    r"(?<!\w)(?:he|she|they|the user|someone|it)\s+(?:said|told|wrote|mentioned|asked)(?!\w)"
    r"|(?<!\w)(?:you|i)\s+(?:said|told me|wrote|mentioned)(?!\w)"
    r"|(?<!\w)earlier\s+(?:i|you)\s+said(?!\w)"
    r"|(?<!\w)the\s+(?:tool|log|logs|output|error|docs?|readme|guide|policy|"
    r"response|server|api|ci|pipeline|build|report)(?:\s+\w+){0,2}\s+"
    r"(?:say|says|said|state|states|warn|warns|reports?|complains?)(?!\w)"
    r"|^\s*according to(?!\w)",
    re.IGNORECASE,
)

# Leading conditional / hypothetical, not a live order.
_HYPOTHETICAL_RE = re.compile(
    r"^\s*(?:if|what if|in case|unless|suppose|imagine|assuming|whenever)(?!\w)",
    re.IGNORECASE,
)

# Question without a question mark.  The negative lookahead is essential:
# "Do not push" is an imperative, not an interrogative.
_INTERROGATIVE_LEAD_RE = re.compile(
    r"^\s*(?:what|why|how|can|could|should|would|will|do|does|did|is|are|"
    r"was|were|shall|may)(?!\w)(?!\s+not(?!\w))(?!\s*n't)",
    re.IGNORECASE,
)

# First-person incapacity: "I can't push to main because my access was
# revoked" states a fact about the user, not an instruction to the agent.
_SELF_INCAPACITY_RE = re.compile(
    r"(?<![\w'’])(?:i|we)\s+"
    r"(?:can'?t|cannot|couldn'?t|am unable to|are unable to|"
    r"do not have|don'?t have|lack)(?![\w'’])",
    re.IGNORECASE,
)

# Past-tense report about what happened, not an instruction about what to do.
_PAST_REPORT_RE = re.compile(
    r"(?<!\w)(?:didn'?t|did not|wasn'?t|weren'?t|couldn'?t|hasn'?t|haven'?t|hadn'?t)(?!\w)",
    re.IGNORECASE,
)

# Implementation advice: the object is a code construct, not a boundary action.
_CODE_ADVICE_RE = _phrase_re(
    ("recursion", "loop", "loops", "regex", "import", "imports", "exception",
     "exceptions", "class", "classes", "variable", "variables", "comment",
     "comments", "type hint", "type hints", "docstring", "docstrings",
     "indentation", "naming", "abstraction", "abstractions", "library",
     "dependency", "framework", "mock", "mocks", "globals", "lambda",
     "inheritance", "magic number", "magic numbers", "one-liner")
)

# Unresolved demonstratives make a re-injected clause ambiguous: after the
# turns that established the referent are pruned, "don't touch that file"
# no longer says which file.
_DEMONSTRATIVE_RE = _phrase_re(
    ("that", "this", "those", "these", "it", "them", "there", "the same")
)

# A concretely-named target: a path, a dotted filename, a branch, an issue/PR
# number, or a quoted name.
_QUALIFIED_TARGET_RE = re.compile(
    r"(?P<quoted>`[^`]+`)"
    r"|(?P<path>(?:[\w.-]+/)+[\w.-]+)"
    r"|(?P<file>(?<!\w)[\w-]+\.(?:py|md|js|ts|tsx|json|ya?ml|toml|sh|sql|txt|html|css|rs|go)(?!\w))"
    r"|(?P<ref>#\d+)"
    r"|(?P<branch>(?<!\w)(?:main|master|develop|production|prod|staging)(?!\w))"
)

_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?;\n])\s+|\n+")

# A bare approval names no action, so it cannot be matched by class.  These
# two publish-shaped families are treated as one for revocation, because
# "ok, ship it" plainly releases "don't push until I review".
_REVOCATION_FAMILIES: Dict[ActionClass, Set[ActionClass]] = {
    ActionClass.VCS_PUBLISH: {ActionClass.VCS_PUBLISH, ActionClass.DEPLOY_RELEASE},
    ActionClass.DEPLOY_RELEASE: {ActionClass.VCS_PUBLISH, ActionClass.DEPLOY_RELEASE},
}


# ---------------------------------------------------------------------------
# Detected constraint
# ---------------------------------------------------------------------------

def _semantic_key(clause: str) -> str:
    """Identity of a standing rule: the clause minus its negation and noise.

    "don't push to main" and "do not push to main" are the same rule and must
    collapse; "never force-push to main" is a different, stronger one and must
    not be overwritten by either.
    """
    text = _PROHIBITION_RE.sub(" ", (clause or "").lower())
    return " ".join(re.findall(r"[\w'-]+", text))


@dataclass
class DetectedConstraint:
    """One binding clause extracted from a user turn.

    ``clause`` is the verbatim span that will be re-injected on compaction —
    the clause alone, never the chatty envelope it arrived in.  ``qualified``
    is False when the object is an unresolved demonstrative; the pin is still
    kept (action-class-wide) but :meth:`ConstraintRegistry.pin_texts` appends
    a clarifier so the agent asks instead of guessing a referent.
    """

    kind: ConstraintKind
    action_class: ActionClass
    clause: str
    object_ref: Optional[str] = None
    qualified: bool = True
    created_at: float = 0.0
    source_role: str = "user"

    @property
    def key(self) -> Tuple[str, str, str]:
        """Identity used for supersession: (kind, action class, object).

        ``kind`` is part of the identity on purpose.  Without it a later
        "don't push to main until I review" (a temporary gate) overwrites a
        standing "never force-push to main" at the same action/object, and
        releasing the gate then silently takes the standing rule with it.
        """
        # Every kind carries a semantic discriminator.  Two live gates on the
        # same action and object are still two gates: "don't push to main
        # until I review it" and "...until I check the logs" must both
        # survive, and "never force-push to main" must not be replaced by the
        # weaker "don't push to main".  Revocation matches on the constraint's
        # own fields rather than on this key.
        return (self.kind.value, self.action_class.value, _semantic_key(self.clause))

    def display_text(self) -> str:
        """Clause as re-injected, with the ambiguity clarifier when needed."""
        if self.qualified:
            return self.clause
        return self.clause + UNRESOLVED_REFERENT_HINT

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["action_class"] = self.action_class.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DetectedConstraint":
        return cls(
            kind=ConstraintKind(data["kind"]),
            action_class=ActionClass(data["action_class"]),
            clause=str(data.get("clause", "")),
            object_ref=data.get("object_ref"),
            qualified=bool(data.get("qualified", True)),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            source_role=str(data.get("source_role", "user")),
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _split_clauses(text: str) -> List[str]:
    """Split *text* into candidate clauses, preserving original casing."""
    if not text:
        return []
    out: List[str] = []
    for part in _CLAUSE_SPLIT_RE.split(text):
        # A trailing "but ..." / "and ..." often carries its own speech act.
        for sub in re.split(r",\s+(?=but\b|and\b|then\b)", part or ""):
            s = sub.strip()
            if s:
                out.append(s)
    return out


def _is_excluded(clause: str) -> bool:
    """True when *clause* looks binding but is not a live order."""
    return bool(
        _FALSE_NEGATION_RE.search(clause)
        or _SELF_INCAPACITY_RE.search(clause)
        or _REPORTED_SPEECH_RE.search(clause)
        or _HYPOTHETICAL_RE.search(clause)
        or clause.rstrip().endswith("?")
        or _INTERROGATIVE_LEAD_RE.match(clause)
        or _PAST_REPORT_RE.search(clause)
        or _CODE_ADVICE_RE.search(clause)
    )


def _match_action(clause: str) -> Optional[Tuple[ActionClass, str]]:
    """Return the (class, matched phrase) of the boundary action, if any.

    Longest match wins so "force-push" beats "push" and the more specific
    class is chosen when two families both match.
    """
    best: Optional[Tuple[ActionClass, str]] = None
    for action_class, pattern in _ACTION_RES:
        for m in pattern.finditer(clause):
            phrase = m.group(0)
            if best is None or len(phrase) > len(best[1]):
                best = (action_class, phrase)
    return best


def _extract_object_ref(clause: str) -> Optional[str]:
    """Return a concretely-named target mentioned in *clause*, if any."""
    m = _QUALIFIED_TARGET_RE.search(clause)
    if not m:
        return None
    return (m.group(0) or "").strip().strip("`\"'") or None


def _is_revocation(clause: str) -> bool:
    """True when *clause* releases a constraint rather than imposing one.

    Matched approval phrases are blanked before the negation test, so
    "don't push it" (which contains the approval phrase "push it") still reads
    as a prohibition, and a clause carrying both readings fails closed.
    """
    if not (_GENERIC_REVOCATION_RE.search(clause) or _SCOPED_REVOCATION_RE.search(clause)):
        return False
    remainder = _SCOPED_REVOCATION_RE.sub(" ", _GENERIC_REVOCATION_RE.sub(" ", clause))
    if _NEGATED_APPROVAL_RE.search(remainder):
        # "not approved to ship", "this isn't approved" — a rejection. Checked
        # here rather than only in _is_bare_approval, because a named action
        # reaches _classify_kind without ever consulting that helper.
        return False
    return not _PROHIBITION_RE.search(remainder)


def _is_bare_approval(clause: str) -> bool:
    """True when the clause IS an approval, not prose containing one.

    "go ahead" releases a gate; "go ahead and fix the tests" approves the
    tests, and "the dashboard says all clear" approves nothing.  Requiring the
    approval to carry the clause keeps stray matches from deleting a live
    constraint.
    """
    if not _GENERIC_REVOCATION_RE.search(clause) or not _is_revocation(clause):
        return False
    remainder = _SCOPED_REVOCATION_RE.sub(" ", _GENERIC_REVOCATION_RE.sub(" ", clause))
    words = [
        w
        for w in re.findall(r"[\w'-]+", remainder.lower())
        if w not in _APPROVAL_FILLER
    ]
    return not words


def _classify_kind(clause: str, action_class: ActionClass) -> Optional[ConstraintKind]:
    """Decide which binding speech act *clause* performs, if any."""
    if _is_revocation(clause):
        return ConstraintKind.REVOCATION

    negated = _PROHIBITION_RE.search(clause) is not None
    gated = _DEFERRAL_GATE_RE.search(clause) is not None
    cued_deferral = _DEFERRAL_CUE_RE.search(clause) is not None

    if negated and gated:
        return ConstraintKind.DEFERRAL
    if cued_deferral and not negated:
        # "hold off on the deploy" is a gate; "don't pause the deploy" is the
        # opposite instruction and must not become a releasable gate.
        return ConstraintKind.DEFERRAL
    if not negated:
        return None
    if action_class is ActionClass.FILE_SCOPE or _SCOPE_CUE_RE.search(clause):
        return ConstraintKind.SCOPE_EXCLUSION
    return ConstraintKind.PROHIBITION


def detect_constraints(
    text: str,
    *,
    role: str = "user",
    now: Optional[Callable[[], float]] = None,
) -> List[DetectedConstraint]:
    """Extract binding constraints from a single message *text*.

    Only direct user speech is binding; an assistant restating a rule must not
    create one, or the agent could pin itself.  Returns an empty list for
    anything that is not a prohibition / deferral / scope exclusion /
    revocation over an irreversible or out-of-turn action.
    """
    if role != "user" or not isinstance(text, str) or not text.strip():
        return []

    clock = now or time.time
    found: List[DetectedConstraint] = []
    seen: Set[Tuple[str, str, str]] = set()
    # A genuine approval is a short message.  A pasted CI log containing
    # "LGTM" must never release a gate.
    allow_revocation = len(text.strip()) <= MAX_REVOCATION_MESSAGE_CHARS

    for clause in _split_clauses(text):
        if _is_excluded(clause):
            continue

        matched = _match_action(clause)
        object_ref = _extract_object_ref(clause)
        revocation = _is_revocation(clause)
        if revocation and not allow_revocation:
            continue

        if matched is None:
            if revocation and _is_bare_approval(clause):
                # Only an unambiguous approval that carries the whole clause
                # may revoke without naming an action.  Prose that merely
                # contains an approval word is not an approval — accepting it
                # would let ordinary text delete a live user constraint.
                action_class = ActionClass.ANY
            elif not revocation and _SCOPE_CUE_RE.search(clause) and (
                object_ref or _SCOPE_NOUN_RE.search(clause)
            ):
                # A scope verb over a filesystem/repo noun is a boundary
                # constraint even when the referent stays unresolved
                # ("don't touch that file") — dropping it is the only
                # outcome that lets the agent act against a live order.
                action_class = ActionClass.FILE_SCOPE
            else:
                continue
        else:
            action_class = matched[0]
            # A named path plus a scope verb is a file-scope constraint even
            # when a publish verb also appears in the same clause.
            if (
                action_class is not ActionClass.AUTONOMY
                and object_ref
                and ("/" in object_ref or "." in object_ref)
                and _SCOPE_CUE_RE.search(clause)
            ):
                action_class = ActionClass.FILE_SCOPE

        kind = _classify_kind(clause, action_class)
        if kind is None:
            continue

        # An irreversible verb needs an irreversible target.  Applied to plain
        # prohibitions only: a deferral names a human approver and a scope
        # exclusion already carries an object, so both are governance on their
        # face.  A bare demonstrative ("don't push it") also passes — the user
        # is pointing at something they just named, and it is kept as an
        # unqualified pin.  Without this rule, "do not push values into the
        # buffer" and "do not delete the AST node" pin as governance.
        if kind is ConstraintKind.PROHIBITION and not (
            object_ref
            or _BOUNDARY_NOUN_RE.search(clause)
            or _INHERENTLY_BOUNDARY_RE.search(clause)
            or _DEMONSTRATIVE_RE.search(clause)
        ):
            continue

        qualified = object_ref is not None or not _DEMONSTRATIVE_RE.search(clause)

        clean = " ".join(clause.split())[:MAX_CLAUSE_CHARS]
        dedupe = (kind.value, action_class.value, clean.lower())
        if dedupe in seen:
            continue
        seen.add(dedupe)

        found.append(
            DetectedConstraint(
                kind=kind,
                action_class=action_class,
                clause=clean,
                object_ref=object_ref,
                qualified=qualified,
                created_at=float(clock()),
                source_role=role,
            )
        )
    return found


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class ConstraintRegistry:
    """Durable set of active constraints for one session.

    Holds what the message row cannot: ``_insert_message_rows`` persists a
    fixed column list, so a metadata flag does not survive a compaction
    round-trip.  Pins are keyed by ``(action class, object)`` so a later
    approval supersedes the matching gate instead of accumulating forever —
    an append-only pin set would permanently deadlock the job it protected.
    """

    session_id: str = "default"
    storage_dir: Optional[Path] = None
    _active: Dict[Tuple[str, str, str], DetectedConstraint] = field(
        default_factory=dict, repr=False
    )

    # -- storage ------------------------------------------------------------

    @property
    def storage_file(self) -> Path:
        base = self.storage_dir or get_default_registry_dir()
        safe = re.sub(r"[^\w.-]", "_", self.session_id) or "default"
        return Path(base) / f"{safe}.json"

    # -- mutation -----------------------------------------------------------

    def ingest(
        self,
        constraints: Sequence[DetectedConstraint],
        *,
        apply_revocations: bool = True,
    ) -> List[DetectedConstraint]:
        """Apply *constraints* in order; return the ones that became active.

        Two rules keep this from ever losing a live constraint:

        * At :data:`MAX_ACTIVE_PINS` a NEW pin is refused rather than an
          existing one evicted.  Evicting to make room would delete a standing
          rule the user is still relying on; refusing merely fails to add a
          new one, which the re-derivation on the next pass can retry.
        * A revocation never releases a gate created by the same message, so
          "don't push until I review it, then go ahead" does not cancel itself
          within one turn.
        """
        activated: List[DetectedConstraint] = []
        this_message: Set[Tuple[str, str, str]] = set()
        for c in constraints:
            if c.kind is ConstraintKind.REVOCATION:
                if apply_revocations:
                    self._revoke(c, protect=this_message)
                continue
            if c.key not in self._active and len(self._active) >= MAX_ACTIVE_PINS:
                # Never silent: refusing is safer than evicting a rule the user
                # still relies on, but the caller must be able to see it.
                logger.warning(
                    "Pinned-constraint registry full (%d); refusing new pin: %s",
                    MAX_ACTIVE_PINS,
                    c.clause[:80],
                )
                continue
            self._active[c.key] = c
            this_message.add(c.key)
            activated.append(c)
        return activated

    def _revoke(
        self,
        revocation: DetectedConstraint,
        protect: Optional[Set[Tuple[str, str, str]]] = None,
    ) -> List[DetectedConstraint]:
        """Release the pending gates *revocation* supersedes.

        **Only DEFERRALs are ever released.**  A deferral is a temporary gate
        the user is explicitly waiting on, so an approval in the same action
        family clears it.  A PROHIBITION or SCOPE_EXCLUSION is a standing rule
        and is never cleared by inference: earlier designs tried exact-object
        matching, and every variant found a way to delete one
        ("ok, ship it" erasing "never force-push", a plain push approval
        erasing a force-push ban, an object-less rule collapsing to the same
        wildcard key).  Keeping a rule the user already lifted costs one
        re-injected line; dropping one they still mean is unbounded.
        """
        removed: List[DetectedConstraint] = []
        protected = protect or set()

        classes = {
            cls.value
            for cls in _REVOCATION_FAMILIES.get(
                revocation.action_class, {revocation.action_class}
            )
        }
        target_object = revocation.object_ref
        for key in list(self._active):
            current = self._active[key]
            if current.kind is not ConstraintKind.DEFERRAL or key in protected:
                continue
            if (
                revocation.action_class is not ActionClass.ANY
                and current.action_class.value not in classes
            ):
                continue
            if target_object is not None and current.object_ref not in (
                target_object,
                None,
            ):
                continue
            removed.append(self._active.pop(key))
        return removed

    def clear(self) -> None:
        self._active.clear()

    # -- reads --------------------------------------------------------------

    def active(self) -> List[DetectedConstraint]:
        """Active pins, oldest first."""
        return sorted(self._active.values(), key=lambda c: c.created_at)

    def pin_texts(self) -> List[str]:
        """Clause texts, ready for the compressor's re-injection path."""
        return [c.display_text() for c in self.active()]

    def summary(self) -> str:
        items = self.active()
        if not items:
            return "No active pinned constraints."
        lines = [f"{len(items)} active pinned constraint(s):"]
        lines.extend(
            f"- [{c.kind.value}/{c.action_class.value}] {c.display_text()}" for c in items
        )
        return "\n".join(lines)

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "constraints": [c.to_dict() for c in self.active()],
        }

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], storage_dir: Optional[Path] = None
    ) -> "ConstraintRegistry":
        reg = cls(session_id=str(data.get("session_id", "default")), storage_dir=storage_dir)
        for raw in data.get("constraints", []) or []:
            try:
                c = DetectedConstraint.from_dict(raw)
            except (KeyError, ValueError):
                continue
            reg._active[c.key] = c
        return reg

    def save(self, path: Optional[Union[str, Path]] = None) -> Path:
        target = Path(path) if path else self.storage_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target

    def load(self, path: Optional[Union[str, Path]] = None) -> bool:
        target = Path(path) if path else self.storage_file
        if not target.exists():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        self._active = self.from_dict(data, storage_dir=self.storage_dir)._active
        return True


# ---------------------------------------------------------------------------
# Message marking
# ---------------------------------------------------------------------------

def mark_pinned_messages(
    messages: List[Dict[str, Any]],
    registry: Optional[ConstraintRegistry] = None,
    *,
    now: Optional[Callable[[], float]] = None,
) -> List[DetectedConstraint]:
    """Flag constraint-bearing user turns for the compressor, in place.

    Sets only the ``_pinned_constraint`` metadata key the compressor already
    understands.  Message content is left byte-identical: rewriting a stored
    user turn to carry the inline marker would change what the user said and
    invalidate the prompt-cache prefix.

    Returns every constraint detected across *messages*, revocations included,
    in arrival order.
    """
    detected: List[DetectedConstraint] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        found = detect_constraints(content, role="user", now=now)
        if not found:
            continue
        detected.extend(found)
        if any(c.kind is not ConstraintKind.REVOCATION for c in found):
            msg[PINNED_CONSTRAINT_METADATA_KEY] = True
        # Ingest per message, not once at the end: the registry's
        # same-message guard must scope to one turn, or an approval in a
        # LATER turn would be treated as self-cancelling and never apply.
        if registry is not None:
            registry.ingest(found)
    return detected
