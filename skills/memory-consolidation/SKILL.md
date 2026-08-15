---
name: memory-consolidation
description: Autonomous sleep-time memory consolidation.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [cron, memory, tqmemory]
    category: memory
---

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
