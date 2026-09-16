# Tool-call log & replay-or-fork semantics (#2225 / #2236)

## Problem

Hermes supports background tasks, context compaction, and session resume — all
of which checkpoint agent state and restore it later. On restore, the agent's
**local** state rolls back, but **external side effects from tool calls may
have already executed** (an email was sent, a payment was charged, a single-use
token was consumed). Worse, the LLM re-synthesizes a "subtly different" tool
call after restore (new UUID, nonce, or trace_id), which defeats server-side
idempotency detection — so the side effect happens twice.

This was formally characterized as **semantic rollback attacks** (Action
Replay + Authority Resurrection) and demonstrated at 100% success on comparable
agent frameworks (LangGraph, CrewAI, AutoGen).

## Solution: replay-or-fork semantics

A tool-call log records every call to a **non-atomic** tool (irreversible side
effect) with its arguments split into two classes:

- **semantic-intent** fields — what the user actually wants (`recipient`,
  `amount`, `memo`). Two calls with the same semantic intent are *the same
  action* and must not be re-executed after a restore.
- **syntactic-noise** fields — per-call randomness (`trace_id`, `nonce`,
  `request_id`) that should NOT be treated as intent.

A stable **idempotency key** is derived from the semantic-intent fields alone.
On checkpoint-restore:

- **Same intent** (same idempotency key) → replay the cached result, do NOT
  re-execute the side effect.
- **Different intent** (different key) → block + surface to the caller (fork).

## Status

- **Slice A (#2236)** — MERGED. `tools/tool_call_log.py`: non-atomic tool
  registry, field classifier, idempotency-key inference, thread-safe
  append-only log. Standalone — records and classifies only.
- **Slice B (#2237)** — PENDING. Wires replay-or-fork into the MCP dispatch
  path and adds restore-scenario integration tests. Depends on Slice A.

## Usage (Slice A)

```python
from tools.tool_call_log import get_default_log, is_non_atomic

log = get_default_log()

# Record a non-atomic call (before or after execution):
if is_non_atomic("agentmail__send_message"):
    log.record("agentmail__send_message", {"to": "a@b.com", "subject": "hi"})

# Check whether this exact intent has already executed:
if log.has_executed("agentmail__send_message", {"to": "a@b.com", "subject": "hi"}):
    # Slice B will replay the cached result here instead of re-executing.
    ...
```
