# v0.22.1 integration preservation and validation

Status: integration candidate; full-tree validation and independent review remain pending.
This is an implementation record, not approval to deploy or publish a release.

## Frozen inputs

- Fork: `dbf42adee2aed357ceb9a10da0f2315fb4a711f3`.
- Upstream v0.22.1: `297552a9d646119871391d202509822cd86330df`.
- Merge base: `f97608f178d1ffeca59860195ab7da295f7c8e5f`.
- Initial merge: 207 conflicting paths, including 59 core paths. These are
  measurements of the frozen inputs, not promises about future merge attempts.
- Concurrent remediation main: `3cd9963bcce9c0aba451dadeaff5c701bca102e4`
  (aggregate #3485). Its true ancestry and behavior must enter this candidate before
  final validation; incorporation is still pending.

## Preservation checklist and decisions

| Domain | Required preserved behavior | Integration decision / validation |
|---|---|---|
| Agent lifecycle | Prompt-cache stability, auxiliary routing, compression isolation, plugin toolset validation | Port fork behavior into upstream extracted lifecycle/turn owners; remove duplicate obsolete facade implementations. Targeted prompt, routing and isolation suites pass. |
| Provenance and pinned constraints | Trusted ingress only, profile-local constraints, persistent origin normalization | Union origin and message UID fields; preserve untrusted import sanitization and trusted profile adoption. Validate disk-ingress normalization and historical replay with synthetic fixtures. Independent provenance/persistence slice: 300 passed. |
| Database compatibility | Both fork-origin and upstream-UID historical layouts | Preserve both physical lineages in history, then apply merged final schema. No invented combined historical release layout. Schema-history slice: 4 passed. |
| File operations | Special-file refusal, write refusal taxonomy and recovery, profile-safe boundaries | Preserve fork guards with upstream extracted file owners and diagnostics. Focused file-operation slice: 132 passed. |
| Skills | User-owned, pinned, bundled and organization-mirror protections | Deliberately reject upstream expansion of autonomous background edits. Retain enforceable operation guards and matching prompts. Read-before-write does not grant ownership. Protected-write matrix: 15 passed; skill-manager suite: 79 passed. |
| Memory | Provenance, staging/replay, supersession, atomic operations and error isolation | Combine fork memory semantics with upstream surgical patch API. Invalid target requests cannot trip the provider outage breaker. Focused contract/breaker slice: 70 passed. |
| Bundled providers | Honcho breaker/reset, Mem0 temporal validity/supersession, Home Assistant reconnect behavior | Retain complete packages, tests and manifests as an explicit fork exception to upstream externalization. Use declared PM extras and current host APIs. Migration must not automatically replace them with catalog copies. Real bundled precedence/update coverage includes Honcho, Mem0 and Hindsight. |
| Terminal approval | Configured Tirith outcomes remain visible; configured fail-closed remains blocked | Adapt scanner into new approval flow. Disabled/unconfigured baseline avoids scanner imports. Session-scoped approval and original failure cases retained. Approval and migration slices: 180 passed. |
| Telegram | Shared send/edit pacing, certain-nondelivery resume, cumulative delivery IDs, no repeated head | Use upstream extraction with fork paced reentrant gate and preserved tail accounting. Ambiguous timeout never acquires a resumable tail. Network/pacing slice: 24 passed. |
| Gateway/TUI | Session spool, profile ownership, cancellation, persistence, GUI schemas | Port into canonical upstream owners; keep transport flags explicit. TUI slice: 660 passed with isolated temporary roots. |
| CLI/update | Snapshot compaction, tqmemory registration, Evolution cron reconciliation | Adopt upstream fresh-process handoff and PM ownership; move retained maintenance into new post-update owners. Remove old duplicate facade implementations. Autostash/owning-install slice: 41 passed. |
| Cron | Provider snapshots/drift guard, audit/backoff/no-op, profile-local dotenv, usage/tool counts | Preserve upstream canonical execution and receipt bookkeeping. Apply dotenv before sanitizer, never after. Transport tool counts with the job receipt rather than an unwritten process-global cache. Jobs/count slice: 136 passed. |
| Installers | Fork destination, source aliases, failure diagnostics, native runtime resolution | Adopt PM bootstrap; retain fork URL overrides and verified redirect aliases. Product-completion diagnostics replace obsolete inline npm failure assertions. Synthetic real-git redirect/diagnostic slice: 5 passed. |
| CI | Fork cost controls, deterministic autonomous merge gates, fork ownership | Preserve standard-runner defaults and OS opt-in. Aggregate results understand explicitly inapplicable fork OS lanes while selected/upstream/release lanes fail closed. CI routing/evaluator slice: 272 passed. |
| Release automation | No new unattended fork publishing, pruning or deployment | Gate canary publication/pruning and automatic site deployment to intended upstream repository; heavy native bundle jobs require upstream or explicit opt-in. Manual workflows are not executed as part of integration. |
| Review policy | Fork owner and actual gate semantics | Keep `@Lexus2016` ownership rules plus new trust-boundary paths. Upstream review-label retirement is accepted: previous fork label behavior was advisory. CODEOWNERS enforcement depends on branch protection and has not been verified. |

## Dependency and platform decisions

The merged lock is generated from actual package metadata using repository-pinned
uv 0.12.3, with no hand-written integrity hashes. A fresh Linux Python 3.14 environment
installed successfully from the frozen lock with dev/test groups and all, otel,
Honcho, Mem0 and messaging extras. Embedded Hindsight requires a separately pinned PM environment because its server
dependencies conflict with core SDK pins. Ambiguous legacy data in non-default
profiles requires an explicit non-destructive operator choice; it is not silently
imported, deleted or replaced by an empty store. This is a documented rollout
requirement, not fully automatic migration.

Runtime plugin setup uses PM rather than mutating
Hermes dependencies with ad-hoc pip commands.

The fork's 14-day release-age cutoff and existing explicit exception table are
retained. Google Meet's Playwright-dependent extra is explicitly unavailable on
Android, where Playwright supplies no supported wheel; ordinary desktop platforms
remain eligible. This does not disable unrelated browser providers or tests.
The unchanged upstream DMG background `apps/desktop/packaging/nous-dmg-2b.tiff`
has an exact-path blob-size exception (SHA-256
`9d71588c15d855bed520239f58eb3ca8d9056a76fc6978e386f9cefbe8504a46`).
Its existing LZW compression, pixels and metadata are unchanged; the global size
limit remains intact.

The pinned embedded Hindsight 0.6.1 dependency set requires Python 3.12 rather
than the core Python 3.14 runtime. The explicit canonical PM selector provisioned official CPython 3.12.13 and the
exact pinned server dependencies; installed-runtime imports passed. Synthetic
lifecycle coverage passed 103 tests, with one skip. Actual server startup reached
PostgreSQL initialization but was blocked by local Unix-socket permissions.
Installed-runtime health, protected-endpoint authentication and successful shutdown
remain required trusted-CI gates. Readiness and authentication are separate contracts.

Native Windows/macOS behavior still requires the corresponding native CI lanes.

## Validation rules and remaining work

All Python test receipts use `scripts/run_tests.sh`. Counts above are individual
slices and overlap; they must not be summed into a whole-suite result.

The first tools diagnostic run, during unresolved cross-domain integration, had
11,759 passed, 163 failed, 143 skipped and 12 collection-blocked files. Those
failures remain an explicit repair/revalidation inventory. The subsequent whole-suite diagnostic was interrupted at 37.2% after 25,397
passes and 303 failures across 150 failing files; a large group was blocked at
Unix-socket bootstrap by the local sandbox. It ran while fixes were being applied
and is neither complete nor final-tree validation. Final approval requires an
immutable candidate tree, complete reruns and review of concurrent main changes.

This environment denies some socket operations and its `/tmp/.git` placeholder
changes repository discovery for temporary fixtures. Isolated temporary roots
resolved the latter in targeted verification. Environment-blocked tests are not
reported as passes. No native Windows/macOS or live external service execution is
claimed by these local receipts.

Retained-provider test review restored five original Honcho/Mem0 user-ID cases;
the full file passes all eight cases. TLS tests follow upstream's OS-store and
real-transport architecture; its 30 current cases passed. These are bounded
preservation checks, not proof that every historical test has an exact equivalent.

Local desktop type checking terminated with exit 137 twice. Available evidence
does not establish the cause, and no type-check pass is claimed. The code-health
wrapper timed out after 900 seconds while measuring the large integration diff;
this is an incomplete gate. A subsequent immutable-tree diagnostic made progress
through both revisions but lost execution state during an environment refresh,
without a terminal result. Both require successful exact-candidate validation.

Before merge:
- [ ] Incorporate the landed fork remediation aggregate with ancestry preserved.
- [ ] Freeze the final candidate tree and rerun all required test/lint gates.
- [ ] Resolve every diagnostic failure or document a reproducible environment blocker.
- [ ] Validate native platform lanes and frontend checks where applicable.
- [ ] Complete independent preservation and final-tree review.
- [ ] Obtain green required CI on the exact reviewed head.
