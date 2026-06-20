#!/usr/bin/env python3
"""Deterministic pre-extract for evolution-introspection (#89).

evolution-introspection previously loaded RAW session transcripts (last 7 days)
into the LLM context — unbounded megabytes, the single largest context bomb in
the pipeline, AND it put the user's private text into the model context.

This script (no LLM) scans the session files for PROBLEM SIGNALS only and emits
a compact, ANONYMIZED digest — counts per signal/tool, generic shapes, never
raw content. The skill feeds ONLY this digest to the model. Raw private text
never enters the context (complements the PII redaction gate #82).

Two on-disk session formats are scanned (#238): the upstream ``*.jsonl``
transcripts AND ``request_dump_*.json`` snapshots, which some installs persist
instead. A request dump carries the same role-tagged messages at
``request.body.messages`` plus a provider ``error`` object; ignoring it left
those installs reporting ``sessions_scanned: 0`` and blinded the whole
self-improvement loop.

Signals extracted:
  * tool_failures  — tool results that look like failures, attributed to the
    tool (via tool_call_id -> name from the preceding assistant turn). Reuses
    agent.loop_guard's failure markers for consistency.
  * timeouts       — results mentioning timeout / timed out.
  * refusals       — assistant text expressing "I can't / no access / denied".
  * repeated_tool_runs — same tool called many times consecutively (the spiral
    shape loop_guard guards against), counted per session.
  * provider_errors — from request_dump error objects: ``status_code:type``
    only (never the response body/text, which can echo private content).
  * models_used    — the model id from each request dump (anonymized metadata).

Output: a JSON digest to stdout (and optionally a file), a few KB max.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _tool_result_failed(content: Any) -> bool:
    """Structural failure classifier for a tool result (issue #347).

    Every Hermes tool serialises its result as a JSON envelope that already
    carries the authoritative status (``exit_code`` for terminal/code-exec,
    ``error`` and/or ``success``/``status`` for the rest). We read that status
    instead of substring-scanning the body.

    The old substring matcher scanned the WHOLE envelope string for marker
    words ("failed", "error:", "404", "timeout") and fired on file *content*
    returned by SUCCESSFUL calls — e.g. ``read_file`` of a page mentioning
    "HTTP 404", or a ``grep`` whose stdout contains the word "error:". That
    massively over-counted failures and misattributed them to tools with zero
    genuine errors (read_file/skill_view), corrupting the introspection signal.

    A result counts as a failure ONLY when its structured status says so:
      * ``exit_code`` present (terminal/code-exec) → failure iff it is not 0;
      * a truthy ``error`` field, or ``status == "error"``;
      * an explicit ``success``/``ok`` field that is falsy.
    A result with no recognised status field — including any non-JSON / plain
    string body — is NOT counted: we no longer guess from content. This trades
    a few genuinely-plain-string failures (rare; tools emit JSON envelopes) for
    eliminating the false-positive flood, exactly as the issue prescribes.
    """
    data = content
    if isinstance(data, (str, bytes)):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        stripped = text.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return False  # not a JSON envelope → no authoritative status → don't guess
        try:
            data = json.loads(stripped)
        except ValueError:
            return False
    if not isinstance(data, dict):
        return False
    if "exit_code" in data:
        try:
            return int(data["exit_code"]) != 0
        except (TypeError, ValueError):
            return False
    if data.get("error") or str(data.get("status", "")).lower() == "error":
        return True
    for ok_key in ("success", "ok"):
        if ok_key in data:
            return not bool(data[ok_key])
    return False


_TIMEOUT_RE = re.compile(r"\b(timed out|timeout)\b", re.IGNORECASE)
_REFUSAL_RE = re.compile(
    r"\b(i can('|no)?t|cannot|no access|access denied|not permitted|don'?t have (access|permission))\b",
    re.IGNORECASE,
)
_REPEAT_THRESHOLD = 5  # same tool >=N consecutive in a session is a "repeated run"


def _iter_lines(path: Path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def scan_messages(messages) -> Dict[str, Any]:
    """Return per-session signal counts (no raw text) from an iterable of
    role-tagged message dicts. Shared by the JSONL transcript path and the
    request_dump_*.json path (#238) so both formats yield the identical digest.
    """
    tool_failures: Counter = Counter()
    timeouts = 0
    refusals = 0
    id_to_tool: Dict[str, str] = {}
    consec_tool = None
    consec_n = 0
    max_runs: Counter = Counter()  # tool -> max consecutive in this session

    for obj in messages:
        if not isinstance(obj, dict):
            continue
        role = obj.get("role")
        if role == "assistant":
            tcs = obj.get("tool_calls") or []
            names = []
            for tc in tcs:
                if isinstance(tc, dict) and tc.get("function"):
                    nm = tc["function"].get("name")
                    if nm:
                        names.append(nm)
                        if tc.get("id"):
                            id_to_tool[tc["id"]] = nm
            # consecutive same-single-tool run tracking
            if len(set(names)) == 1:
                tool = names[0]
                if tool == consec_tool:
                    consec_n += 1
                else:
                    consec_tool, consec_n = tool, 1
                max_runs[consec_tool] = max(max_runs[consec_tool], consec_n)
            else:
                consec_tool, consec_n = None, 0
            content = obj.get("content")
            if isinstance(content, str) and _REFUSAL_RE.search(content):
                refusals += 1
        elif role == "tool":
            content = obj.get("content")
            tool = id_to_tool.get(obj.get("tool_call_id"), "unknown")
            if _tool_result_failed(content):
                tool_failures[tool] += 1
            if isinstance(content, str) and _TIMEOUT_RE.search(content):
                timeouts += 1

    repeated = {t: n for t, n in max_runs.items() if n >= _REPEAT_THRESHOLD}
    return {
        "tool_failures": dict(tool_failures),
        "timeouts": timeouts,
        "refusals": refusals,
        "repeated_tool_runs": repeated,
    }


def scan_session(path: Path) -> Dict[str, Any]:
    """Per-session signals from a JSONL transcript (one JSON object per line)."""
    return scan_messages(_iter_lines(path))


def _sessions_from_db(db_path: Path, cutoff: float) -> List[List[Dict[str, Any]]]:
    """Return sessions from SessionDB SQLite as message-dict lists (#399).

    The ``state.db`` ``messages`` table carries conversation history for
    installations that persist via SQLite instead of on-disk ``*.jsonl`` files.
    Each session's messages are grouped by ``session_id`` and ordered by
    ``timestamp``, then cast to the same dict shape ``scan_messages()`` expects
    — ``role``, ``content``, ``tool_calls`` (parsed from JSON text for
    assistant turns), ``tool_call_id`` (for tool results).

    Returns an empty list when the db is missing, unreadable, or has no
    ``messages`` table (the existing file-based paths still fire).
    """
    try:
        import sqlite3  # noqa: PLC0415 — stdlib, acceptable import delay
    except ImportError:
        return []

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
    except (sqlite3.OperationalError, OSError):
        return []

    try:
        c.execute(
            "SELECT DISTINCT session_id FROM messages WHERE timestamp >= ? ORDER BY session_id",
            (cutoff,),
        )
        session_ids = [row["session_id"] for row in c.fetchall()]
    except sqlite3.OperationalError:
        conn.close()
        return []

    sessions: List[List[Dict[str, Any]]] = []
    for sid in session_ids:
        c.execute(
            "SELECT role, content, tool_call_id, tool_calls, tool_name "
            "FROM messages WHERE session_id = ? AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (sid, cutoff),
        )
        msgs: List[Dict[str, Any]] = []
        for row in c.fetchall():
            msg: Dict[str, Any] = {"role": row["role"]}
            content = row["content"]
            if content is not None:
                msg["content"] = content
            if row["role"] == "assistant":
                tc_raw = row["tool_calls"]
                if tc_raw:
                    try:
                        parsed = json.loads(tc_raw)
                        if isinstance(parsed, list):
                            msg["tool_calls"] = parsed
                    except (ValueError, TypeError):
                        pass
            elif row["role"] == "tool":
                tci = row["tool_call_id"]
                if tci:
                    msg["tool_call_id"] = tci
            msgs.append(msg)
        sessions.append(msgs)

    conn.close()
    return sessions


def _request_dump_messages(obj: Dict[str, Any]) -> List[Any]:
    """The conversation messages carried inside a request_dump_*.json snapshot
    live at request.body.messages — the same role-tagged shape as a JSONL
    transcript, so scan_messages handles it directly."""
    req = obj.get("request") if isinstance(obj, dict) else None
    body = req.get("body") if isinstance(req, dict) else None
    msgs = body.get("messages") if isinstance(body, dict) else None
    return msgs if isinstance(msgs, list) else []


def scan_request_dump(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Per-session signals from a request_dump_*.json snapshot (#238).

    Reuses scan_messages over request.body.messages, and adds the provider-layer
    error signal from the top-level ``error`` object — but ONLY its status code
    and error type, never ``message``/``body``/``response_text`` (those can echo
    private content; the no-raw-text contract still holds). Also records the
    model id used, which is anonymized metadata, not user content."""
    s = scan_messages(_request_dump_messages(obj))
    provider_errors: Counter = Counter()
    err = obj.get("error")
    if isinstance(err, dict):
        status = err.get("status_code") or err.get("response_status")
        # Prefer the structured recovery class (#236) over the raw exception type:
        # `rate_limit`/`auth`/`model_not_found` groups recurring bad provider-model
        # pairs far better than `RuntimeError`/`BadRequestError` (#237 pt3). Falls
        # back to the exception type for dumps written before failure_category.
        label = err.get("failure_category") or err.get("type") or "error"
        provider_errors[f"{status}:{label}" if status else str(label)] += 1
    s["provider_errors"] = dict(provider_errors)
    body = obj.get("request", {}).get("body") if isinstance(obj.get("request"), dict) else None
    model = body.get("model") if isinstance(body, dict) else None
    s["models"] = {model: 1} if isinstance(model, str) and model else {}
    return s


def _fresh(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime >= cutoff
    except OSError:
        return False


def build_digest(
    sessions_dir: Path,
    window_days: int = 7,
    now: float | None = None,
    db_path: Path | None = None,
) -> Dict[str, Any]:
    """Build a compact, ANONYMIZED signal digest from session data.

    Scans three data sources in order:
      1. ``*.jsonl`` transcripts (the upstream format);
      2. ``request_dump_*.json`` snapshots (#238);
      3. SessionDB SQLite ``state.db`` → ``messages`` table (#399).

    Each source reuses the same ``scan_messages()`` path so all produce the
    identical digest shape.
    """
    now = now if now is not None else time.time()
    cutoff = now - window_days * 86400
    failures: Counter = Counter()
    timeouts = 0
    refusals = 0
    provider_errors: Counter = Counter()
    models: Counter = Counter()
    repeated: Dict[str, Dict[str, int]] = {}  # tool -> {max_consecutive, sessions}
    scanned = 0

    def _aggregate(s: Dict[str, Any]) -> None:
        nonlocal timeouts, refusals
        failures.update(s.get("tool_failures", {}))
        timeouts += s.get("timeouts", 0)
        refusals += s.get("refusals", 0)
        provider_errors.update(s.get("provider_errors", {}))
        models.update(s.get("models", {}))
        for tool, n in s.get("repeated_tool_runs", {}).items():
            r = repeated.setdefault(tool, {"max_consecutive": 0, "sessions": 0})
            r["max_consecutive"] = max(r["max_consecutive"], n)
            r["sessions"] += 1

    if sessions_dir.is_dir():
        # 1. Native JSONL transcripts (the upstream session format).
        for path in sorted(sessions_dir.glob("*.jsonl")):
            if not _fresh(path, cutoff):
                continue
            scanned += 1
            _aggregate(scan_session(path))

        # 2. request_dump_*.json snapshots (#238 — this install persists sessions
        #    this way, so the JSONL glob found zero and the whole pipeline went
        #    blind). Multiple dumps of one session each carry a growing prefix of
        #    the same conversation, so dedup by session_id keeping the most
        #    complete snapshot — one session contributes its signals once.
        dumps: Dict[str, tuple] = {}  # session_id -> (msg_count, obj)
        for path in sorted(sessions_dir.glob("request_dump_*.json")):
            if not _fresh(path, cutoff):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    obj = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            sid = obj.get("session_id") or path.stem
            n_msgs = len(_request_dump_messages(obj))
            if n_msgs >= dumps.get(sid, (-1, None))[0]:
                dumps[sid] = (n_msgs, obj)
        for _sid, (_n, obj) in dumps.items():
            scanned += 1
            _aggregate(scan_request_dump(obj))

    # 3. SessionDB SQLite (#399 — reads state.db messages table when it exists,
    #    covering installations where sessions live in the DB rather than in
    #    on-disk files. Grouped by session_id, dedup'd within the query, and
    #    fed through scan_messages() just like the file-based paths.)
    if db_path is not None and db_path.is_file():
        for msgs in _sessions_from_db(db_path, cutoff):
            scanned += 1
            _aggregate(scan_messages(msgs))

    return {
        "window_days": window_days,
        "sessions_scanned": scanned,
        "signals": {
            "tool_failures": dict(failures.most_common()),
            "timeouts": timeouts,
            "refusals_or_access_denied": refusals,
            "repeated_tool_runs": repeated,
            "provider_errors": dict(provider_errors.most_common()),
            "models_used": dict(models.most_common()),
        },
    }


def _sessions_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "sessions"


def main(argv: List[str]) -> int:
    days = 7
    for a in argv[1:]:
        if a.startswith("--days="):
            try:
                days = int(a.split("=", 1)[1])
            except ValueError:
                pass
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    sessions_dir = hermes_home / "sessions"
    state_db = hermes_home / "state.db"
    digest = build_digest(sessions_dir, window_days=days, db_path=state_db)
    print(json.dumps(digest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
