# One-time v0.22.1 inherited-debt review

This is a bounded disposition mechanism for issue #3455. Its initial authority is
explicit owner authorization and independent review of the exact integration tree,
manifest, checker, dependency pins and workflow wiring. The manifest digest detects
subsequent drift; candidate code cannot authenticate its own human review.

## Activation and future changes

The transition requires exact main baseline
`365acb3229e7301a3b3566cc5b32114f06c023d1` and actual ancestry from upstream release
`297552a9d646119871391d202509822cd86330df`. Missing history or a different baseline
fails closed. There is no runtime override. The health CI checkout fetches full
history. Once upstream is already an ancestor of the comparison base, this adapter
is dormant and the ordinary ratchet applies with unchanged thresholds.

The initial mechanism checkpoint has an empty manifest. Separately reviewed
activations currently record **338 occurrences across 322 exact entries**. These
cover individually reviewed name bindings, bounded runtime/error boundaries,
process-bootstrap options, metadata latency, structural test metrics, a literal attribution-map size, one status-view function length, and one test-dispatcher complexity. The
manifest contains each precise rationale, source identity and validation limit.
Structural-only test-size dispositions claim unchanged test organization, not a
runtime pass or general feature compatibility. Real bugs and behavioral findings
remain separate blocking gates. Two scheduler deferred-annotation findings and the
unreadable-skill listing fallback remain unresolved.

The manifest is the separate accepted-debt report; it never rewrites a raw receipt.
All other findings remain blocking until individually supported and reviewed.
Owner authorization permits recording only inherited findings that do not impair
fork functionality or security. Unknown behavior, real defects and integration
growth cannot be accepted as inherited debt.

## Evidence contract

Each reviewed entry identifies the candidate file blob, exact pinned source commit
and blob, identical path and scope, rule, diagnostic and occurrence count. Pattern
entries also bind the exact normalized code fingerprint. Source occurrences are
measured by the same checker rather than trusted from manifest counts. Metrics may
not exceed the measured source value. Copied or renamed units receive no exception.

For modified files, the unit must match the source body exactly and the review must
explicitly cover surrounding imports and globals. A provenance match alone does not
establish correctness. Every entry additionally needs a substantive compatibility
rationale, reviewer receipt and passed validation references bound to repository
blobs. Validation metadata is a human-reviewed assertion, not independent execution proof.
Pending evidence never grants credit. A changed reviewed file or evidence
reference invalidates its disposition and requires review again.

The initial review receipt must independently record the final commit/tree and
manifest SHA-256. Any subsequent candidate change requires renewed review. Existing
autonomous high-risk gates cover the checker, evidence, workflow, admission and tool
configuration paths; this is an owner-directed integration, not autonomous admission.
CODEOWNERS is advisory unless repository protection separately enforces it.

## Reports and validation

`v0221-raw-report.json` preserves the complete historical comparison from main3cd to
ff327c842c, including all 1,239 blocking and 41 advisory findings. It is a historical
receipt, not a claim about the current candidate. New reports retain every raw
finding and its original `blocking` value; accepted inherited dispositions are a
separate field. The effective exit status subtracts only matching reviewed entries.
Keep the final exact-head report alongside the historical receipt during review.

The health command has a bounded 3,600-second subprocess budget because the first
measured full comparison took about 14 minutes. Other checks retain 900 seconds.
Timeout is failure. The standard read-only Ubuntu job has a 60-minute bound.

Synthetic real-Git tests exercise ancestry, manifest drift, source occurrence
budgets, stale evidence, copied files and units, metric growth and dormant operation.
They test enforcement mechanics; they do not prove the truth of human review or
replace actual fork regression suites and required exact-head hosted CI.

## Immutable intermediate report after initial dispositions

`v0221-e4d3-health-report.json` records the fresh, uncached comparison of
`e4d3f42197134f4830c3ec44463bbb75f1c6f75e` against the historical main baseline
`3cd9963bcce9c0aba451dadeaff5c701bca102e4`.
It completed in 527.4 seconds with exit 1: 1,178 raw blocking findings,
272 matching reviewed dispositions, and **906 unresolved blockers**. The report
SHA-256 is `1029b71a2bea931aa380915c1a5151bc50d71abc14ba1540a244c1034c6643c5`.
This is an intermediate failed gate, not final acceptance of later corrections.

Diagnostic provenance classified 775 unresolved findings in exact upstream files,
118 in exact upstream Python units within modified files, and 13 other module/unit
cases. A separate canonical TypeScript measurement establishes one of those 13
(the mock-server callback) also has identical upstream body and complexity.
Provenance alone does not establish safety or qualify any entry for acceptance.

