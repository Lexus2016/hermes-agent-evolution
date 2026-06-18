"""Gather-Act-Verify — the verify-hook registry for mutating tool calls.

Issue #293 (child of #282). After a mutating tool call *succeeds*, the agent
may consult a registry of verifiers that re-check the claimed outcome: a write
that returned ``bytes_written`` actually landed on disk, a ``terminal`` command
that exited 0 actually produced the artifact it promised, and so on. Mutating
calls can succeed silently while producing the wrong outcome — this registry is
the seam where a verifier confirms (or contradicts) that.

Scope (deliberately minimal — see the constraints on #293):

* This module is the **registry + policy** only. It defines what a verifier is,
  how mutating tools register one, and a :func:`VerifyPolicy.consult` entrypoint
  that returns a structured :class:`VerifyOutcome`.
* The *default* verifiers (file-exists + content for writes, exit-code + output
  for terminal) and **retry-on-mismatch** are the sibling issue #294. Nothing
  here retries, blocks, or rewrites a call.
* Consultation is **advisory**. ``consult`` is a pure read: it runs the
  registered verifier and reports ``ok`` / ``mismatch`` / ``error`` /
  ``skipped``. It never raises out of a verifier, never mutates the call, and
  never touches the conversation. The caller decides what (if anything) to do
  with the outcome — by default, log it.

Design mirrors :mod:`agent.tool_guardrails` / :mod:`agent.policy_interceptors`
intentionally: frozen dataclasses, pure verifiers, a config-shaped registry.
This is verify-**after-success** and **opt-in** — distinct from the existing
turn-end file-mutation *failure* footer (``turn_finalizer.py`` ~L189), which
surfaces writes that FAILED. The two never collide: that footer reads failures,
this registry reads successes.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

# Canonical names of the mutating tools that MUST be verifiable. Mirrors the
# tools called out in #293. Kept local (not imported from tool_guardrails'
# broader MUTATING_TOOL_NAMES) because the verify contract is about tools whose
# success is checkable against an external artifact — writes, patches, and
# shell mutations — not every state-touching tool (todo/memory/etc.).
MUTATING_TOOL_NAMES: frozenset[str] = frozenset(
    {"write_file", "patch", "terminal", "write_to_file"}
)


@dataclass(frozen=True)
class VerifyCall:
    """Immutable view of the mutating call being verified.

    ``tool_name`` and ``args`` describe what the model asked for; ``result`` is
    the (successful) tool result string the dispatch path observed. A verifier
    inspects these to decide whether the claimed mutation really happened.
    """

    tool_name: str
    args: Mapping[str, Any]
    result: Any = None


@dataclass(frozen=True)
class VerifyOutcome:
    """Structured result of consulting the registry for one mutating call.

    ``status`` is one of:

    * ``ok``       — a verifier ran and confirmed the mutation.
    * ``mismatch`` — a verifier ran and the mutation did NOT hold (the silent
      failure #282 is about). Advisory: the caller logs it; #294 will retry.
    * ``error``    — the verifier itself raised / could not run. Never fatal.
    * ``skipped``  — no verifier registered for this tool, or the feature is off.

    ``verifier`` names which verifier produced the outcome (empty when skipped).
    ``detail`` is a short human-readable explanation for logs.
    """

    status: str  # ok | mismatch | error | skipped
    tool_name: str
    verifier: str = ""
    detail: str = ""

    @property
    def confirmed(self) -> bool:
        """True only when a verifier ran and the mutation held."""
        return self.status == "ok"

    @property
    def ran(self) -> bool:
        """True when a verifier actually executed (ok, mismatch, or error)."""
        return self.status in {"ok", "mismatch", "error"}

    @classmethod
    def ok(cls, tool_name: str, verifier: str, detail: str = "") -> "VerifyOutcome":
        return cls(status="ok", tool_name=tool_name, verifier=verifier, detail=detail)

    @classmethod
    def mismatch(cls, tool_name: str, verifier: str, detail: str = "") -> "VerifyOutcome":
        return cls(status="mismatch", tool_name=tool_name, verifier=verifier, detail=detail)

    @classmethod
    def error(cls, tool_name: str, verifier: str, detail: str = "") -> "VerifyOutcome":
        return cls(status="error", tool_name=tool_name, verifier=verifier, detail=detail)

    @classmethod
    def skipped(cls, tool_name: str, detail: str = "") -> "VerifyOutcome":
        return cls(status="skipped", tool_name=tool_name, detail=detail)


# A verifier is a pure predicate: given the call, return True when the mutation
# is confirmed and False when it is not. Raising is tolerated by ``consult`` and
# surfaced as a ``VerifyOutcome.error`` — a verifier never crashes a turn.
Verifier = Callable[[VerifyCall], bool]


@dataclass(frozen=True)
class RegisteredVerifier:
    """A verifier bound to a human-readable name for log attribution."""

    name: str
    verifier: Verifier


# ── Verifier factories ───────────────────────────────────────────────────────
# Three kinds, matching #293: command, file-existence, symbolic predicate.


def make_file_exists_verifier(
    path_resolver: Callable[[VerifyCall], list[str]] | None = None,
) -> Verifier:
    """File-existence verifier: confirm the call's target path(s) now exist.

    ``path_resolver`` maps a call to the list of paths that should exist after
    it. Defaults to reading ``args["path"]`` (the common write_file/patch shape)
    plus ``args["file"]`` for ``write_to_file``. A mutation is confirmed only
    when *every* resolved path exists on disk; an empty resolution is treated as
    "nothing to check" → confirmed (the call mutated something unaddressable
    here, e.g. an in-place terminal command — not this verifier's job).
    """

    def _default_resolver(call: VerifyCall) -> list[str]:
        paths: list[str] = []
        for key in ("path", "file"):
            val = call.args.get(key)
            if isinstance(val, str) and val:
                paths.append(val)
        return paths

    resolve = path_resolver or _default_resolver

    def _verify(call: VerifyCall) -> bool:
        paths = resolve(call)
        if not paths:
            return True
        return all(os.path.exists(p) for p in paths)

    return _verify


def make_command_verifier(
    command: str | list[str],
    *,
    cwd_resolver: Callable[[VerifyCall], str | None] | None = None,
    timeout: float = 30.0,
) -> Verifier:
    """Command verifier: confirm by running a shell command, exit 0 == verified.

    ``command`` is a string (run via the shell) or an argv list (run directly,
    no shell). The mutation is confirmed iff the command exits 0. ``timeout``
    bounds the run; a timeout or spawn failure raises, which ``consult`` reports
    as a verify *error* (not a mismatch) — we don't claim the mutation failed
    just because the checker couldn't run.

    Note: the command is supplied by the *registrant* (a skill / config), never
    by the model — this is not a path for the LLM to run arbitrary shell.
    """
    use_shell = isinstance(command, str)

    def _verify(call: VerifyCall) -> bool:
        cwd = cwd_resolver(call) if cwd_resolver else None
        completed = subprocess.run(
            command if use_shell else list(command),
            shell=use_shell,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode == 0

    return _verify


def make_predicate_verifier(predicate: Verifier) -> Verifier:
    """Symbolic-predicate verifier: wrap an arbitrary pure ``VerifyCall`` check.

    The escape hatch for skills that verify against in-memory or domain state
    (a parsed AST, a config object, a service health flag) rather than the
    filesystem or a subprocess. Thin by design — it exists so callers express
    intent (``make_predicate_verifier(...)``) symmetrically with the other two
    factories rather than passing a bare lambda to ``register``.
    """
    return predicate


# Convenience parser for the command-verifier string form, so a config layer
# can register ``"pytest -q"`` without importing shlex itself.
def split_command(command: str) -> list[str]:
    """Split a shell-style command string into an argv list."""
    return shlex.split(command)


class VerifyPolicy:
    """Registry mapping mutating tool names to their verifiers.

    The contract #293 asks for: mutating tools MUST be *registerable* with a
    verifier, the run agent can *look one up*, and *consult* it after a mutating
    call. Consultation is advisory and never raises.

    A tool may register more than one verifier (e.g. file-exists AND a content
    command); :func:`consult` runs them in registration order and returns the
    first non-confirming outcome (``mismatch`` or ``error``), else ``ok``. This
    makes the common single-verifier case a straight pass/fail while still
    letting a registrant layer cheap and expensive checks.
    """

    def __init__(self) -> None:
        self._verifiers: dict[str, list[RegisteredVerifier]] = {}

    # ── registration / lookup ────────────────────────────────────────────
    def register(self, tool_name: str, verifier: Verifier, *, name: str = "") -> None:
        """Register ``verifier`` for ``tool_name``.

        ``tool_name`` SHOULD be one of :data:`MUTATING_TOOL_NAMES` — that is the
        set #293 says must be verifiable — but registration is not restricted to
        it, so a skill can attach a verifier to a custom mutating tool. ``name``
        is used for log attribution; it defaults to the callable's ``__name__``.
        """
        if not tool_name:
            raise ValueError("tool_name must be a non-empty string")
        if not callable(verifier):
            raise TypeError("verifier must be callable")
        label = name or getattr(verifier, "__name__", "") or "verifier"
        self._verifiers.setdefault(tool_name, []).append(
            RegisteredVerifier(name=label, verifier=verifier)
        )

    def unregister(self, tool_name: str) -> None:
        """Drop all verifiers for ``tool_name`` (no-op if none registered)."""
        self._verifiers.pop(tool_name, None)

    def lookup(self, tool_name: str) -> tuple[RegisteredVerifier, ...]:
        """Return the verifiers registered for ``tool_name`` (possibly empty)."""
        return tuple(self._verifiers.get(tool_name, ()))

    def has_verifier(self, tool_name: str) -> bool:
        """True when at least one verifier is registered for ``tool_name``."""
        return bool(self._verifiers.get(tool_name))

    @property
    def registered_tools(self) -> frozenset[str]:
        """The set of tool names with at least one verifier."""
        return frozenset(self._verifiers)

    def missing_verifier_tools(
        self, mutating_tools: frozenset[str] = MUTATING_TOOL_NAMES
    ) -> frozenset[str]:
        """Mutating tools that have no verifier yet — the #293 policy gap.

        Reporting helper: the policy is that every mutating tool MUST register a
        verifier. This names the ones that haven't, so a config/bootstrap layer
        (or a test) can assert coverage without enforcing it at dispatch time.
        """
        return frozenset(t for t in mutating_tools if not self.has_verifier(t))

    # ── consultation ─────────────────────────────────────────────────────
    def consult(self, tool_name: str, args: Mapping[str, Any], result: Any = None) -> VerifyOutcome:
        """Run the registered verifier(s) for ``tool_name`` against the call.

        Returns a :class:`VerifyOutcome`. Pure and advisory:

        * no verifier registered → ``skipped``;
        * all verifiers confirm   → ``ok``;
        * a verifier returns False → ``mismatch`` (first one wins);
        * a verifier raises        → ``error`` (first one wins), caught here so a
          buggy verifier can never crash the turn.

        This never executes the tool, mutates ``args``, or touches the
        conversation — it only reports.
        """
        verifiers = self._verifiers.get(tool_name)
        if not verifiers:
            return VerifyOutcome.skipped(tool_name, "no verifier registered")
        call = VerifyCall(tool_name=tool_name, args=dict(args), result=result)
        for rv in verifiers:
            try:
                confirmed = rv.verifier(call)
            except Exception as exc:  # a verifier must never break the turn
                return VerifyOutcome.error(
                    tool_name, rv.name, f"{type(exc).__name__}: {exc}"
                )
            if not confirmed:
                return VerifyOutcome.mismatch(
                    tool_name, rv.name, "verifier did not confirm the mutation"
                )
        return VerifyOutcome.ok(
            tool_name,
            verifiers[-1].name,
            f"{len(verifiers)} verifier(s) confirmed",
        )


# ── default verifiers for the built-in mutating tools (#294) ─────────────────
# #294 ships the *defaults* the registry (#293) was built to hold: a
# file-existence check for writes/patches and an exit-code (+ optional grep)
# check for terminal. Each reuses the #293 factories so the contract — pure,
# never-raises, advisory — is unchanged. ``register_default_verifiers`` is the
# single entry point a bootstrap/config layer calls; nothing here runs unless
# the agent has opted in via ``verify_policy_enabled()`` (default OFF).

# Header lines of a V4A patch name the file(s) it touches; the patch arg shape
# carries no plain ``path`` for these. Mirrors the extraction in
# ``patch_tool`` (tools/file_tools.py) so the default patch verifier checks the
# same files the patch claimed to write.
_V4A_FILE_HEADER = re.compile(
    r"^\*\*\*\s+(?:Update|Add)\s+File:\s*(.+)$", re.MULTILINE
)


def _patch_path_resolver(call: VerifyCall) -> list[str]:
    """Resolve the file(s) a ``patch`` call should have produced.

    Two shapes, matching ``patch_tool``:

    * ``mode="replace"`` (default) → the explicit ``path`` arg.
    * ``mode="patch"`` (V4A) → the ``Update``/``Add File:`` headers inside the
      ``patch`` content. ``Delete File:`` headers are intentionally skipped —
      a successful delete means the path should be *absent*, which a
      file-exists verifier would wrongly flag as a mismatch.

    An unresolvable shape returns ``[]`` → "nothing to check" → confirmed, so a
    patch form this resolver doesn't model never produces a false mismatch.
    """
    paths: list[str] = []
    explicit = call.args.get("path")
    if isinstance(explicit, str) and explicit:
        paths.append(explicit)
    patch_body = call.args.get("patch")
    if isinstance(patch_body, str) and patch_body:
        for match in _V4A_FILE_HEADER.finditer(patch_body):
            header_path = match.group(1).strip()
            if header_path:
                paths.append(header_path)
    return paths


def make_terminal_verifier(
    *,
    expect_in_output: str | Sequence[str] | None = None,
) -> Verifier:
    """Default ``terminal`` verifier: confirm by the call's own JSON result.

    The ``terminal`` tool returns a JSON string with an ``exit_code`` (int) and
    an ``output`` (str). Unlike the file/command verifiers, this one does NOT
    spawn a subprocess — it re-reads what the call already reported, so it adds
    no latency and cannot itself mutate state. A call is confirmed when:

    * ``exit_code == 0`` (the command the model ran actually succeeded), AND
    * every string in ``expect_in_output`` (if any) appears in ``output`` — the
      "grep" half of the #294 contract, letting a registrant assert the command
      produced the artifact text it promised, not merely exited 0.

    A result that isn't parseable JSON, or carries no ``exit_code``, resolves to
    "nothing to check" → confirmed: the default verifier must not turn a result
    it can't read into a false mismatch (that's an ``error``-shaped situation,
    and this predicate stays advisory). ``expect_in_output`` accepts a single
    string or a sequence; all must be present.
    """
    if expect_in_output is None:
        needles: tuple[str, ...] = ()
    elif isinstance(expect_in_output, str):
        needles = (expect_in_output,)
    else:
        needles = tuple(expect_in_output)

    def _verify(call: VerifyCall) -> bool:
        payload = call.result
        if not isinstance(payload, str) or not payload.strip():
            return True  # nothing to read → not this verifier's place to deny
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            return True
        if not isinstance(parsed, dict) or "exit_code" not in parsed:
            return True
        if parsed.get("exit_code") != 0:
            return False
        if needles:
            output = parsed.get("output")
            text = output if isinstance(output, str) else ""
            return all(needle in text for needle in needles)
        return True

    return _verify


def register_default_verifiers(policy: VerifyPolicy) -> VerifyPolicy:
    """Register #294's default verifiers for the built-in mutating tools.

    Idempotency: tools that already have a verifier are left untouched, so a
    skill that registered a custom check before bootstrap wins and a second
    bootstrap call doesn't stack duplicate defaults. Returns ``policy`` for
    chaining. After this runs, ``policy.missing_verifier_tools()`` is empty.

    Defaults, one per #294's mapping:

    * ``write_file`` / ``write_to_file`` → file-existence (the write landed).
    * ``patch`` → file-existence over the resolved target path(s) — explicit
      ``path`` for replace mode, V4A ``Update``/``Add File:`` headers otherwise.
    * ``terminal`` → exit-code (== 0) read from the call's own JSON result, the
      grep half available via ``make_terminal_verifier(expect_in_output=...)``.
    """
    if not policy.has_verifier("write_file"):
        policy.register(
            "write_file", make_file_exists_verifier(), name="default-file-exists"
        )
    if not policy.has_verifier("write_to_file"):
        policy.register(
            "write_to_file", make_file_exists_verifier(), name="default-file-exists"
        )
    if not policy.has_verifier("patch"):
        policy.register(
            "patch",
            make_file_exists_verifier(path_resolver=_patch_path_resolver),
            name="default-patch-exists",
        )
    if not policy.has_verifier("terminal"):
        policy.register(
            "terminal", make_terminal_verifier(), name="default-exit-code"
        )
    return policy


# ── retry-on-mismatch, capped at 1 (#294) ────────────────────────────────────
# When a default (or custom) verifier reports a ``mismatch`` and the feature is
# ON, #294 surfaces the verifier output back to the model and re-runs the
# mutating call ONCE. A second mismatch aborts to the user — we never loop more
# than once. This is the only *behavioral* half of Gather-Act-Verify; it stays
# entirely behind ``verify_policy_enabled()`` so a disabled agent is
# byte-identical to before. The policy is expressed as a pure decision over a
# consult outcome + an attempt counter, so it is testable without a live agent.

# How many times a single mutating call may be re-executed after a mismatch.
MAX_VERIFY_RETRIES = 1


@dataclass(frozen=True)
class RetryDecision:
    """Pure decision for what to do after consulting a mutating call.

    * ``retry``    — re-execute the call once, then re-consult (only on the
      first mismatch).
    * ``abort``    — a mismatch persisted past the retry cap; surface to the
      user and stop treating the step as complete.
    * ``feedback`` — the verifier output to append to the tool result the model
      sees (empty when there is nothing to surface, i.e. ok/skipped).
    """

    retry: bool
    abort: bool
    feedback: str = ""

    @property
    def acted(self) -> bool:
        """True when the decision changes turn behaviour (retry or abort)."""
        return self.retry or self.abort


def _verifier_feedback(outcome: VerifyOutcome, *, retrying: bool) -> str:
    """Human-readable block surfaced to the model for a non-confirming consult."""
    head = (
        f"[verify] {outcome.tool_name}: {outcome.verifier} reported "
        f"{outcome.status} — {outcome.detail}"
    )
    if retrying:
        return head + " Re-running the call once to reconcile the outcome."
    return (
        head
        + " The mutation could not be confirmed after one retry; aborting to the"
        " user instead of reporting success."
    )


def decide_retry(outcome: VerifyOutcome, attempts: int) -> RetryDecision:
    """Map a consult ``outcome`` + prior ``attempts`` to a :class:`RetryDecision`.

    ``attempts`` is the number of times the call has ALREADY been re-executed
    by the retry path (0 on the first consult). The contract:

    * ``ok`` / ``skipped`` / ``error`` → no action. A verifier that couldn't run
      (``error``) is advisory only; we do not retry on it, matching the #293
      rule that a broken checker never claims the mutation failed.
    * first ``mismatch`` (``attempts < MAX_VERIFY_RETRIES``) → ``retry`` with
      feedback announcing the re-run.
    * mismatch past the cap (``attempts >= MAX_VERIFY_RETRIES``) → ``abort`` with
      feedback telling the model the call is being surfaced to the user.

    This is pure: it never runs anything, so the retry cap is enforced in one
    auditable place and ``MAX_VERIFY_RETRIES == 1`` guarantees at most one re-run.
    """
    if outcome.status != "mismatch":
        return RetryDecision(retry=False, abort=False)
    if attempts < MAX_VERIFY_RETRIES:
        return RetryDecision(
            retry=True, abort=False, feedback=_verifier_feedback(outcome, retrying=True)
        )
    return RetryDecision(
        retry=False, abort=True, feedback=_verifier_feedback(outcome, retrying=False)
    )


# ── advisory consult gate ────────────────────────────────────────────────────
# The wiring point in the dispatch path is opt-in: the agent behaves identically
# unless this is explicitly turned on. Default OFF so #293 ships without
# altering any turn. #294 (default verifiers + retry) flips the behavioral side.

_VERIFY_POLICY_ENV = "HERMES_VERIFY_POLICY"


def verify_policy_enabled() -> bool:
    """Whether the advisory verify-after-mutation consult is enabled.

    Default **OFF**. Enabled by setting ``HERMES_VERIFY_POLICY`` to a truthy
    value (``1``/``true``/``yes``/``on``), or via the ``verify_policy.enabled``
    key in ``config.yaml``. Reads the env var first so a session can flip it
    without editing config. Any failure resolving config → OFF (safe default).
    """
    env = os.environ.get(_VERIFY_POLICY_ENV)
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.config import load_config as _load_config

        cfg = _load_config() or {}
    except Exception:
        return False
    section = cfg.get("verify_policy") if isinstance(cfg, dict) else None
    if isinstance(section, dict) and "enabled" in section:
        return bool(section.get("enabled"))
    return False


@dataclass
class _AgentVerifyState:
    """Per-agent lazily-built registry + the per-turn outcome ledger.

    Held off the agent as a single attribute so the dispatch path touches one
    optional seam. ``outcomes`` accumulates this turn's advisory results purely
    for logging / introspection; it is reset each turn alongside the existing
    ``_turn_failed_file_mutations`` state.

    ``retry_attempts`` (#294) tracks how many times a given mutating call has
    already been surfaced back to the model after a mismatch, keyed by a stable
    signature of the call. It enforces the retry cap (:data:`MAX_VERIFY_RETRIES`)
    across the turn so a call can be surfaced for re-run at most once before the
    seam aborts to the user. ``defaults_registered`` guards one-time bootstrap
    of the default verifiers. Both persist across turns (the cap is per call,
    not per turn) while ``outcomes`` resets.
    """

    registry: VerifyPolicy = field(default_factory=VerifyPolicy)
    outcomes: list[VerifyOutcome] = field(default_factory=list)
    retry_attempts: dict[str, int] = field(default_factory=dict)
    defaults_registered: bool = False


def call_signature(tool_name: str, args: Mapping[str, Any]) -> str:
    """Stable per-call key for the retry counter.

    Same tool + same args == same logical call, so a re-issued identical
    mutating call maps to the same counter and is correctly capped. Args are
    serialized deterministically; unserializable values fall back to ``repr``.
    """
    try:
        arg_repr = json.dumps(args, sort_keys=True, default=repr, ensure_ascii=False)
    except (TypeError, ValueError):
        arg_repr = repr(sorted(args.items(), key=lambda kv: kv[0]))
    return f"{tool_name}::{arg_repr}"


__all__ = [
    "MUTATING_TOOL_NAMES",
    "VerifyCall",
    "VerifyOutcome",
    "Verifier",
    "RegisteredVerifier",
    "VerifyPolicy",
    "make_file_exists_verifier",
    "make_command_verifier",
    "make_predicate_verifier",
    "make_terminal_verifier",
    "register_default_verifiers",
    "split_command",
    "verify_policy_enabled",
    "MAX_VERIFY_RETRIES",
    "RetryDecision",
    "decide_retry",
    "call_signature",
]
