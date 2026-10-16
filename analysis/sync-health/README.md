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
activations currently record **320 occurrences across 305 exact entries**. These
cover individually reviewed name bindings, bounded runtime/error boundaries,
process-bootstrap options, metadata latency, and structural test metrics. The
manifest contains each precise rationale, source identity and validation limit.
Structural-only test-size dispositions claim unchanged test organization, not a
runtime pass or general feature compatibility. Real bugs and behavioral findings
remain separate blocking gates. Five deferred-annotation findings and the
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
The old base cannot reuse the refreshed transition policy. A fresh complete
comparison against main365 is still required.

Scanner validation preserves the fork's existing off/yolo/permanent-allowlist and
explicit unattended-approve bypasses. Invalid policy refuses when scanning is
reached, including unattended-deny and interactive paths. Scanning is not universal.

The fork scanner defaults remain enabled with path `tirith`, timeout five seconds
and fail-open true, matching current main. Explicit disabled, fail-closed and
custom settings survive configuration migrations; the upstream version-50 removal
is retained only as a no-op compatibility slot. Values already erased by an earlier
external upgrade cannot be reconstructed by this correction.
