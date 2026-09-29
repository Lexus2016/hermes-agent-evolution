---
title: "Memory Consolidation — Autonomous sleep-time memory consolidation"
sidebar_label: "Memory Consolidation"
description: "Autonomous sleep-time memory consolidation"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Memory Consolidation

Autonomous sleep-time memory consolidation.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/memory-consolidation` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `cron`, `memory`, `tqmemory` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Memory Consolidation (Sleep-Time Compute)

## Overview
Runs an autonomous offline pass over recent session notes to deduplicate episodic fragments, promote recurrent patterns to durable memory, and create cross-session entity links without consuming live interactive turn tokens.

## Execution Procedure
1. Query uncompressed/episodic notes via `tqmemory.semantic_search(query="*", tier_filter=["episodic"])`.
2. Pass retrieved notes to `SleepTimeMemoryConsolidator.consolidate_notes(notes)`.
3. Apply resulting actions:
   - For `promote` actions: call `tqmemory.promote_note(note_id)`.
   - For `link` actions: call `tqmemory.link_entities(source_uri, target_uri, relation_type)`.
   - For `deprecate` actions: call `tqmemory.deprecate_note(note_id)`.
4. Output a summary consolidation report.

## Verification
- Confirm consolidation report generated with non-negative merged and promoted counts.
