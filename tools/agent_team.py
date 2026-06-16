#!/usr/bin/env python3
"""Agent-team coordination store (GitHub issue #252, first increment).

Hermes already spawns isolated teammate sessions via ``delegate_task``
(``tools/delegate_tool.py``): each child gets its own fresh context window,
optional per-task model, and the lead merges their outputs without re-running
the subtasks in its own context. What delegation does NOT provide is the
peer-coordination primitive #252 calls for: a *shared task list* that teammates
can claim and update during execution, and *direct teammate-to-teammate
messaging* (not just one-way handoffs back to the parent).

This module supplies exactly that residual: a ``team_id``-scoped SQLite store
holding (a) a shared task list and (b) per-teammate mailboxes. It is the
coordination backbone; the actual isolated teammate sessions are still spawned
by the existing delegation machinery, which injects ``HERMES_TEAM_ID`` /
``HERMES_TEAM_MEMBER`` into each teammate's environment so the teammate-facing
tools (``team_task``, ``team_message``) bind to the right team and identity.

Storage convention mirrors the kanban board (``tools/kanban_tools.py`` /
``hermes_cli/kanban_db.py``): a SQLite file under the Hermes home directory,
one DB per team at ``<hermes_home>/agent_teams/<team_id>.db``.

The store is deliberately small and dependency-free (stdlib ``sqlite3`` only).
WAL mode + a short busy-timeout make concurrent teammate writers safe; teammate
sessions run in separate ThreadPoolExecutor worker threads (delegation) or
separate processes (kanban dispatcher), so cross-connection concurrency is the
realistic access pattern.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

# Env vars the lead injects into each teammate session. Mirror the kanban
# dispatcher's ``HERMES_KANBAN_TASK`` convention so the gating + identity
# resolution is familiar and the two systems never collide.
TEAM_ID_ENV = "HERMES_TEAM_ID"
TEAM_MEMBER_ENV = "HERMES_TEAM_MEMBER"
TEAM_DB_ENV = "HERMES_TEAM_DB"  # defense-in-depth path pin (like HERMES_KANBAN_DB)

# Shared task lifecycle. Kept intentionally flat — this is a coordination
# list, not a workflow engine (that is what kanban is for).
TASK_STATUSES = frozenset({"open", "claimed", "done"})

# Identifiers (team id, member name) are embedded in filesystem paths, so
# constrain them to a safe slug to avoid path traversal.
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_BUSY_TIMEOUT_MS = 5000


def is_valid_slug(value: str) -> bool:
    """Return True when *value* is a safe team-id / member-name slug."""
    return bool(_SLUG_RE.match(value or ""))


def _teams_dir() -> Path:
    """Directory holding per-team SQLite files."""
    return get_hermes_home() / "agent_teams"


def team_db_path(team_id: str) -> Path:
    """Resolve the SQLite path for *team_id*.

    ``HERMES_TEAM_DB`` pins the path directly (highest precedence) so the lead
    can inject an exact path into teammate sessions and make them immune to any
    home-resolution disagreement — same defense-in-depth the kanban dispatcher
    uses with ``HERMES_KANBAN_DB``.
    """
    override = os.environ.get(TEAM_DB_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if not is_valid_slug(team_id):
        raise ValueError(f"invalid team_id slug: {team_id!r}")
    return _teams_dir() / f"{team_id}.db"


class TeamStore:
    """SQLite-backed shared task list + per-teammate mailboxes for one team.

    A single team is one DB file. Construct with a ``team_id``; call
    :meth:`ensure_schema` (idempotent) before use. Every method opens a fresh
    short-lived connection so the store is safe to share across the threads /
    processes that host the isolated teammate sessions.
    """

    def __init__(self, team_id: str, db_path: Optional[Path] = None):
        if not is_valid_slug(team_id):
            raise ValueError(f"invalid team_id slug: {team_id!r}")
        self.team_id = team_id
        self.db_path = Path(db_path) if db_path is not None else team_db_path(team_id)

    # -- connection -----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def ensure_schema(self) -> None:
        """Create tables if absent. Idempotent."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS team_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    name        TEXT PRIMARY KEY,
                    role        TEXT NOT NULL DEFAULT '',
                    model       TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'open',
                    assignee    TEXT NOT NULL DEFAULT '',
                    result      TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id          TEXT PRIMARY KEY,
                    sender      TEXT NOT NULL,
                    recipient   TEXT NOT NULL DEFAULT '',
                    body        TEXT NOT NULL,
                    created_at  REAL NOT NULL,
                    read_at     REAL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_recipient
                    ON messages(recipient, read_at);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO team_meta(key, value) VALUES('team_id', ?)",
                (self.team_id,),
            )

    # -- members --------------------------------------------------------

    def add_member(self, name: str, role: str = "", model: str = "") -> Dict[str, Any]:
        """Register (or update) a teammate. Returns the member row as a dict."""
        if not is_valid_slug(name):
            raise ValueError(f"invalid member name slug: {name!r}")
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members(name, role, model, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET role=excluded.role,
                                                model=excluded.model
                """,
                (name, role or "", model or "", now),
            )
        return {"name": name, "role": role or "", "model": model or ""}

    def list_members(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, role, model FROM members ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- shared task list ----------------------------------------------

    def add_task(self, title: str, assignee: str = "") -> Dict[str, Any]:
        """Append a task to the shared list. Returns the task row."""
        title = (title or "").strip()
        if not title:
            raise ValueError("task title is required")
        if assignee and not is_valid_slug(assignee):
            raise ValueError(f"invalid assignee slug: {assignee!r}")
        task_id = f"tt_{uuid.uuid4().hex[:12]}"
        now = time.time()
        status = "claimed" if assignee else "open"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(id, title, status, assignee, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (task_id, title, status, assignee or "", now, now),
            )
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def claim_task(self, task_id: str, member: str) -> Dict[str, Any]:
        """Atomically claim an open task for *member*.

        Returns ``{"claimed": True, "task": {...}}`` on success, or
        ``{"claimed": False, "reason": ...}`` when the task does not exist or
        was already claimed by someone else (lost the race).
        """
        if not is_valid_slug(member):
            raise ValueError(f"invalid member slug: {member!r}")
        now = time.time()
        with self._connect() as conn:
            # Single UPDATE guarded on status='open' is the atomic claim — the
            # SQLite write lock serializes concurrent claimers, so exactly one
            # wins. Idempotent re-claim by the same member is allowed.
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status='claimed', assignee=?, updated_at=?
                 WHERE id=?
                   AND (status='open' OR (status='claimed' AND assignee=?))
                """,
                (member, now, task_id, member),
            )
            changed = cur.rowcount
        task = self.get_task(task_id)
        if task is None:
            return {"claimed": False, "reason": "no such task", "task": None}
        if not changed:
            return {
                "claimed": False,
                "reason": f"already claimed by {task['assignee'] or 'someone'}",
                "task": task,
            }
        return {"claimed": True, "task": task}

    def complete_task(
        self, task_id: str, result: str = "", member: str = ""
    ) -> Dict[str, Any]:
        """Mark a task done with an optional result summary.

        ``member`` is accepted for call-site symmetry with :meth:`claim_task`
        and is not enforced — completion is cooperative within a team, and a
        teammate that finishes another's work (e.g. picked up a handoff) should
        not be blocked. Ownership is recorded by ``assignee`` at claim time.
        """
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='done', result=?, updated_at=? WHERE id=?",
                (result or "", now, task_id),
            )
        task = self.get_task(task_id)
        if task is None:
            return {"completed": False, "reason": "no such task", "task": None}
        return {"completed": True, "task": task}

    # -- direct teammate messaging -------------------------------------

    def send_message(
        self, sender: str, body: str, recipient: str = ""
    ) -> Dict[str, Any]:
        """Send a direct message to *recipient*, or broadcast when empty.

        ``recipient=""`` is a broadcast: every member (other than the sender)
        receives a copy in their inbox. This is the peer-to-peer channel #252
        asks for — teammates message each other, not only the parent.
        """
        if not is_valid_slug(sender):
            raise ValueError(f"invalid sender slug: {sender!r}")
        body = (body or "").strip()
        if not body:
            raise ValueError("message body is required")
        if recipient and not is_valid_slug(recipient):
            raise ValueError(f"invalid recipient slug: {recipient!r}")
        now = time.time()

        recipients: List[str]
        if recipient:
            recipients = [recipient]
        else:
            recipients = [
                m["name"] for m in self.list_members() if m["name"] != sender
            ]
            if not recipients:
                # No registered peers yet: keep the broadcast as an unaddressed
                # message so it is not silently dropped.
                recipients = [""]

        created: List[str] = []
        with self._connect() as conn:
            for rcpt in recipients:
                msg_id = f"tm_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO messages(id, sender, recipient, body, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (msg_id, sender, rcpt, body, now),
                )
                created.append(msg_id)
        return {
            "sent": True,
            "message_ids": created,
            "recipients": recipients,
            "broadcast": not recipient,
        }

    def inbox(
        self, member: str, unread_only: bool = True, mark_read: bool = True
    ) -> List[Dict[str, Any]]:
        """Return messages addressed to *member* (and broadcasts).

        Broadcasts are persisted as per-recipient rows by :meth:`send_message`,
        so a member's inbox is simply rows where ``recipient == member``. When
        ``mark_read`` is set, returned unread rows are stamped read so the next
        poll only surfaces new traffic.
        """
        if not is_valid_slug(member):
            raise ValueError(f"invalid member slug: {member!r}")
        query = "SELECT * FROM messages WHERE recipient = ?"
        params: tuple = (member,)
        if unread_only:
            query += " AND read_at IS NULL"
        query += " ORDER BY created_at"
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(query, params).fetchall()]
            if mark_read and rows:
                now = time.time()
                unread_ids = [r["id"] for r in rows if r["read_at"] is None]
                if unread_ids:
                    placeholders = ",".join("?" for _ in unread_ids)
                    conn.execute(
                        f"UPDATE messages SET read_at=? WHERE id IN ({placeholders})",
                        (now, *unread_ids),
                    )
        return rows

    # -- lead-side merge ------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Full team state for the lead to merge teammate outputs.

        Lets the lead collect every teammate's completed-task results and the
        message log in one read, instead of re-running subtasks in its own
        context (success criterion #3).
        """
        tasks = self.list_tasks()
        return {
            "team_id": self.team_id,
            "members": self.list_members(),
            "tasks": tasks,
            "results": {
                t["id"]: t["result"]
                for t in tasks
                if t["status"] == "done" and t["result"]
            },
            "open_count": sum(1 for t in tasks if t["status"] == "open"),
            "done_count": sum(1 for t in tasks if t["status"] == "done"),
        }


# ---------------------------------------------------------------------------
# Teammate-side identity resolution
# ---------------------------------------------------------------------------
#
# Two teammate-hosting models exist in Hermes, so identity must resolve from
# both:
#   * delegate_task spawns children as in-process ThreadPoolExecutor worker
#     THREADS that all share one ``os.environ`` — env vars cannot distinguish
#     them, so each thread stamps its identity into a ``threading.local``.
#   * the kanban dispatcher spawns workers as SUBPROCESSES with their own
#     environment — those carry identity in ``HERMES_TEAM_ID`` /
#     ``HERMES_TEAM_MEMBER`` env vars (the same convention kanban uses).
# Resolution order is therefore: explicit arg → thread-local → env.

_thread_identity = threading.local()


def set_thread_identity(team_id: Optional[str], member: Optional[str]) -> None:
    """Bind this thread's team identity (called at a teammate thread's entry).

    Used by ``delegate_task`` when it runs a teammate in a worker thread so the
    teammate-facing tools resolve the right team + member without relying on the
    shared process environment.
    """
    _thread_identity.team_id = team_id
    _thread_identity.member = member


def clear_thread_identity() -> None:
    """Drop this thread's bound team identity (called when the thread exits)."""
    _thread_identity.team_id = None
    _thread_identity.member = None


def current_team_id(arg: Optional[str] = None) -> Optional[str]:
    """Resolve the active team id: explicit arg → thread-local → env."""
    if arg:
        return arg
    tl = getattr(_thread_identity, "team_id", None)
    if tl:
        return tl
    val = os.environ.get(TEAM_ID_ENV, "").strip()
    return val or None


def current_member(arg: Optional[str] = None) -> Optional[str]:
    """Resolve the calling teammate's name: explicit arg → thread-local → env."""
    if arg:
        return arg
    tl = getattr(_thread_identity, "member", None)
    if tl:
        return tl
    val = os.environ.get(TEAM_MEMBER_ENV, "").strip()
    return val or None
