# Evolution admission receipts

Autonomous `evolution/issue-*` PRs must pass class-specific admission in
`scripts/evolution_merge_gate.py`. Other PRs retain their existing policy.

The gate classifies changed paths as memory/skill documents, tools, control
logic, or plugin composition. Mixed changes require the union of their checks.
All classes need the successful CI aggregate; tool/plugin changes additionally
need Python E2E, and control changes also need the Windows footgun check.
The policy table is `CLASS_GATES` in `scripts/evolution_admission.py`.

Evidence comes directly from the repository's GitHub Actions API, for the exact
reviewed SHA, the latest pull-request run of `.github/workflows/ci.yaml`, and its
current attempt. Local scorecards cannot substitute for these receipts. Missing,
failed, skipped, malformed, future-dated, or older-than-seven-day evidence blocks
admission with a diagnostic. Workflow/action changes still require human review;
a candidate cannot change its own verifier and attest to itself.

Immediately before the atomic merge call, the gate records the receipt in
`<HERMES_HOME>/evolution/admission.sqlite3`. The cycle is the issue branch; the
repository, cycle, and SHA form an idempotency key. A SQLite immediate transaction
serializes admission accounting. Rechecking or retrying a SHA never spends its
budget twice. Valid same-head revalidation refreshes its stored baseline and
receipt without charging twice; a failed final binding check creates no admission.
A failed merge leaves the admission receipt, not a claim that the
merge succeeded. Each record includes its classes and verifier run/attempt.

The autonomous regression budget is zero: successful independent CI attests to
zero observed CI regressions, not proof that no untested behavior regressed.
Existing example-level flip gates remain additional requirements. Unlike GRASP's
improvement-oriented scoring, admission never expands its budget to tolerate
baseline failures. The existing line cap, high-risk path checks, and atomic
head-SHA merge protection remain in force.

Validation data independence is checked separately from execution provenance:
the run's immutable PR baseline is compared to the candidate. Existing baseline
tests, fixtures and evals cannot be edited, renamed or deleted by a self-admitted
change; test runners, configuration, hooks and workflow machinery cannot be
changed either. Added tests supplement the baseline but never replace it. All
baseline Python test slices must be present, and their actual execution steps
must succeed. Class-specific validation steps cannot be skipped under a green
job. This deliberately routes baseline-test changes and docs-only runs that skip
the baseline suite to human review, with a missing-evidence diagnostic. It does
not infer a successful replay from candidate-authored narrative reports.

## Upstream CI compatibility: supplemental E2E

PR CI intentionally skips E2E. Admission still requires its latest successful
exact-head PR run, complete executed regression slices, class-specific lint steps,
and `All required checks pass (v2)`. Tool/control/plugin changes additionally need
the latest successful exact-head `workflow_dispatch` run of the same `ci.yaml`,
including the actually executed `Python tests / e2e` → `Run e2e tests` step.
A successful job with a skipped step does not count. Dispatch never substitutes
for PR validation. Both receipt IDs, attempts and timestamps are recorded.

When the gate reports missing supplemental evidence, an operator or an evolution
stage already authorized to run validation can use:

```bash
gh workflow run ci.yaml --repo Lexus2016/hermes-agent-evolution \
  --ref <verified-candidate-branch> -f release=false
gh run list --repo Lexus2016/hermes-agent-evolution --workflow ci.yaml \
  --event workflow_dispatch --commit <reviewed-head-sha> \
  --json databaseId,headSha,status,conclusion
gh run watch <matching-run-id> --repo Lexus2016/hermes-agent-evolution --exit-status
```

Recheck the branch's server SHA before dispatch. This existing manual workflow
runs all validation lanes, including native OS and E2E jobs; it is not a cheap
E2E-only command. Do not set `release=true`, trigger live-provider or publication
workflows, or request extra credentials. If CI spending has not been authorized,
request that authorization instead. The merge gate only reads receipts and never
dispatches workflows. Wait for terminal results, then rerun the gate; an older
successful run cannot replace a newer failed/incomplete attempt. If the branch
moves, the resulting different-head run cannot validate the reviewed candidate.

Dispatch runs have no trustworthy PR-baseline metadata. The verifier therefore
uses the primary PR run's frozen baseline and independently fetches the current
open fork PR, requiring the same head and base on fork `main`. Immutable
`base...head` comparison must show unchanged baseline tests and validation
machinery, including renamed-away workflows. Workflow/action changes require
human review. A stale base requires fresh PR validation, not accepting a dispatch
as a substitute. The PR binding is checked again immediately before merge.
GitHub atomically checks the head SHA only: a base update after that final read
is still possible; this is not a base-SHA compare-and-swap guarantee.
