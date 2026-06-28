"""Persistent calibration memory for the Agent-as-a-Judge (child of #226, #305).

A small, additive layer on top of ``agent.agent_judge``.  It records past
judge verdicts alongside the *human* label for the same trace, and — when the
judge disagreed with the human — derives adjusted rubric-dimension weights and
re-evaluates with the recalibrated rubric.

Design (deliberately minimal):

* **Store** (``CalibrationStore``) — a sqlite table of calibration cases, one
  row per ``(session_id, trace_summary, judge_verdict, human_label)``.  All
  sqlite I/O is isolated here; nothing else in this module touches the disk.
  The DB path is configurable (``get_hermes_home()`` honours ``HERMES_HOME``)
  and defaults to ``<hermes-home>/judge_calibration.db``; pass an explicit
  ``db_path`` (e.g. a tmp file) in tests.
* **Weight math** (``derive_weight_adjustments`` / ``recalibrated_rubric``) —
  pure functions, no I/O, fully unit-testable.  Where the judge's pass/fail
  *disagreed* with the human label, the dimensions whose scores pulled the
  overall verdict *away* from the human are down-weighted; dimensions aligned
  with the human are up-weighted.  Agreement cases contribute nothing.
* **Engine helper** (``recalibrate_and_score``) — a thin ``AgentJudge``-facing
  convenience that re-scores a trace with the recalibrated rubric.

SAFETY / ADDITIVITY
-------------------
This is an **opt-in** path.  Nothing here is wired into ``AgentJudge.score``;
default scoring is byte-for-byte unchanged.  With NO calibration data (empty
store, or only agreement cases), ``recalibrated_rubric`` returns a rubric whose
weights are identical to the input — so a re-score reproduces the original
verdict exactly.  Calibration only ever *layers on top* of an existing rubric.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agent.agent_judge import (
    AgentJudge,
    JudgeVerdict,
    Rubric,
    RubricDimension,
)

logger = logging.getLogger(__name__)


# A human label is a binary good/bad judgment of the trace.  We accept the
# common spellings and normalise to a bool; ``None`` means "unlabelled / can't
# tell" and the case is ignored for re-weighting.
_GOOD_LABELS = frozenset({"good", "pass", "1", "true", "yes", "ok", "accept"})
_BAD_LABELS = frozenset({"bad", "fail", "0", "false", "no", "reject"})

# How hard a single disagreement nudges a dimension's weight (multiplicative).
# Small on purpose: calibration should bend the rubric, not snap it.  A
# down-weighted dimension is scaled by (1 - step) per disagreement it caused;
# an aligned dimension by (1 + step).  Clamped so weights stay positive.
_NUDGE_STEP = 0.25
# Floor a weight can never drop below (keeps every dimension in the mean and
# avoids a degenerate all-zero rubric).
_MIN_WEIGHT = 1e-3


def normalize_human_label(label: Any) -> Optional[bool]:
    """Coerce a human label to ``True`` (good) / ``False`` (bad) / ``None``.

    Accepts bools, ints (0/1), and common string spellings (case-insensitive).
    Anything unrecognised → ``None`` so the case is treated as unlabelled and
    skipped by the re-weighting math rather than guessed at.
    """
    if isinstance(label, bool):
        return label
    if isinstance(label, (int, float)) and not isinstance(label, bool):
        if label == 1:
            return True
        if label == 0:
            return False
        return None
    if isinstance(label, str):
        token = label.strip().lower()
        if token in _GOOD_LABELS:
            return True
        if token in _BAD_LABELS:
            return False
    return None


@dataclass
class CalibrationCase:
    """One recorded calibration case.

    ``trace_summary`` and ``judge_verdict`` are the JSON-serialisable dicts the
    judge already produces (``TraceSummary.to_dict`` and ``JudgeVerdict.to_dict``).
    ``dimension_scores`` is pulled out of the verdict for the re-weighting math.
    """

    session_id: str
    trace_summary: Dict[str, Any]
    judge_verdict: Dict[str, Any]
    human_label: Optional[bool]
    created_at: float = field(default_factory=time.time)

    @property
    def overall_score(self) -> float:
        try:
            return float(self.judge_verdict.get("overall_score", 0.0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def dimension_scores(self) -> Dict[str, float]:
        raw = self.judge_verdict.get("dimension_scores")
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, float] = {}
        for key, value in raw.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return out


# ── Persistent store (the only sqlite I/O in this module) ────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_cases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    trace_summary TEXT NOT NULL,
    judge_verdict TEXT NOT NULL,
    human_label   INTEGER,            -- 1 good, 0 bad, NULL unlabelled
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calibration_session
    ON calibration_cases (session_id);
"""


def default_db_path() -> Path:
    """Default calibration DB path: ``<hermes-home>/judge_calibration.db``.

    Resolved lazily (not at import) so ``HERMES_HOME`` / profile overrides set
    after import are honoured, mirroring ``hermes_state`` / ``kanban_db``.
    """
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "judge_calibration.db"


