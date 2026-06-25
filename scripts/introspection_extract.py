#!/usr/bin/env python3
"""Deterministic pre-extract for evolution-introspection (#89).

evolution-introspection previously loaded RAW session transcripts (last 7 days)
into the LLM context — unbounded megabytes, the single largest context bomb in
the pipeline, AND it put the user's private text into the model context.

This script (no LLM) scans the session files for PROBLEM SIGNALS only and emits
a compact, ANONYMIZED digest — counts per signal/tool, generic shapes, never
raw content. The skill feeds ONLY this digest to the model. Raw private text
never enters the context (complements the PII redaction gate #82).

Three on-disk session formats are scanned: the upstream ``*.jsonl``
transcripts, ``request_dump_*.json`` snapshots (#238), and the SQLite
SessionDB ``state.db`` messages table (#399). A request dump carries the same
role-tagged messages at ``request.body.messages`` plus a provider ``error``
object; ignoring it left those installs reporting ``sessions_scanned: 0`` and
blinded the whole self-improvement loop. The SessionDB is where >90% of real
sessions live, so the messages table is read, grouped by session_id and ordered
by id, then passed through the same scan_messages path.

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
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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


# Keep the id on DB-derived message dicts so _state_db_session_signals can re-sort.
_MESSAGE_ROW_ID_KEY = "_db_id"


def _message_row_to_dict(row: sqlite3.Row) -> Optional[Dict[str, Any]]:
    """Convert a SessionDB messages row into the role-tagged dict scan_messages
    consumes (#399).  Drops DB-only columns (session_id, timestamp) but keeps
    the original id for ordering."""
    obj: Dict[str, Any] = {_MESSAGE_ROW_ID_KEY: row["id"]}
    if "role" in row.keys():
        obj["role"] = row["role"]
    if "content" in row.keys():
        obj["content"] = row["content"]
    if "tool_call_id" in row.keys():
        obj["tool_call_id"] = row["tool_call_id"]
    if "tool_calls" in row.keys() and row["tool_calls"] is not None:
        try:
            parsed = json.loads(row["tool_calls"])
            if isinstance(parsed, list):
                obj["tool_calls"] = parsed
        except ValueError:
            pass
    if "tool_name" in row.keys():
        obj["tool_name"] = row["tool_name"]
    return obj if obj.get("role") else None


def _iter_state_db(
    db_path: Path, *, min_timestamp: Optional[float] = None
) -> Iterable[tuple[str, List[Dict[str, Any]]]]:
    """Yield (session_id, messages) from a SQLite state.db messages table.

    Messages are grouped by session_id and ordered by id (insertion order) so
    tool_call_id -> tool name resolution works exactly as it does for JSONL.
    Malformed rows / missing columns are skipped without crashing the scan.

    If ``min_timestamp`` is given, only sessions that contain at least one
    message with ``timestamp >= min_timestamp`` are yielded. This keeps the
    ``window_days`` bound for DB-derived sessions, mirroring the file-source
    freshness gate (#543).
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Probe schema; the expected columns are id, session_id, role, content,
        # tool_call_id, tool_calls, tool_name, timestamp.  Any subset is fine.
        try:
            cur.execute(
                "SELECT session_id, id, role, content, tool_call_id, tool_calls, "
                "tool_name, timestamp FROM messages ORDER BY session_id, id"
            )
        except sqlite3.Error:
            # timestamp column may be missing in very old schemas; retry without it
            try:
                cur.execute(
                    "SELECT session_id, id, role, content, tool_call_id, tool_calls, "
                    "tool_name FROM messages ORDER BY session_id, id"
                )
            except sqlite3.Error:
                return

        # Freshness pre-filter: only sessions with any message >= cutoff.
        fresh_sessions: Optional[set[str]] = None
        if min_timestamp is not None:
            try:
                cur2 = conn.cursor()
                cur2.execute(
                    "SELECT DISTINCT session_id FROM messages WHERE timestamp >= ?",
                    (min_timestamp,),
                )
                fresh_sessions = {row["session_id"] for row in cur2}
            except sqlite3.Error:
                # timestamp column absent or other schema issue — fall through and
                # scan everything rather than silently dropping data.
                fresh_sessions = None

        current_session: Optional[str] = None
        current_messages: List[Dict[str, Any]] = []
        for row in cur:
            sid = row["session_id"]
            if fresh_sessions is not None and sid not in fresh_sessions:
                continue
            msg = _message_row_to_dict(row)
            if msg is None:
                continue
            if sid != current_session:
                if current_session is not None:
                    yield current_session, current_messages
                current_session = sid
                current_messages = []
            current_messages.append(msg)
        if current_session is not None:
            yield current_session, current_messages
    finally:
        conn.close()


def _state_db_session_signals(msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return signals from one SessionDB session, ordered by the original id.

    The caller gives us messages already grouped by session_id and ordered by
    id, but we also carry the original id on each dict so we can re-sort here
    as a defense-in-depth step.  The id key is stripped before scanning so it
    never leaks into the digest."""
    ordered = sorted(msgs, key=lambda m: m.get(_MESSAGE_ROW_ID_KEY, 0))
    for m in ordered:
        m.pop(_MESSAGE_ROW_ID_KEY, None)
    return scan_messages(ordered)


def scan_messages(messages) -> Dict[str, Any]:
    """Return per-session signal counts (no raw text) from an iterable of
    role-tagged message dicts. Shared by the JSONL transcript path, the
    request_dump_*.json path (#238), and the SessionDB state.db path (#399) so
    all formats yield the identical digest.
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
            failed = _tool_result_failed(content)
            if failed:
                tool_failures[tool] += 1
            if failed and isinstance(content, str) and _TIMEOUT_RE.search(content):
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
    body = (
        obj.get("request", {}).get("body")
        if isinstance(obj.get("request"), dict)
        else None
    )
    model = body.get("model") if isinstance(body, dict) else None
    s["models"] = {model: 1} if isinstance(model, str) and model else {}
    return s


def _fresh(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime >= cutoff
    except OSError:
        return False


def build_digest(
    sessions_dir: Path, window_days: int = 7, now: float | None = None
) -> Dict[str, Any]:
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

        # 3. SQLite SessionDB messages table (#399) — canonical store for real
        #    sessions.  Resolve the correct profile-aware path (#543); the old
        #    hard-coded ``sessions_dir / "state.db"`` only existed in legacy
        #    installs and missed modern Hermes real sessions.
        db_path = _resolve_state_db_path(sessions_dir)
        if db_path:
            for _sid, msgs in _iter_state_db(db_path, min_timestamp=cutoff):
                if not msgs:
                    continue
                scanned += 1
                _aggregate(_state_db_session_signals(msgs))

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


def _resolve_state_db_path(sessions_dir: Path) -> Optional[Path]:
    """Find the canonical SessionDB ``state.db`` for the active Hermes profile.

    Legacy installs kept ``state.db`` inside ``~/.hermes/sessions/``. The
    canonical location used by the rest of the agent (SessionDB, gateway,
    session_search_tool, tui_gateway, ACP) is ``~/.hermes/state.db`` for the
    default profile, or ``~/.hermes/profiles/<name>/state.db`` for a named
    profile (#543). The evolution-introspection extractor was still scanning
    ``sessions/state.db`` and therefore missed almost all real sessions.

    Resolution order:
      1. Sibling of ``sessions_dir`` — the canonical modern path.  This is
         the same directory that contains the ``sessions/`` folder, so if
         ``sessions_dir`` is ``~/.hermes/sessions`` the sibling is
         ``~/.hermes/state.db``.
      2. Inside ``sessions_dir`` itself — legacy fallback, kept so older
         installs keep working.
      3. The HERMES_HOME root or current profile dir if we can determine it.

    Returns ``None`` when no candidate exists, letting build_digest fall back to
    JSONL/request_dump sources only.
    """
    candidates: List[Path] = []

    # 1. Canonical sibling: sessions_dir/../state.db
    if sessions_dir.parent.exists():
        candidates.append(sessions_dir.parent / "state.db")

    # 2. Legacy in-sessions path.
    candidates.append(sessions_dir / "state.db")

    # 3. Profile-aware resolution when HERMES_HOME points at a named profile.
    #    sessions_dir is normally <HERMES_HOME>/sessions, so HERMES_HOME itself
    #    is the profile root and its state.db is the right one.
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        candidates.append(Path(hermes_home) / "state.db")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _sessions_dir() -> Path:
    return (
        Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "sessions"
    )


def main(argv: List[str]) -> int:
    days = 7
    for a in argv[1:]:
        if a.startswith("--days="):
            try:
                days = int(a.split("=", 1)[1])
            except ValueError:
                pass
    digest = build_digest(_sessions_dir(), window_days=days)
    print(json.dumps(digest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
