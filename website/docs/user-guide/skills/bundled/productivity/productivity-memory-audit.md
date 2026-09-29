---
title: "Memory Audit — Audit memory health: secrets, stale, broken wikilinks"
sidebar_label: "Memory Audit"
description: "Audit memory health: secrets, stale, broken wikilinks"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Memory Audit

Audit memory health: secrets, stale, broken wikilinks.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/productivity/memory-audit` |
| Version | `1.0.0` |
| Author | Hermes Evolution |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `memory`, `audit`, `health`, `reliability`, `secrets`, `obsidian`, `cron` |
| Related skills | [`obsidian`](../../bundled/note-taking/note-taking-obsidian.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Memory Audit Skill

Audit the health of Hermes memory layers for problems that accumulate
silently and degrade agent performance: secrets leaking into the
always-in-context memory store, stale entries that no longer apply,
memory approaching the char limit, and broken Obsidian wikilinks. The
audit is SILENT when everything is healthy — it only reports problems.

## When to Use

Run this skill when the user says "audit my memory", "check memory
health", or "scan for secrets in memory". It is also designed as a daily
cron job that surfaces problems without spam (empty output when healthy).

## Prerequisites

No external dependencies — the audit script uses only the Python
standard library. A Python 3.8+ interpreter is required.

For Obsidian vault checks, set the vault path via `--vault-path` or the
`OBSIDIAN_VAULT_PATH` environment variable.

## How to Run

### One-off audit

```bash
python skills/productivity/memory-audit/scripts/memory_audit.py
```

This defaults to `$HERMES_HOME/memories` (or `~/.hermes/memories`) and
checks MEMORY.md and USER.md for secrets, stale entries, and usage.

### With an Obsidian vault

```bash
python skills/productivity/memory-audit/scripts/memory_audit.py \
  --vault-path "$OBSIDIAN_VAULT_PATH"
```

### Scheduled via cron (recommended)

```bash
hermes cron add --name "memory-audit" \
  --schedule "30 6 * * *" \
  --prompt "Run the memory audit skill and report any findings." \
  --toolsets "file,terminal"
```

Or as a script-only cron job (no agent needed):

```bash
hermes cron add --name "memory-audit" \
  --schedule "30 6 * * *" \
  --script "python skills/productivity/memory-audit/scripts/memory_audit.py --vault-path \"\$OBSIDIAN_VAULT_PATH\"" \
  --no-agent
```

## Quick Reference

| Check | What it finds | Severity |
|-------|--------------|----------|
| Secrets | API keys, tokens, passwords in memory | CRITICAL |
| Stale | PR numbers, commit SHAs, completed phases | advisory |
| Usage | Memory near char limit (>85%) | warning |
| Wikilinks | `[[Note]]` → non-existent Obsidian file | advisory |
| Daily note | Today's daily note missing | advisory |

## Procedure

1. **Run the audit script** with `terminal` or `execute_code`. It reads
   MEMORY.md and USER.md from the memories directory and (if a vault
   path is given) scans the Obsidian vault.
2. **Review findings.** If the script is SILENT (exit 0, no output),
   memory is healthy — no action needed.
3. **Fix CRITICAL findings first.** Secrets in memory are in every
   system prompt. Use the `memory` tool with `action=remove` to delete
   the offending entry, then confirm it is gone.
4. **Clean up stale entries.** Use `memory(action=remove, ...) ` to
   delete entries referencing merged PRs, old commit SHAs, or completed
   phases that no longer apply.
5. **Fix broken wikilinks** by creating the missing target notes or
   correcting the link syntax in the source note with `patch`.
6. **Create today's daily note** if it is missing.

## Pitfalls

- **Don't echo secrets back.** The audit script truncates secret matches
  to 40 chars to avoid echoing full credentials. When removing a secret
  via the memory tool, reference it by a short unique substring, not
  the full value.
- **Stale detection is advisory.** A commit SHA in memory might be
  intentionally there (e.g. "the fix for X is commit abc1234"). Review
  before removing — the script flags candidates, it does not auto-delete.
- **Memory limits vary.** The default limits (2200 / 1375) mirror the
  config defaults but if the user changed them in `config.yaml`, pass
  the actual limits via `--memory-char-limit` and `--user-char-limit`.

## Verification

```bash
# Run the audit — healthy memory produces no output
python skills/productivity/memory-audit/scripts/memory_audit.py
echo "exit: $?"  # 0 = healthy, 1 = issues found

# Run the test suite
scripts/run_tests.sh tests/skills/test_memory_audit_skill.py -q
```