class CalibrationStore:
    """A tiny sqlite store of judge-vs-human calibration cases.

    Mirrors the repo's sqlite idiom (``sqlite3.Row`` factory, ``CREATE TABLE
    IF NOT EXISTS`` schema, WAL with a graceful fallback).  Connections are
    short-lived and opened per operation, so the store is safe to share across
    threads without holding a long-lived handle.
    """

    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self._initialised = False

    # -- connection plumbing -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``isolation_level=None`` (autocommit) mirrors hermes_state / kanban_db.
        # It is also required under pypy, whose non-refcounting cursor semantics
        # otherwise leave a PRAGMA's open statement in progress and break the
        # next commit ("SQL statements in progress").
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL is best for the concurrent-reader / single-writer access this
        # store sees; fall back to the rollback journal on filesystems that
        # reject WAL (NFS/SMB), matching hermes_state's documented behaviour.
        # Fully consume the PRAGMA result row so no open statement lingers.
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError:
            logger.debug("judge_calibration: WAL unsupported, using default journal")
        return conn

    def open(self) -> "CalibrationStore":
        """Create the schema if needed and return ``self`` (idempotent)."""
        if self._initialised:
            return self
        with closing(self._connect()) as conn:
            # autocommit (isolation_level=None) — executescript persists the
            # DDL without an explicit commit.
            conn.executescript(_SCHEMA)
        self._initialised = True
        return self

    def __enter__(self) -> "CalibrationStore":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        return None

    # -- writes --------------------------------------------------------------

    def record_case(
        self,
        session_id: str,
        trace_summary: Dict[str, Any],
        judge_verdict: Dict[str, Any],
        human_label: Any,
    ) -> CalibrationCase:
        """Persist one calibration case and return it.

        ``human_label`` is normalised (see :func:`normalize_human_label`) before
        storage; an unrecognised label is stored as ``NULL`` (unlabelled) rather
        than rejected, so recording never raises on a caller's odd label.
        """
        self.open()
        label = normalize_human_label(human_label)
        created_at = time.time()
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO calibration_cases "
                "(session_id, trace_summary, judge_verdict, human_label, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(session_id),
                    json.dumps(trace_summary, ensure_ascii=False),
                    json.dumps(judge_verdict, ensure_ascii=False),
                    None if label is None else int(label),
                    created_at,
                ),
            )
        return CalibrationCase(
            session_id=str(session_id),
            trace_summary=trace_summary,
            judge_verdict=judge_verdict,
            human_label=label,
            created_at=created_at,
        )

    def record_verdict(
        self,
        verdict: JudgeVerdict,
        human_label: Any,
    ) -> CalibrationCase:
        """Convenience: record a :class:`JudgeVerdict` directly.

        Pulls ``trace_summary`` and ``judge_verdict`` from the verdict's own
        ``to_dict`` so callers holding a verdict don't reassemble the payload.
        """
        verdict_dict = verdict.to_dict()
        return self.record_case(
            session_id=verdict.session_id,
            trace_summary=verdict_dict.get("trace_summary", {}),
            judge_verdict=verdict_dict,
            human_label=human_label,
        )

    # -- reads ---------------------------------------------------------------

    def cases(self, session_id: Optional[str] = None) -> List[CalibrationCase]:
        """Return stored cases (newest first), optionally filtered by session.

        Malformed rows (un-decodable JSON in ``trace_summary`` /
        ``judge_verdict``) are tolerated: the row is skipped with a debug log
        rather than crashing the read, so one bad write can't poison retrieval.
        """
        self.open()
        sql = "SELECT * FROM calibration_cases"
        params: Sequence[Any] = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params = (str(session_id),)
        sql += " ORDER BY created_at DESC, id DESC"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()

        out: List[CalibrationCase] = []
        for row in rows:
            try:
                summary = json.loads(row["trace_summary"])
                verdict = json.loads(row["judge_verdict"])
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.debug(
                    "judge_calibration: skipping malformed row id=%s",
                    row["id"] if "id" in row.keys() else "?",
                )
                continue
            if not isinstance(summary, dict) or not isinstance(verdict, dict):
                continue
            raw_label = row["human_label"]
            label = None if raw_label is None else bool(raw_label)
            out.append(
                CalibrationCase(
                    session_id=row["session_id"],
                    trace_summary=summary,
                    judge_verdict=verdict,
                    human_label=label,
                    created_at=float(row["created_at"]),
                )
            )
        return out


# ── Pure re-weighting math (no I/O — unit-tested in isolation) ────────────────


def _verdict_disagrees(case: CalibrationCase, threshold: float) -> Optional[bool]:
    """Did the judge disagree with the human on this case?

    Returns ``True`` (disagree), ``False`` (agree), or ``None`` (can't tell —
    the case is unlabelled and contributes nothing).  The judge's verdict is a
    pass iff its overall score clears ``threshold`` (the same default 0.7
    ``JudgeVerdict.passed`` uses).
    """
    if case.human_label is None:
        return None
    judge_pass = case.overall_score >= threshold
    return judge_pass != case.human_label


