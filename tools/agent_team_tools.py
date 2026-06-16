#!/usr/bin/env python3
"""Teammate-facing agent-team tools (GitHub issue #252, first increment).

Two tools, surfaced only inside a teammate session (env ``HERMES_TEAM_ID``
set by the lead — same env-gating discipline kanban uses with
``HERMES_KANBAN_TASK``):

  * ``team_task``    — read / claim / update the shared task list.
  * ``team_message`` — send a direct message to a named teammate (or broadcast)
                       and read your own inbox.

These ride on :class:`tools.agent_team.TeamStore`. The lead spawns each
teammate as an isolated delegation/kanban session with ``HERMES_TEAM_ID`` and
``HERMES_TEAM_MEMBER`` injected; the tools bind to that team + identity so a
teammate can coordinate without the lead flattening every interaction into a
single context.

Why a separate peer-messaging channel rather than reusing kanban comments:
kanban comments are worker→thread handoffs read by the *next* worker, not a
direct teammate→teammate channel during concurrent execution. ``team_message``
is the addressed, mark-read inbox #252 calls for.
"""

from __future__ import annotations

import os
from typing import Optional

from tools.agent_team import (
    TASK_STATUSES,
    TEAM_ID_ENV,
    TEAM_MEMBER_ENV,
    TeamStore,
    current_member,
    current_team_id,
)
from tools.registry import registry, tool_error, tool_result


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def check_agent_team_requirements() -> bool:
    """Team tools are available only inside a teammate session.

    A teammate session is one the lead spawned with ``HERMES_TEAM_ID`` in its
    environment. A normal ``hermes chat`` session (no team id) sees zero
    team_* tools — identical to how kanban tools stay hidden without
    ``HERMES_KANBAN_TASK``.
    """
    return bool(os.environ.get(TEAM_ID_ENV, "").strip())


def _resolve_store(team_id_arg: Optional[str]) -> tuple[Optional[TeamStore], Optional[str]]:
    """Build a TeamStore for the active team, or return a tool-error string."""
    team_id = current_team_id(team_id_arg)
    if not team_id:
        return None, tool_error(
            f"no active team — {TEAM_ID_ENV} is not set and no team_id was "
            "provided. team_* tools run inside a teammate session spawned by "
            "the lead."
        )
    try:
        store = TeamStore(team_id)
        store.ensure_schema()
    except ValueError as exc:
        return None, tool_error(str(exc))
    return store, None