Remaining families include 422 exception-handling findings, 238 subprocess/timeout
findings, 209 structural metrics, 25 name-binding findings, eight environment
boundary findings and four asynchronous blocking-I/O findings. Individual review
and relevant failure-path evidence remain necessary. Local terminal/socket/native
limitations and pending hosted validation cannot be converted into passing receipts.

## Reviewed fork-main advancement

`v0221-main365-baseline-review.json` records the real main security merge and
integration ancestry, scoped regression receipts, and unchanged disposition
identities. Only the exact activation base moved from main3cd to main365; the
historical report bytes and their original comparison identities remain intact.
The old base cannot reuse the refreshed transition policy. The fresh immutable comparison below records the first complete main365 result.

Scanner validation preserves the fork's existing off/yolo/permanent-allowlist and
explicit unattended-approve bypasses. Invalid policy refuses when scanning is
reached, including unattended-deny and interactive paths. Scanning is not universal.

The fork scanner defaults remain enabled with path `tirith`, timeout five seconds
and fail-open true, matching current main. Explicit disabled, fail-closed and
custom settings survive configuration migrations; the upstream version-50 removal
is retained only as a no-op compatibility slot. Values already erased by an earlier
external upgrade cannot be reconstructed by this correction.


## Fresh main365 comparison at 9ad6d6a828

`v0221-9ad6-main365-health-report.json` preserves the full unmodified report for
head `9ad6d6a828dab0f1345e2e528c6121e8cd430c8b`, tree
`b7b0f01d9c1116303f6ffbdfe8af18a2b08ecd2c`, compared with main
`365acb3229e7301a3b3566cc5b32114f06c023d1`. Fresh measurement completed in
527.2 seconds with exit 1. All 320 reviewed occurrence dispositions matched;
1,145 raw blocking findings leave **825 unresolved blockers**. The report retains
raw severity and separate inherited dispositions. Its SHA-256 is
`1b511fdd12f0539d9fd7c5a61520dcb398bba9f5521bcc4ff76ca7005b2a8c46`.

The unresolved rules are BLE001 366, CC 136, HX013 116, HX006 106, S110 32,
FUNC_LINES 24, FILE_LINES 22, HX011 15, F821 5, NESTING 2 and HX002 1.
Diagnostic provenance identifies 727 whole-file upstream matches, 92 same-scope
Python-unit upstream matches, and six other cases. These identities do not prove
correctness or grant acceptance. There are also 41 unallowed advisory findings
and 124 inline-allowed findings. The full gate remains failed.

This receipt applies only to the named head. Subsequent corrections require
appropriate regression checks and a new final immutable comparison. Required
hosted Python, Hindsight protected-API lifecycle, desktop typecheck, E2E and native
platform gates remain pending; local socket limitations are not passing results.


The host-file metadata correction changed the enclosing probe function. Its prior
three-occurrence remote-probe disposition was removed because the exact upstream
unit identity no longer holds. The historical review artifact is retained for
traceability, but grants no current exception. Real metadata uncertainty now
refuses replacement rather than treating an existing file as absent.


Two postponed Cron provider return-annotation findings retain an explicit metadata
limitation: generic `typing.get_type_hints` raises `NameError`. Supported provider
discovery and dispatch do not evaluate these annotations; seven focused caller
tests and actual canonical-provider probes passed. The narrow disposition does
not claim repaired introspection and introduces no eager scheduler/profile setup.


## Fresh corrected-source comparison at 9ff0fd4197

`v0221-9ff0-main365-health-report.json` preserves the full unmodified report for
head `9ff0fd41973bff45a288e56e1700f9b6433d7675`, tree
`969c303093d70a9b4e2a2d8222f8fe83d0fb451f`, against the same exact main365
baseline. It completed normally in 539.6 seconds with exit 1: 1,134 raw blocking
findings, 352 matched reviewed dispositions, and **782 unresolved blockers**.
Its SHA-256 is `3e6ec0448f68ba5e9860cc253024bcb0e21eb5359491c0c1bad518f301788276`.
The previous 825-blocker receipt remains an unchanged historical result.

The summary records every rule/domain count. Diagnostic provenance identifies
665 exact upstream files, 91 same-scope Python units and 26 other findings;
that classifier does not establish TypeScript unit identity or correctness.
No unreviewed disposition is inferred from these counts. The gate still fails.

The exact clean source passed canonical Ruff and the consolidated corrected
desktop selection: 381 tests in 19 suites, with all 32 selected file hashes
unchanged before/after. The bound receipt and validation report preserve scope
and commands. These mocked/React tests do not replace hosted full Python,
Hindsight protected API/lifecycle, native platform or full desktop typechecks.
Boot recovery cannot cancel an already-started cloud ladder/reset; stale results
are prevented from resetting or reloading a replacement owner.


