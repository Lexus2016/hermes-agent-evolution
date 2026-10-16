# Upstream catch-up: owner decision evidence

Measured 2026-10-15 against fork main `1a69d59c9d83855d85ed6264c79b29f7c5efd2af`. These are real
independent Git merges in detached temporary worktrees, not a sequential merge
simulation. Every merge was aborted and its worktree asserted clean and removed.
No upstream writes, merge resolution or target-branch change was performed.
Repository-local custom merge drivers were not configured for these probes;
Git's normal merge behavior was used, with hooks and rerere disabled.

## Findings

- v2026.9.24 is already included: zero behind, zero conflicts.
- v0.21.6: 9,180 commits behind, 178 conflicting paths, 47 core paths.
- v0.21.7: 9,957 behind, 198 conflicts, 55 core.
- v0.22.0: 10,713 behind, 201 conflicts, 57 core.
- v0.22.1: 12,929 behind, 207 conflicts, 59 core.
- 177/207 latest-release conflicting paths also conflict at v0.21.6.
- Classification against actual common ancestor
  `f97608f178d1ffeca59860195ab7da295f7c8e5f` reports all 198 v0.21.7 paths
  and all 207 v0.22.1 paths as `manual`: both sides changed. This is evidence
  of overlapping edits, not a verdict that every hunk is irreconcilable.

Core is conservatively all `agent/`, `hermes_cli/`, `cron/`, `hermes_state*`,
and `run_agent.py`. Every outstanding candidate is OWNER-SESSION under the
existing ten-non-core-conflict ceiling. Merely choosing the oldest release does
not produce a small autonomous first step. Later steps cannot be sized exactly
until a reviewed first merge exists; the independent probes do not prove what
sequential merge resolutions would produce.

## Options for the owner

1. Review a first release merge starting at v0.21.6, preserve fork behavior per
   the upstream-sync skill, run relevant integration tests, then re-probe the
   next release. This bounds release scope but still starts with 178 conflicts.
2. Review one catch-up to v0.22.1 with subsystem-by-subsystem resolution and
   tests. This avoids repeated decisions but starts with 207 conflicts, including
   59 core paths. It cannot be autonomously merged under the current gate.
3. Define a deliberate longer-term fork/rebase strategy and explicit feature
   preservation criteria before attempting the catch-up. No rebase is proposed
   as an automatic replacement for merge-based release tracking.

Issue #3455 remains open. Tooling completion does not complete the catch-up.
The code and evidence PR also exceeds the 200-line unattended self-merge cap;
no merge gate, label or policy was changed to bypass that requirement.

## Reproduce

Fetch the exact published release tags first, then run the commands in step 1
of `skills/evolution/evolution-upstream-sync/SKILL.md`. The probe itself is fully
offline and pins source and target SHAs. JSON includes the full conflict paths;
classification JSON includes each side's mode/type/object ID and commit SHA.
Historical counts are not invariants in tests.

Validation: four real-Git contract tests through `scripts/run_tests.sh`; Ruff
check/format; Windows-footgun check; generated skill docs regenerated and checked
for repeatability. The old #3470 implementation was reproduced returning no
error for unrelated histories; the fixed implementation reports the fatal merge
and OWNER-SESSION. Clean merges, contained tags, unknown refs, and cleanup of
registered worktrees are exercised offline.
