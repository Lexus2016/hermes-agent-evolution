# -*- coding: utf-8 -*-
"""Tool verification gate — verify external tools before ingestion (#2577).

Misevolution guardrail (parent #2538; arXiv:2509.26354 "Your Agent May
Misevolve" — tool pathway: >76% of tool-evolving agents produced
vulnerable tools, ~93% failed to reject malicious external tools).

Before external tool code is ingested into the tool registry — and again
before a previously-ingested tool is reused — this gate verifies it:

1. **Syntax gate** — ``ast.parse``; unparseable code is rejected outright.
2. **Static analysis** — reuses ``scan_code_for_safety_violations`` (#2575);
   violations reject *before* any sandbox execution of the code.
3. **Sandbox validation** — reuses ``SandboxValidator`` (#2259) against
   the spec's ``test_inputs`` in an isolated subprocess.
4. **Judge-LLM slot on reuse** — injectable ``judge``; veto-only.

Fail-closed: any stage error, violation, or missing record is rejected.
Ingestion writes a content-hash versioned audit record (mirroring the
``SkillReuseGate.version`` pattern). Pure, DI-testable, import-safe.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from evolution.lib.safety_review_gate import scan_code_for_safety_violations
from evolution.lib.tool_synthesis import SandboxValidator, SynthesizedTool, ToolRegistry

logger = logging.getLogger(__name__)

__all__ = ["VerificationResult", "verify_external_tool", "ingest_tool", "revalidate_on_reuse"]

VERDICT_ACCEPTED = "accepted"
VERDICT_REJECTED = "rejected"

# Judge slot: receives the tool and the sandbox's binary verdict; returns the
# final reuse decision. May veto a sandbox pass, never overturn a failure.
Judge = Callable[[SynthesizedTool, bool], bool]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _default_store_dir() -> Path:
    """Default audit-record store — profile-aware, never hardcoded."""
    try:
        from hermes_constants import get_hermes_home

        base = get_hermes_home()
    except Exception:
        base = Path.home() / ".hermes"
    return base / "verified_tools"


def _normalize_test_inputs(spec: Dict[str, Any]) -> List[str]:
    raw = spec.get("test_inputs", "test")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw] or ["test"]
    return ["test"]


@dataclass
class VerificationResult:
    """Outcome of the tool-verification gate for a single tool."""

    tool_name: str
    verdict: str
    reasons: List[str] = field(default_factory=list)
    version: str = ""
    verified_at: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict == VERDICT_ACCEPTED

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VerificationResult":
        return cls(tool_name=str(d.get("tool_name", "")), verdict=str(d.get("verdict", VERDICT_REJECTED)),
                   reasons=list(d.get("reasons", []) or []), version=str(d.get("version", "")),
                   verified_at=str(d.get("verified_at", "")))


def _result(name: str, verdict: str, reason: str, code: str) -> VerificationResult:
    return VerificationResult(tool_name=name, verdict=verdict, reasons=[reason] if reason else [],
                              version=_content_hash(code), verified_at=_now_iso())


def verify_external_tool(code: str, spec: Optional[Dict[str, Any]] = None) -> VerificationResult:
    """Verify external tool *code* against *spec* — three stages, fail-closed.

    Stage 1 ``ast.parse``; stage 2 the #2575 safety scanner (rejects
    dangerous code *before* sandbox execution); stage 3 the #2259 sandbox
    validator against every ``spec["test_inputs"]`` entry.
    """
    spec = spec or {}
    name = str(spec.get("name", "external_tool"))

    # Stage 1 — syntax gate.
    try:
        ast.parse(code or "")
    except SyntaxError as exc:
        return _result(name, VERDICT_REJECTED, f"syntax error: {exc.msg} (line {exc.lineno})", code)

    # Stage 2 — static analysis (reuses the #2575 safety-review gate).
    try:
        violations = scan_code_for_safety_violations({f"{name}.py": code})
    except Exception as exc:  # fail-closed: scanner failure blocks ingestion
        return _result(name, VERDICT_REJECTED, f"safety scan failed: {exc}", code)
    if violations:
        return _result(name, VERDICT_REJECTED, f"safety violations: {'; '.join(violations)}", code)

    # Stage 3 — sandbox validation (reuses the #2259 harness).
    tool = SynthesizedTool(name=name, description=str(spec.get("description", "")), code=code)
    try:
        for test_input in _normalize_test_inputs(spec):
            if not SandboxValidator.validate(tool, test_input):
                return _result(name, VERDICT_REJECTED, f"sandbox validation failed for input {test_input!r}", code)
    except Exception as exc:  # fail-closed: sandbox crash blocks ingestion
        return _result(name, VERDICT_REJECTED, f"sandbox error: {exc}", code)

    return _result(name, VERDICT_ACCEPTED, "", code)


def ingest_tool(registry: ToolRegistry, name: str, code: str,
               spec: Optional[Dict[str, Any]] = None,
               store_dir: Optional[Path | str] = None) -> VerificationResult:
    """Verify an external tool, then record it; register only when accepted.

    Audit record mirrors the ``SkillReuseGate.version`` pattern: keyed by
    a content hash, storing code + spec + verdict + timestamp under
    *store_dir* (caller-injected; defaults to the profile-aware
    ``verified_tools`` dir). Rejected: recorded, never registered.
    """
    spec = dict(spec or {})
    spec["name"] = name
    result = verify_external_tool(code, spec)

    base = Path(store_dir) if store_dir is not None else _default_store_dir()
    base.mkdir(parents=True, exist_ok=True)
    record = {"tool_name": name, "version": result.version, "created_at": _now_iso(),
              "code": code, "spec": spec, "verification": result.to_dict()}
    dest = base / f"{name}@{result.version}.json"
    if not dest.exists():
        dest.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    if result.accepted:
        registry.store(SynthesizedTool(name=name, description=str(spec.get("description", "")),
                                       code=code, accepted=True))
    else:
        logger.warning("tool %s rejected on ingestion: %s", name, result.reasons)
    return result


def revalidate_on_reuse(registry: ToolRegistry, name: str,
                        test_inputs: Optional[Sequence[str]] = None,
                        judge: Optional[Judge] = None) -> VerificationResult:
    """Re-run the sandbox stage before a previously-ingested tool is reused.

    Missing record ⇒ reject; the sandbox validator runs against the
    **stored** code, then the injectable *judge* may veto. Fail-closed.
    """
    tool = registry.get(name)
    if tool is None:
        return _result(name, VERDICT_REJECTED, "missing registry record; reuse rejected", "")

    inputs = [str(x) for x in (test_inputs or ["test"])]
    try:
        sandbox_ok = all(SandboxValidator.validate(tool, i) for i in inputs)
    except Exception as exc:  # fail-closed: sandbox crash rejects reuse
        return _result(name, VERDICT_REJECTED, f"sandbox error during re-validation: {exc}", tool.code)

    reuse_ok = sandbox_ok
    if judge is not None:
        try:
            reuse_ok = sandbox_ok and bool(judge(tool, sandbox_ok))
        except Exception as exc:  # fail-closed: judge crash rejects reuse
            return _result(name, VERDICT_REJECTED, f"judge error during re-validation: {exc}", tool.code)

    if not reuse_ok:
        reason = "judge vetoed reuse" if sandbox_ok else "sandbox re-validation failed"
        return _result(name, VERDICT_REJECTED, reason, tool.code)
    return _result(name, VERDICT_ACCEPTED, "", tool.code)