## Frozen d66b verification (2026-10-16)

The exact source commit `d66b3ea03b130ee28b50533fe947af83098a200e`
(tree `af70c706ccd76f9a3f24598b58b47f54ff9d6b72`) completed fresh health against
main `365acb3229e7301a3b3566cc5b32114f06c023d1` in 537.3 seconds, exit 1.
The verbatim report retains 1,126 raw blocking findings, 352 matched reviewed
occurrences and **774 unresolved blockers**. Canonical full Ruff passed.
No source moved during measurement; the previous 9ff0 report remains historical.

The corrected desktop regression expanded to 410 passing tests across 25 suites,
with all 39 selected source/test bindings unchanged. Group workspace extraction
removed its integration-created growth while preserving parent state and callbacks.
The media streaming correction has 128 passing tests and three native-platform
skips. Its private descriptors and cleanup retain explicit non-POSIX ACL and
check-before-rename/unlink limitations; codec derivative writers are outside that
correction. These scoped receipts do not establish full desktop/native or hosted
acceptance. Workflow source inventory remains identical across all 58 files.

After this terminal run, separately reviewed metadata commit `c930dcfc2478` added
only two CC dispositions (bootstrap event presentation and profile normalization),
validated with 27 adapter tests. The manifest now has 338 entries and 354 occurrence
budgets. The d66b report is not relabeled as a report for that later commit.


## Test timing evidence repair

Commit `dfbffa67c4a15f76d3b2d4f7af383453780dcc90` changes only three test
files: it observes the socket shutdown return, drives the attempt-origin budget
with a module-local logical clock, and verifies every real refresh inter-hold gap.
The same 83 collected cases and 46 test definitions/decorators are retained.
A deliberately regressed lifecycle-origin control fails at the expected retirement
deadline; the restored committed tree passes all 83 tests with retries disabled
and full canonical Ruff. The real 2-second loopback prefill remains unchanged
and has no accepted debt disposition. No production or acceptance policy changed.
The full d66b health result remains historical and failed, not a report for this
later test-only checkpoint. Exact receipts are in
`v0221-dfbf-test-timing-validation.json`.

## Lossless evidence packaging (2026-10-16)

The five paths below are now small storage indexes, with ordered UTF-8 text parts
under `<path>.parts/`. Historical receipt content and published history are unchanged.
Digest claims for these artifacts refer to the **reconstructed original bytes**,
not the storage index. No policy thresholds or accepted entries were changed.

| Original artifact | Reconstructed bytes | Original SHA256 |
| --- | ---: | --- |
| `v0221-manifest.json` | 867025 | `9cc8ae0890c9a2a9fb63ea50d04100fa906df0746140ce8653425146904dde71` |
| `v0221-9ff0-main365-health-report.json` | 533663 | `3e6ec0448f68ba5e9860cc253024bcb0e21eb5359491c0c1bad518f301788276` |
| `v0221-d66b-main365-health-report.json` | 531603 | `f29657a0a665dfbc0c737139001066874bcddb5cd9f368f5f0e34b8e99c4b6ea` |
| `v0221-9ad6-main365-health-report.json` | 519685 | `1b511fdd12f0539d9fd7c5a61520dcb398bba9f5521bcc4ff76ca7005b2a8c46` |
| `v0221-ef4609-main9ef-health-report.json` | 531370 | `f8f7c2dfb93aee0ca46a5b1c309256bce2b7c3665ac42f79bed65bbef0e20182` |

From the repository root, reconstruct and independently compare an artifact with
the published pre-packaging checkpoint (substitute any filename from the table):

```bash
python -m scripts.code_health.evidence_parts analysis/sync-health/v0221-manifest.json > /tmp/v0221-manifest.json
git show 7daf390b3fb41c2ed97de13367d05def561620b7:analysis/sync-health/v0221-manifest.json > /tmp/v0221-manifest-original.json
cmp /tmp/v0221-manifest-original.json /tmp/v0221-manifest.json
sha256sum /tmp/v0221-manifest.json
```

The independent local review used `23551df05bf722d1cda34b0c9fd33518c31ee2f1`
as its byte-identical pre-packaging comparison; that locator is local-only.

Use `--revision <commit-or-tree>` to reconstruct from Git rather than the worktree.
The reconstructed manifest retains 338 entries and 354 occurrence budgets and its
existing pinned digest. The loader accepts old direct-file evidence too. Packaging
is bounded to eight parts of at most 256 KiB each (2 MiB total), uses exact sequential
paths, rejects unexpected/missing entries and symlinks, and verifies sizes and hashes
before the unchanged policy digest validation. It introduces no trust authority.
