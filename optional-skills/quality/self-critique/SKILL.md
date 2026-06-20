---
name: self-critique
description: Audit a completed task against the user's original request before declaring done. Catches omitted constraints, misread scope, partially-met requirements, and unsupported "it's done" claims after long multi-tool loops. Opt-in quality gate — run it, report the verdict, never silently re-loop.
version: 1.0.0
author: Nous Research (proposed by @dimokru, issue #372)
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [quality, self-critique, reflection, verification, audit, review, post-task]
    requires_toolsets: [terminal]
---

# Self-Critique

Audit a finished task against what was **originally asked**, not against how
good the final answer looks. Fluent, well-formatted responses routinely omit a
requested constraint, misread scope, or stop one step short — especially after
long multi-tool loops where the final message drifts from the initial ask.

## When to use

- Right before telling the user a non-trivial task is complete.
- After a long multi-tool run, a hand-off, or a plan with several steps.
- When invoked explicitly (CLI / cron / a `/self-critique` style request).

Skip it for routine short answers — it adds noise without value there.

## Output shape

Always one JSON object:

```json
{
  "verdict": "satisfied | partial | missing | unknown",
  "missing_items": ["concise unmet requirement", "..."],
  "suggested_follow_up": "one short actionable sentence, or empty"
}
```

- `satisfied` — every explicit requirement met (`missing_items` empty).
- `partial` — some requirements met, some not.
- `missing` — the core ask is unmet.
- `unknown` — the audit could not run (no auditor available); never a guess.

## How to run

Feed the original request, the final response, and (optionally) the tool trace
to the script. It uses Hermes' shared auxiliary client for a cheap audit:

```bash
echo '{"original_request": "<the user ask>", "final_response": "<your answer>", "tool_trace": "<optional>"}' \
  | python optional-skills/quality/self-critique/scripts/self_critique.py
```

Or from a file:

```bash
python optional-skills/quality/self-critique/scripts/self_critique.py --input audit.json
```

You can also call it in-process and inject your own LLM function (used in
tests and when embedding). Put the `scripts/` dir on `sys.path` first (or run
from inside it):

```python
import sys; sys.path.insert(0, ".../optional-skills/quality/self-critique/scripts")
from self_critique import critique
result = critique(original_request, final_response, tool_trace_json)
```

## Hard rules

- **Report only.** This skill never edits conversation history and never
  re-enters the agent loop on its own. Surface the verdict; let the user
  decide whether to act.
- **Opt-in.** It is not part of the default tool schema and must not run on
  every short turn.
- **No false confidence.** If the auditor is unavailable, return `unknown` —
  do not fabricate a `satisfied`.

## What it checks

- Omitted or partially-satisfied explicit constraints.
- Scope drift (answered a narrower/different question than asked).
- Completion claims unsupported by the tool trace.
