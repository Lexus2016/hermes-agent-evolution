# #3455 final-main reconciliation checkpoint

Measured source: `ef4609bc96f59c3354cd76daadebf861e44580ff`, tree
`f236a9b5da1df9cc6776f4e9fdffebc6a606dc75`.
Baseline: `9efba6dcfa78013a3763778aaca4fe37921c495e`.
Both that main commit and pinned upstream `297552a9d646119871391d202509822cd86330df`
are genuine ancestors. No integration branch has been published or merged remotely.

## Verified result

Canonical full health exited **1**. The JSON preserves all 1,287 findings:
1,122 raw blocking findings, 354 individually reviewed inherited dispositions,
**768 unresolved**, 124 inline allowances and 41 advisories.
The manifest contains 338 entries with 354 occurrence budgets. No thresholds or
budgets were increased during this main reconciliation. Full canonical Ruff passed.

Unresolved groups:
- 383 exception-handling findings: BLE001 352, S110 31
- 151 structural findings: CC 109, function length 21, file length 19, nesting 2
- 106 subprocess-timeout findings (HX006)
- 112 test-wait findings (HX013)
- 15 dispatch-structure findings (HX011)
- 1 environment-boundary finding (HX002)

These are unresolved policy findings, not 768 demonstrated functional defects.
656 are in files exactly matching pinned upstream; 112 are in modified or
integration-only files. Provenance alone establishes no correctness exemption.
Compared with historical d66b/main365, there are no added unresolved fingerprints.
Two previously approved structural dispositions and four reviewed deterministic
wait corrections account for the six removed occurrences. Both source and baseline
changed; the historical report remains immutable and is not relabeled.

## Retained corrections and scoped evidence

- Final main process lifetime ownership retained, with integration environment
  behavior: 112 lifecycle passes / 72 native skips, 29 environment passes,
  and 7 actual plugin-host passes.
- Canonical media Docker/no-chmod policy retained in the shared helper; prior
  streaming privacy correction unchanged: 160 passes / 62 host skips.
- Watchdog halt/weekly/PATH behavior: 96 passes.
- Native workflow canonical-toolchain adaptation, exact proof inventories,
  strict JUnit rejection and cancelled-classifier regression: 180 passes.
- Installed evolution sibling deployment and standalone funnel: 77 passes.
- Final main harm grammar and observer cleanup: 55 passes / 25 native skips.
- Inherited-policy ancestry, drift and budget contracts: 27 passes.

Counts overlap and must not be added into a unique-suite total. Scope receipts
are in the reviewed reconciliation and baseline artifacts. Platform skips do not
prove adapted macOS/Windows behavior. Two blocklist execute-code cases remained
blocked by AF_UNIX EPERM; their console excerpt is explicitly qualified.

## Remaining completion gates, in order

1. **Authentic upstream import.** The dedicated import ref returned 404 at 2026-10-16 15:08 UTC
   on the final remote check. The owner must use existing authenticated Git access to push the
   exact official upstream commit to `refs/heads/import/upstream-v0.22.1-3455`.
   The supplied bounded script verifies the full upstream object/history and
   shared ancestor, changes no main branch or tags, and refuses a conflicting ref.
   Do not reconstruct or fabricate upstream ancestry through Git-data APIs.
2. **Paused protected-path work.** A separately identified protected-runtime
   identity defect remains unresolved under an explicit safety pause. No further
   probe, implementation, alternate route or claim of closure is authorized here.
   It requires an appropriate human/security handoff before acceptance.
3. **Current-main runtime validation.** Exact main9ef postmerge E2E reported two
   SQLite deleted-shm observation failures (269 other E2E cases passed), despite
   earlier candidate CI passing. Compaction cases passed 6/6. Diagnostic tracing
   was denied before execution and was stopped. A separate bot-desktop orphan-detection test passed
   only on retry. These are open validation gaps, not proven harmless flakes.
4. **Health acceptance.** Resolve the 768 remaining findings through bounded
   behavior-preserving fixes or individually reviewed nonbreaking evidence.
   Structural identity can support only the metric itself; catches, timeouts,
   state, credentials and filesystem boundaries need their own behavior evidence.
   Unknowns remain blocking. No blanket waiver or endless automatic disposition
   generation is part of this checkpoint.
5. **Exact published-candidate gates.** After the preceding prerequisites,
   publish the reviewed tree with genuine histories and run required hosted
   Python/E2E/upgrade, real isolated Hindsight health/auth/shutdown smoke,
   adapted native process/media jobs, desktop tests and complete type checks.
   Prior desktop exit137 is not a verified OOM or a successful type check.
   Local AF_UNIX restrictions and native skips require real suitable runners.
6. **Final review and normal merge.** Recheck current main/head/tree, report
   identities, workflow side-effect guards and required results. Only then merge
   normally and close #3455 with verified evidence. This checkpoint is not ready
   for merge and does not close the issue.

No further defect investigations, acceptance additions or repeated full scans were
started in this integration pass after this terminal report. A later source or baseline change requires a
new explicitly identified result; these files retain their exact measured identity.