def derive_weight_adjustments(
    cases: Sequence[CalibrationCase],
    rubric: Rubric,
    *,
    threshold: float = 0.7,
) -> Dict[str, float]:
    """Derive a multiplicative weight factor per rubric dimension from history.

    PURE — no I/O.  Returns ``{dimension_key: factor}`` for every dimension in
    ``rubric``.  A factor of ``1.0`` means "leave this dimension's weight
    unchanged"; that is what every dimension gets when there is no disagreement
    data, so an empty / all-agreement history is a guaranteed no-op.

    The rule, per *disagreement* case (judge pass/fail differs from human):

    * **Judge said pass, human said bad** — the judge over-trusted the trace.
      Dimensions it scored *above* the case's overall score were the ones
      inflating the verdict → down-weight them.  Dimensions it scored *below*
      overall pointed the right way (toward bad) → up-weight them.
    * **Judge said fail, human said good** — the judge over-penalised.
      Symmetric: dimensions scored *below* overall dragged the verdict down
      wrongly → down-weight; dimensions scored *above* overall agreed with the
      human (good) → up-weight.

    In both cases the principle is identical: a dimension whose score pulled the
    overall *away* from the human's truth is down-weighted; one that pulled
    *toward* it is up-weighted.  Agreement cases are skipped entirely.
    """
    factors: Dict[str, float] = {d.key: 1.0 for d in rubric.dimensions}
    keys = set(factors)

    for case in cases:
        disagrees = _verdict_disagrees(case, threshold)
        if not disagrees:  # None (unlabelled) or False (agreement) → skip
            continue
        human_good = case.human_label  # True if human said good
        overall = case.overall_score
        scores = case.dimension_scores
        for key in keys:
            if key not in scores:
                continue
            dim_score = scores[key]
            # "Toward the human truth" = high score when human said good, low
            # score when human said bad.  A dimension scoring above the overall
            # leans optimistic; below leans pessimistic.
            leans_optimistic = dim_score >= overall
            aligned_with_human = leans_optimistic == bool(human_good)
            if aligned_with_human:
                factors[key] *= (1.0 + _NUDGE_STEP)
            else:
                factors[key] *= (1.0 - _NUDGE_STEP)

    return factors


def recalibrated_rubric(
    rubric: Rubric,
    cases: Sequence[CalibrationCase],
    *,
    threshold: float = 0.7,
) -> Rubric:
    """Return a NEW rubric with weights nudged by calibration history.

    PURE — builds a fresh :class:`Rubric`; the input is never mutated (its
    dimensions are frozen dataclasses anyway).  With no disagreement data the
    returned rubric's weights are **identical** to the input's, so re-scoring
    reproduces the original verdict exactly (the additivity guarantee).

    Weights are floored at ``_MIN_WEIGHT`` so no dimension drops out of the
    weighted mean and the rubric can never become all-zero (which
    ``Rubric.total_weight`` would otherwise have to special-case).
    """
    factors = derive_weight_adjustments(cases, rubric, threshold=threshold)
    new_dimensions: List[RubricDimension] = []
    for dim in rubric.dimensions:
        factor = factors.get(dim.key, 1.0)
        new_weight = max(_MIN_WEIGHT, dim.weight * factor)
        new_dimensions.append(
            RubricDimension(
                key=dim.key,
                title=dim.title,
                description=dim.description,
                weight=new_weight,
            )
        )
    return Rubric(dimensions=tuple(new_dimensions))


# ── Engine-facing helper ─────────────────────────────────────────────────────


def recalibrate_and_score(
    session_id: str,
    messages: Sequence[Dict[str, Any]],
    cases: Sequence[CalibrationCase],
    *,
    base_rubric: Optional[Rubric] = None,
    task: Optional[str] = None,
    use_llm: bool = False,
    threshold: float = 0.7,
) -> JudgeVerdict:
    """Re-score a trace with a rubric recalibrated from past disagreements.

    A thin convenience over ``AgentJudge``: builds the recalibrated rubric from
    ``cases`` and scores ``messages`` with it.  ``use_llm`` defaults to ``False``
    so calibration re-evaluation is deterministic and free by default (the
    deterministic heuristic), which is what unit tests and offline re-scoring
    want; pass ``use_llm=True`` to route through the LLM path.

    With an empty / all-agreement ``cases`` list the rubric is unchanged, so the
    returned verdict matches a plain ``AgentJudge(base_rubric).score(...)``.
    """
    if base_rubric is None:
        base_rubric = AgentJudge().rubric
    new_rubric = recalibrated_rubric(base_rubric, cases, threshold=threshold)
    return AgentJudge(new_rubric).score(
        session_id, messages, task=task, use_llm=use_llm
    )
