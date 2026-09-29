---
title: "Skill Audit — Standing security review of installed Hermes skills"
sidebar_label: "Skill Audit"
description: "Standing security review of installed Hermes skills"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Skill Audit

Standing security review of installed Hermes skills.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/security/skill-audit` |
| Version | `1.0.0` |
| Author | Hermes Evolution |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `security`, `audit`, `skills`, `supply-chain`, `skilltrojan` |
| Related skills | [`ai-safe-audit`](../../bundled/security/security-ai-safe-audit.md), [`evolution-implementation`](../../bundled/evolution/evolution-evolution-implementation.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Skill Audit — standing skill-store security review

Implements the **model → scan → triage → patch** chain (Anthropic
defending-code-reference-harness shape) pointed at the agent's own supply
chain: installed skills under `~/.hermes/skills/` (or `$HERMES_HOME/skills`).

The store is only ever audited reactively today. This skill makes the review
**standing**: a nightly, Alfred-shaped scan that is silent on success and
reports only when something needs a human/agent eye. It closes the gap where a
poisoned or maliciously-updated skill can sit undiscovered between
incident-driven audits (see #120 SkillTrojan guard, #91 MCP audit).

> **Boundary:** this skill detects and proposes. It NEVER auto-applies fixes.
> The patch stage always hands a diff to the owner for review. Auto-applying
> security fixes to the skill store is how an attacker turns a detector into
> a weapon.

## The chain

### 1. MODEL — decide when to run

- **Nightly scheduled scan** (recommended, Alfred-shaped):
  ```bash
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
    python3 "$HERMES_HOME/skills/security/skill-audit/scripts/skill_audit_scan.py"
  ```
  Exit 0 → do nothing, say nothing. Exit 1 → triage the findings (step 2) and
  report through the standard alert path (e.g. Telegram). Exit 2 → the scan is
  broken; that itself is a finding.
- **On demand** — run the same command whenever a skill is installed,
  updated, or a new skill appears from a hub/PR before first use.

### 2. SCAN — detect what changed

`scripts/skill_audit_scan.py` walks the store and maintains a SHA-256
baseline manifest (`state/skill-audit-baseline.json`). It reports:

| Finding | Meaning | Severity |
|---|---|---|
| `new_skill` | skill not in the baseline (never reviewed) | medium |
| `changed_skill` | a file was added/modified/removed since the baseline | medium |
| `removed_skill` | skill vanished from the store | low |
| `suspicious_content` | heuristic pattern hit (pipe-to-shell, hardcoded secret, `eval`…) | low/high |

Details:
- A skill is any directory containing `SKILL.md` — the scanner handles both
  flat stores (`caveman`) and categorized stores (`security/ai-safe-audit`).
- The baseline **advances after every scan**, so each change surfaces exactly
  once. A clean scan today is the known-good state for tomorrow.
- Heuristic hits are hints, never verdicts: they are only scanned on
  NEW/ADDED files, and triage makes the real call.

### 3. TRIAGE — reviewed vs unreviewed

For every finding, classify BEFORE acting:

1. **Known-allowlisted** — skill is in
   `state/skill-audit-allowlist.json` (`["security/ai-safe-audit", …]`).
   - A first-appearance `new_skill` that is allowlisted is skipped by design.
   - **A `changed_skill` is NOT skipped** — allowlisted just means "reviewed
     once"; a reviewed skill that changed needs re-review. That is the
     distinction the allowlist exists to draw.
2. **New/unreviewed** — read the skill. `SKILL.md` frontmatter (name,
   description, author), any bundled `scripts/`, and `references/`. Ask:
   - Does it match its description? Does it reference external hosts, fetch
     and execute remote content, or ask the agent to exfiltrate data?
   - Cross-check with `ai-safe-audit` (INPUT/EXEC/LOGIC/INFRA/DATA domains)
     for anything that will act on untrusted input.
3. **Changed** — diff against the baseline (hashes are per-file; the finding
   lists added/modified/removed paths). Review ONLY what changed; unchanged
   files were reviewed before.
4. **Suspicious** — confirm or dismiss the heuristic with a human-eye read.
   A real hit (e.g. `curl … | sh`, a hardcoded secret) escalates to a
   high-severity alert, not a quiet patch.

### 4. PATCH — propose, never apply

- Propose a **diff**: remove an injected file, pin a skill version, tighten a
  script's permissions, move a hardcoded secret to `~/.hermes/secrets/`.
- Present the proposal for review (same alert path). Apply only after
  approval.
- If a skill is malicious: quarantine it (move out of the store, e.g.
  `~/.hermes/quarantine/`), alert, and record the SHA-256 in a blocklist note
  inside the findings report so the same artifact is recognised elsewhere.

## Success criteria (from issue #153)

- [x] Nightly skill-store scan runs and reports findings via the standard alert path
- [x] Triage output distinguishes known-allowlisted skills from new/changed ones
- [x] Patch step proposes (not auto-applies) fixes for review

## Cron wiring (owner step — not auto-created)

The scan is a CLI + skill; the *schedule* is the owner's call, matching how
the rest of the household agents are scheduled:

```bash
hermes cron add --name skill-audit --schedule "0 3 * * *" \
  --cmd "python3 $HOME/.hermes/skills/security/skill-audit/scripts/skill_audit_scan.py"
```

Silent-on-success is built in: exit 0 produces no output, so a clean night
sends nothing.
