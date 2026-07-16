# -*- coding: utf-8 -*-
"""Feasibility checking for plan execution (issue #1031, child of #1021).

PIVOT-style plan-feasibility validation run *before* execution.  Each
:class:`FeasibilityCheck` examines a plan step's context dict and returns a
:class:`FeasibilityResult` whose :attr:`FeasibilityResult.status` is one of
``FEASIBLE`` / ``INFEASIBLE`` / ``UNCERTAIN``.  A :class:`FeasibilityGate`
batch-runs the checks against one or many steps and reports the blockers.

Design goals (matching the rest of the ``evolution/lib`` corpus):

* Pure Python, ``dataclasses`` + ``Enum`` — **no external dependencies**.
* Injectable seams for every IO-bound predicate (``exists_func``,
  ``can_write_func``, ``available_tools``) so tests never touch the disk.
* Import-safe (no side effects on import), full type hints,
  ``from __future__ import annotations``.
* JSON serialisation via ``to_dict``/``from_dict`` on every dataclass.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "FeasibilityStatus",
    "FeasibilityResult",
    "FeasibilityCheck",
    "FileExistenceCheck",
    "ToolAvailabilityCheck",
    "WritePathCheck",
    "DirectoryExistenceCheck",
    "RegexValidCheck",
    "NonEmptyStringCheck",
    "FeasibilityGate",
]

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FeasibilityStatus(str, Enum):
    """Outcome of a single feasibility check.

    Members
    -------
    FEASIBLE
        The precondition holds — the step may proceed.
    INFEASIBLE
        The precondition is definitively violated — the step *must not*
        proceed (a blocker).
    UNCERTAIN
        The check could not reach a definitive answer.  Not a blocker on
        its own, but signals the caller may want additional validation.
    """

    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    UNCERTAIN = "uncertain"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FeasibilityResult:
    """Outcome of running one check against one context.

    Attributes
    ----------
    status
        The :class:`FeasibilityStatus` verdict.
    check_name
        Human-readable name of the check that produced this result.
    reason
        Short explanation of the verdict (empty string when feasible).
    suggestion
        Optional remediation hint for the caller.
    metadata
        Free-form dict for extra diagnostic data (e.g. the missing paths).
    """

    status: FeasibilityStatus
    check_name: str
    reason: str = ""
    suggestion: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "status": self.status.value,
            "check_name": self.check_name,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeasibilityResult":
        """Reconstruct from a :meth:`to_dict` payload."""
        status_val = data["status"]
        if isinstance(status_val, str):
            status_val = FeasibilityStatus(status_val)
        return cls(
            status=status_val,
            check_name=data["check_name"],
            reason=data.get("reason", ""),
            suggestion=data.get("suggestion", ""),
            metadata=dict(data.get("metadata", {})),
        )

    # -- convenience ------------------------------------------------------

    @property
    def is_blocker(self) -> bool:
        """True when this result blocks execution (status == INFEASIBLE)."""
        return self.status is FeasibilityStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class FeasibilityCheck(ABC):
    """Abstract base for all feasibility checks.

    Subclasses implement :meth:`check`, which inspects a *context* dict
    (typically a plan step) and returns a :class:`FeasibilityResult`.
    """

    #: Human-readable name surfaced in results.  Overridden by subclasses.
    name: str = "FeasibilityCheck"

    @abstractmethod
    def check(self, context: dict[str, Any]) -> FeasibilityResult:  # pragma: no cover - interface
        raise NotImplementedError

    # Allow instances to be called like functions.
    def __call__(self, context: dict[str, Any]) -> FeasibilityResult:
        return self.check(context)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------


def _default_isfile(path: str) -> bool:
    """Default file-existence predicate (``os.path.isfile``).

    Unlike :func:`os.path.exists`, this rejects a directory presented where a
    regular file is required, so the check is honest to its name.
    """
    return os.path.isfile(path)


def _default_isdir(path: str) -> bool:
    """Default directory-existence predicate (``os.path.isdir``).

    Rejects a regular file presented where a directory is required, preventing
    a downstream ``NotADirectoryError`` from a falsely-``FEASIBLE`` verdict.
    """
    return os.path.isdir(path)


def _default_can_write(path: str) -> bool:
    """Default write-access predicate (``os.access(path, os.W_OK)``).

    For a not-yet-existing path, the parent directory is tested instead.
    """
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    parent = os.path.dirname(path) or "."
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


# ---------------------------------------------------------------------------
# Concrete checks
# ---------------------------------------------------------------------------


class FileExistenceCheck(FeasibilityCheck):
    """Verify that the file paths referenced by the step actually exist.

    Parameters
    ----------
    context_key
        Key into the step dict whose value is the list of file paths.
    exists_func
        Injectable ``Callable[[str], bool]``.  Defaults to
        :func:`os.path.exists`.
    name
        Override the reported check name.
    """

    name = "FileExistenceCheck"

    def __init__(
        self,
        context_key: str = "required_files",
        exists_func: Optional[Callable[[str], bool]] = None,
        name: Optional[str] = None,
    ) -> None:
        self.context_key = context_key
        self.exists_func = exists_func or _default_isfile
        if name:
            self.name = name

    def check(self, context: dict[str, Any]) -> FeasibilityResult:
        paths = context.get(self.context_key)
        if not paths:
            return FeasibilityResult(
                status=FeasibilityStatus.FEASIBLE,
                check_name=self.name,
                reason="",
                metadata={"checked": []},
            )
        if isinstance(paths, (str, bytes)):
            paths = [paths]
        missing = [p for p in paths if not self.exists_func(p)]
        if missing:
            return FeasibilityResult(
                status=FeasibilityStatus.INFEASIBLE,
                check_name=self.name,
                reason=f"{len(missing)} required file(s) missing: {missing}",
                suggestion="Ensure the files exist before executing the step.",
                metadata={"missing": missing, "checked": list(paths)},
            )
        return FeasibilityResult(
            status=FeasibilityStatus.FEASIBLE,
            check_name=self.name,
            metadata={"checked": list(paths)},
        )


class DirectoryExistenceCheck(FeasibilityCheck):
    """Verify that the directories referenced by the step exist.

    Identical to :class:`FileExistenceCheck` but targeted at directories
    (and with a different default ``context_key``).
    """

    name = "DirectoryExistenceCheck"

    def __init__(
        self,
        context_key: str = "required_dirs",
        exists_func: Optional[Callable[[str], bool]] = None,
        name: Optional[str] = None,
    ) -> None:
        self.context_key = context_key
        self.exists_func = exists_func or _default_isdir
        if name:
            self.name = name

    def check(self, context: dict[str, Any]) -> FeasibilityResult:
        paths = context.get(self.context_key)
        if not paths:
            return FeasibilityResult(
                status=FeasibilityStatus.FEASIBLE,
                check_name=self.name,
                metadata={"checked": []},
            )
        if isinstance(paths, (str, bytes)):
            paths = [paths]
        missing = [p for p in paths if not self.exists_func(p)]
        if missing:
            return FeasibilityResult(
                status=FeasibilityStatus.INFEASIBLE,
                check_name=self.name,
                reason=f"{len(missing)} required director(y/ies) missing: {missing}",
                suggestion="Create the directories before executing the step.",
                metadata={"missing": missing, "checked": list(paths)},
            )
        return FeasibilityResult(
            status=FeasibilityStatus.FEASIBLE,
            check_name=self.name,
            metadata={"checked": list(paths)},
        )


class ToolAvailabilityCheck(FeasibilityCheck):
    """Verify that the tools required by the step are available.

    Parameters
    ----------
    available_tools
        Injectable set/collection of tool names known to be available.
    context_key
        Key into the step dict whose value is the list of required tool names.
    """

    name = "ToolAvailabilityCheck"

    def __init__(
        self,
        available_tools: Optional[Iterable[str]] = None,
        context_key: str = "required_tools",
        name: Optional[str] = None,
    ) -> None:
        self.available_tools: set[str] = set(available_tools or ())
        self.context_key = context_key
        if name:
            self.name = name

    def check(self, context: dict[str, Any]) -> FeasibilityResult:
        required = context.get(self.context_key)
        if not required:
            return FeasibilityResult(
                status=FeasibilityStatus.FEASIBLE,
                check_name=self.name,
                metadata={"checked": [], "available": sorted(self.available_tools)},
            )
        if isinstance(required, (str, bytes)):
            required = [required]
        missing = [t for t in required if t not in self.available_tools]
        if missing:
            return FeasibilityResult(
                status=FeasibilityStatus.INFEASIBLE,
                check_name=self.name,
                reason=f"{len(missing)} required tool(s) unavailable: {missing}",
                suggestion="Install or enable the missing tools before executing.",
                metadata={
                    "missing": missing,
                    "available": sorted(self.available_tools),
                    "checked": list(required),
                },
            )
        return FeasibilityResult(
            status=FeasibilityStatus.FEASIBLE,
            check_name=self.name,
            metadata={
                "checked": list(required),
                "available": sorted(self.available_tools),
            },
        )


class WritePathCheck(FeasibilityCheck):
    """Verify that the write-target paths of the step are writable.

    Parameters
    ----------
    context_key
        Key into the step dict whose value is the list of write paths.
    can_write_func
        Injectable ``Callable[[str], bool]`` predicate.
    """

    name = "WritePathCheck"

    def __init__(
        self,
        context_key: str = "write_paths",
        can_write_func: Optional[Callable[[str], bool]] = None,
        name: Optional[str] = None,
    ) -> None:
        self.context_key = context_key
        self.can_write_func = can_write_func or _default_can_write
        if name:
            self.name = name

    def check(self, context: dict[str, Any]) -> FeasibilityResult:
        paths = context.get(self.context_key)
        if not paths:
            return FeasibilityResult(
                status=FeasibilityStatus.FEASIBLE,
                check_name=self.name,
                metadata={"checked": []},
            )
        if isinstance(paths, (str, bytes)):
            paths = [paths]
        unwritable = [p for p in paths if not self.can_write_func(p)]
        if unwritable:
            return FeasibilityResult(
                status=FeasibilityStatus.INFEASIBLE,
                check_name=self.name,
                reason=f"{len(unwritable)} write path(s) not writable: {unwritable}",
                suggestion="Adjust permissions or choose a writable location.",
                metadata={"unwritable": unwritable, "checked": list(paths)},
            )
        return FeasibilityResult(
            status=FeasibilityStatus.FEASIBLE,
            check_name=self.name,
            metadata={"checked": list(paths)},
        )


class RegexValidCheck(FeasibilityCheck):
    """Verify that the regex pattern(s) referenced by the step compile.

    Parameters
    ----------
    context_key
        Key into the step dict whose value is the regex pattern (str) or
        list of patterns.
    """

    name = "RegexValidCheck"

    def __init__(
        self,
        context_key: str = "regex_pattern",
        name: Optional[str] = None,
    ) -> None:
        self.context_key = context_key
        if name:
            self.name = name

    def check(self, context: dict[str, Any]) -> FeasibilityResult:
        patterns = context.get(self.context_key)
        if not patterns:
            return FeasibilityResult(
                status=FeasibilityStatus.FEASIBLE,
                check_name=self.name,
                metadata={"checked": []},
            )
        if isinstance(patterns, (str, bytes)):
            patterns = [patterns]
        # Coerce any other non-iterable scalar into a single-element list.
        if not isinstance(patterns, (list, tuple, set)):
            patterns = [patterns]
        invalid: list[tuple[str, str]] = []
        for pat in patterns:
            try:
                re.compile(pat)
            except (re.error, TypeError, ValueError) as exc:
                invalid.append((str(pat), str(exc)))
        if invalid:
            return FeasibilityResult(
                status=FeasibilityStatus.INFEASIBLE,
                check_name=self.name,
                reason=f"{len(invalid)} invalid regex pattern(s): {invalid}",
                suggestion="Fix the regex syntax before executing.",
                metadata={"invalid": invalid, "checked": list(patterns)},
            )
        return FeasibilityResult(
            status=FeasibilityStatus.FEASIBLE,
            check_name=self.name,
            metadata={"checked": list(patterns)},
        )


class NonEmptyStringCheck(FeasibilityCheck):
    """Verify that the required string fields of the step are non-empty.

    Parameters
    ----------
    required_keys
        Iterable of keys whose values must be present and non-empty strings.
    allow_whitespace
        If ``False`` (default) strings that are only whitespace are rejected.
    """

    name = "NonEmptyStringCheck"

    def __init__(
        self,
        required_keys: Optional[Iterable[str]] = None,
        allow_whitespace: bool = False,
        name: Optional[str] = None,
    ) -> None:
        self.required_keys: list[str] = list(required_keys or [])
        self.allow_whitespace = allow_whitespace
        if name:
            self.name = name

    def check(self, context: dict[str, Any]) -> FeasibilityResult:
        if not self.required_keys:
            return FeasibilityResult(
                status=FeasibilityStatus.FEASIBLE,
                check_name=self.name,
                metadata={"checked_keys": []},
            )
        missing: list[str] = []
        for key in self.required_keys:
            val = context.get(key)
            if val is None:
                missing.append(key)
                continue
            if not isinstance(val, str):
                missing.append(key)
                continue
            if self.allow_whitespace:
                if val == "":
                    missing.append(key)
            else:
                if val.strip() == "":
                    missing.append(key)
        if missing:
            return FeasibilityResult(
                status=FeasibilityStatus.INFEASIBLE,
                check_name=self.name,
                reason=f"{len(missing)} required field(s) empty/missing: {missing}",
                suggestion="Provide non-empty values for the required fields.",
                metadata={"missing": missing, "checked_keys": list(self.required_keys)},
            )
        return FeasibilityResult(
            status=FeasibilityStatus.FEASIBLE,
            check_name=self.name,
            metadata={"checked_keys": list(self.required_keys)},
        )


# ---------------------------------------------------------------------------
# FeasibilityGate
# ---------------------------------------------------------------------------


class FeasibilityGate:
    """Batch-run a collection of :class:`FeasibilityCheck` instances.

    Parameters
    ----------
    checks
        Ordered list of checks to run for each step.
    """

    def __init__(self, checks: Optional[list[FeasibilityCheck]] = None) -> None:
        self.checks: list[FeasibilityCheck] = list(checks or [])

    # -- configuration ----------------------------------------------------

    def add_check(self, check: FeasibilityCheck) -> None:
        """Append a check to the gate."""
        self.checks.append(check)

    def __len__(self) -> int:
        return len(self.checks)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<FeasibilityGate checks={len(self.checks)}>"

    # -- evaluation -------------------------------------------------------

    def evaluate_step(self, step: dict[str, Any]) -> list[FeasibilityResult]:
        """Run every check against a single step.

        Defensive against a ``None`` step (treated as empty dict).
        """
        if step is None:
            step = {}
        if not isinstance(step, dict):
            step = {}
        results: list[FeasibilityResult] = []
        for chk in self.checks:
            try:
                results.append(chk.check(step))
            except Exception as exc:  # noqa: BLE001 - surface as UNCERTAIN
                results.append(
                    FeasibilityResult(
                        status=FeasibilityStatus.UNCERTAIN,
                        check_name=getattr(chk, "name", type(chk).__name__),
                        reason=f"Check raised {type(exc).__name__}: {exc}",
                        suggestion="Investigate the check implementation/context.",
                    )
                )
        return results

    def evaluate(self, plan_steps: list[dict[str, Any]]) -> list[FeasibilityResult]:
        """Run all checks against all steps.

        Returns a flat list of results in order (step-major, then check order).
        An empty/``None`` step list yields an empty result list.
        """
        if not plan_steps:
            return []
        out: list[FeasibilityResult] = []
        for step in plan_steps:
            out.extend(self.evaluate_step(step))
        return out

    # -- filtering / helpers ---------------------------------------------

    @staticmethod
    def get_blocking_results(
        results: Iterable[FeasibilityResult],
    ) -> list[FeasibilityResult]:
        """Return only the results whose status is ``INFEASIBLE``."""
        return [r for r in results if r.status is FeasibilityStatus.INFEASIBLE]

    @staticmethod
    def has_blockers(results: Iterable[FeasibilityResult]) -> bool:
        """True if any result in *results* is a blocker (``INFEASIBLE``)."""
        return any(r.status is FeasibilityStatus.INFEASIBLE for r in results)

    @staticmethod
    def summarize(results: Iterable[FeasibilityResult]) -> str:
        """Return a human-readable one-paragraph summary of the results."""
        results = list(results)
        if not results:
            return "No feasibility checks were run."
        feasible = sum(1 for r in results if r.status is FeasibilityStatus.FEASIBLE)
        infeasible = sum(1 for r in results if r.status is FeasibilityStatus.INFEASIBLE)
        uncertain = sum(1 for r in results if r.status is FeasibilityStatus.UNCERTAIN)
        lines: list[str] = [
            f"Feasibility summary: {len(results)} check(s) — "
            f"{feasible} feasible, {infeasible} infeasible, {uncertain} uncertain."
        ]
        blockers = [r for r in results if r.status is FeasibilityStatus.INFEASIBLE]
        if blockers:
            lines.append("Blockers:")
            for b in blockers:
                lines.append(f"  - [{b.check_name}] {b.reason}")
        else:
            lines.append("No blockers detected.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    gate = FeasibilityGate(
        [
            NonEmptyStringCheck(required_keys=["action"]),
            ToolAvailabilityCheck(available_tools={"read", "write"}),
        ]
    )
    steps: list[dict[str, Any]] = [
        {"action": "read", "required_tools": ["read"]},
        {"action": "", "required_tools": ["nonexistent"]},
    ]
    results = gate.evaluate(steps)
    print(FeasibilityGate.summarize(results))