def _resolve_member(member_arg: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    member = current_member(member_arg)
    if not member:
        return None, tool_error(
            f"no teammate identity — {TEAM_MEMBER_ENV} is not set and no "
            "member was provided."
        )
    return member, None


# ---------------------------------------------------------------------------
# team_task
# ---------------------------------------------------------------------------

def team_task(
    action: str,
    task_id: Optional[str] = None,
    title: Optional[str] = None,
    result: Optional[str] = None,
    status: Optional[str] = None,
    team_id: Optional[str] = None,
    member: Optional[str] = None,
) -> str:
    """Read / add / claim / complete tasks on the shared team task list."""
    action = (action or "").strip().lower()
    store, err = _resolve_store(team_id)
    if err:
        return err
    assert store is not None  # narrowed by err guard

    if action == "list":
        if status and status not in TASK_STATUSES:
            return tool_error(
                f"invalid status {status!r}; expected one of "
                f"{sorted(TASK_STATUSES)}"
            )
        return tool_result(
            {"team_id": store.team_id, "tasks": store.list_tasks(status or None)}
        )

    if action == "add":
        if not title or not title.strip():
            return tool_error("title is required to add a task")
        try:
            task = store.add_task(title)
        except ValueError as exc:
            return tool_error(str(exc))
        return tool_result({"added": True, "task": task})

    if action == "claim":
        if not task_id:
            return tool_error("task_id is required to claim a task")
        member_name, merr = _resolve_member(member)
        if merr:
            return merr
        try:
            outcome = store.claim_task(task_id, member_name)  # type: ignore[arg-type]
        except ValueError as exc:
            return tool_error(str(exc))
        return tool_result(outcome)

    if action == "complete":
        if not task_id:
            return tool_error("task_id is required to complete a task")
        member_name = current_member(member) or ""
        outcome = store.complete_task(task_id, result or "", member_name)
        return tool_result(outcome)

    return tool_error(
        f"unknown action {action!r}; expected 'list', 'add', 'claim', or "
        "'complete'"
    )


# ---------------------------------------------------------------------------
# team_message
# ---------------------------------------------------------------------------

def team_message(
    action: str,
    body: Optional[str] = None,
    to: Optional[str] = None,
    team_id: Optional[str] = None,
    member: Optional[str] = None,
) -> str:
    """Send a direct message to a teammate (or broadcast) and read your inbox."""
    action = (action or "").strip().lower()
    store, err = _resolve_store(team_id)
    if err:
        return err
    assert store is not None

    member_name, merr = _resolve_member(member)
    if merr:
        return merr

    if action == "send":
        if not body or not body.strip():
            return tool_error("body is required to send a message")
        try:
            outcome = store.send_message(member_name, body, to or "")  # type: ignore[arg-type]
        except ValueError as exc:
            return tool_error(str(exc))
        return tool_result(outcome)

    if action == "inbox":
        try:
            messages = store.inbox(member_name)  # type: ignore[arg-type]
        except ValueError as exc:
            return tool_error(str(exc))
        return tool_result({"member": member_name, "messages": messages})

    return tool_error(
        f"unknown action {action!r}; expected 'send' or 'inbox'"
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

TEAM_TASK_SCHEMA = {
    "name": "team_task",
    "description": (
        "Coordinate on your agent team's SHARED task list. You are one teammate "
        "in a lead-spawned team; this list is visible to every teammate.\n\n"
        "Actions:\n"
        "- 'list': see all shared tasks (optionally filter by status: open / "
        "claimed / done). Do this first to find unclaimed work.\n"
        "- 'add': append a new shared task (title required) for any teammate to "
        "claim — use this to hand a sub-problem to the team.\n"
        "- 'claim': atomically take an open task (task_id required). If another "
        "teammate already claimed it you'll be told — pick a different one.\n"
        "- 'complete': mark your claimed task done with a result summary "
        "(task_id required; put your findings in 'result' so the lead can "
        "merge them without re-running your work)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "claim", "complete"],
                "description": "What to do with the shared task list.",
            },
            "task_id": {
                "type": "string",
                "description": "Task id (required for 'claim' and 'complete').",
            },
            "title": {
                "type": "string",
                "description": "Task title (required for 'add').",
            },
            "result": {
                "type": "string",
                "description": "Result summary when completing a task.",
            },
            "status": {
                "type": "string",
                "enum": ["open", "claimed", "done"],
                "description": "Optional status filter for 'list'.",
            },
        },
        "required": ["action"],
    },
}

TEAM_MESSAGE_SCHEMA = {
    "name": "team_message",
    "description": (
        "Message your teammates directly. Unlike reporting back to the lead, "
        "this reaches a peer teammate's inbox during execution — use it to "
        "request a clarification, hand off a sub-problem, or share a finding.\n\n"
        "Actions:\n"
        "- 'send': deliver a message. Set 'to' to a teammate's name for a "
        "direct message, or omit 'to' to broadcast to every teammate.\n"
        "- 'inbox': read (and mark read) messages addressed to you. Poll this "
        "when you're waiting on a peer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "inbox"],
                "description": "Send a message or read your inbox.",
            },
            "body": {
                "type": "string",
                "description": "Message text (required for 'send').",
            },
            "to": {
                "type": "string",
                "description": (
                    "Recipient teammate name for a direct message. Omit to "
                    "broadcast to all teammates."
                ),
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry.register(
    name="team_task",
    toolset="agent_team",
    schema=TEAM_TASK_SCHEMA,
    handler=lambda args, **kw: team_task(
        action=args.get("action", ""),
        task_id=args.get("task_id"),
        title=args.get("title"),
        result=args.get("result"),
        status=args.get("status"),
    ),
    check_fn=check_agent_team_requirements,
    emoji="🧩",
)

registry.register(
    name="team_message",
    toolset="agent_team",
    schema=TEAM_MESSAGE_SCHEMA,
    handler=lambda args, **kw: team_message(
        action=args.get("action", ""),
        body=args.get("body"),
        to=args.get("to"),
    ),
    check_fn=check_agent_team_requirements,
    emoji="📨",
)
