# One-time v0.22.1 inherited-debt review

This is a bounded disposition mechanism for issue #3455. Its initial authority is
explicit owner authorization and independent review of the exact integration tree,
manifest, checker, dependency pins and workflow wiring. The manifest digest detects
subsequent drift; candidate code cannot authenticate its own human review.

## Activation and future changes

The transition requires exact main baseline
`3cd9963bcce9c0aba451dadeaff5c701bca102e4` and actual ancestry from upstream release
`297552a9d646119871391d202509822cd86330df`. Missing history or a different baseline
fails closed. There is no runtime override. The health CI checkout fetches full
history. Once upstream is already an ancestor of the comparison base, this adapter
is dormant and the ordinary ratchet applies with unchanged thresholds.

The initial mechanism checkpoint has an empty manifest. The separately reviewed
activations accept 222 demonstrated nonfunctional F811 occurrences, the exact
context-compressor test-module size backed by 302 passing tests, 17 individually
reviewed runtime findings with bounded regression evidence, and 32 frozen-fixture
or dynamic-binding F821 occurrences. Five deferred-annotation findings and the
unreadable-skill listing fallback remain unresolved. A later separately reviewed group adds eleven test-file size and two test-harness
complexity findings, backed by 1,124 passing tests (eight explicit skips) and
thirteen LSP lifecycle tests. This is 285 occurrences across 270 exact entries,
not a claim of complete repository validation. The manifest
is the separate accepted-debt report; it never rewrites the raw receipt. All other
findings remain blocking until individually supported and reviewed. Owner authorization permits recording only
inherited findings that do not impair fork functionality or security. Unknown
behavior, real defects and integration growth cannot be accepted as inherited debt.

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
`e4d3f42197134f4830c3ec44463bbb75f1c6f75e` against the pinned main baseline.
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
