---
name: evolution-analysis
description: Analyze issues and PRs to prioritize implementation
version: 1.0.0
author: Hermes Evolution
category: evolution
mode: PUBLIC
---

# Evolution Analysis Skill

**Operating mode:** PUBLIC (github token auth via GITHUB_TOKEN or gh CLI)

## Mission

This serves one mission: **become the best self-evolving AI agent in the world** —
autonomously completing real work of any level *better than any other agent* and
improving *faster than anyone*. "Best" is measured against the frontier and our
own past self, never just declared.

**Focus test (the FIRST triage filter) — keep an issue only if it makes the agent
YES to ≥1:** (1) better at autonomous real work of any level, (2) more useful to
people (owner + growing community), or (3) evolve faster/better than competitors.
Anything serving none of the three is rejected at triage (with a one-line reason),
no matter how easy it is.

## Task

Triage and prioritize **every open issue — the agent's own AND the community's,
as equal citizens** (a real user's issue is never ignored or down-weighted for
being community-filed) — against the mission, and determine priority for
implementation. The mission focus test above is applied before scoring.

## Process

### Phase 0 — Local triage pass (no GitHub API calls)

**When private tools (GitHub token / `gh` CLI) are unavailable, this phase
runs first and independently — producing a thin-list triage JSON from local
sidecar files alone, so the pipeline is never blind.** When private tools ARE
available, this phase still runs as a pre-pass to warm the selection with
local context before the GitHub-dependent steps.

```bash
python scripts/evolution_local_triage.py
# Reads: issues/, introspection/, research/ sidecars + evolution-health.txt
#        + realized-impact.txt
# Writes: analysis/YYYY-MM-DD.json (standard format, "local_triage": true)
# Does NOT call any GitHub API
```

What it does:
1. Reads the most recent `issues/`, `introspection/`, and `research/` sidecars
   from `~/.hermes/profiles/user1/evolution/`
2. Extracts filed proposals (decision=“filed” with issue numbers) and scores them
3. Reads `evolution-health.txt` for `effort_budget` calibration (1.5 or 3.0)
4. Reads `realized-impact.txt` for consolidation-mode detection
5. Sorts by priority score, applies the effort budget cap, and writes the
   result to `analysis/YYYY-MM-DD.json` in the standard format

**Non-clobber rule:** if a full (non-local) analysis already exists for today,
the script skips writing — a local-only pass never overwrites a complete one.

When this phase produces output (private tools were unavailable), the analysis
stage is DONE for this cycle — the `local_triage: true` flag in the JSON tells
downstream stages the selection is local-only and the private dispatch (PR
creation, implementation handoff) is deferred until private tools are available.

### Phase 1 — Full analysis (requires GitHub token / gh CLI)

The steps below are the original full-analysis pipeline. They run when private
tools are available, producing a richer analysis that includes live issue
bodies, rejection comments, and GitHub-side state.

1. **Retrieve** all open issues — THIN first (context economy). `gh` is
   authorized via persistent `gh auth login` (~/.config/gh), set up by
   setup-hermes.sh — do NOT export GH_TOKEN from env (Hermes strips GitHub
   tokens from the agent terminal).

   **Rule: query thin, hydrate survivors.** Do NOT pull issue bodies for the
   whole backlog — most get rejected at triage and their bodies are pure
   context waste (~10-20k tokens/run measured). Two phases:
```bash
# Phase A — THIN list (no bodies): triage + ranking work from this alone
gh issue list --repo Lexus2016/hermes-agent-evolution --state open \
  --limit 50 --json number,title,labels,createdAt
```
   Run triage (steps 2-3) and scoring (steps 4-5) on titles+labels. Only THEN
   hydrate the survivors — the few issues you actually selected (and any
   `needs-work` ones whose rework brief you must read):
```bash
# Phase B — hydrate ONLY survivors (selected + needs-work), one by one
gh issue view <N> --repo Lexus2016/hermes-agent-evolution --json body,comments
```
   Apply the same thin-first rule to ANY bulk query in this skill.

1a. **Input freshness — verify before consuming (gate against stale state).**
   If you read any prior-stage artifact (a previous analysis JSON, an
   implementation report, a research/introspection report), check its `date`
   field against that stage's MOST RECENT SCHEDULED SLOT — not against the
   calendar day. An artifact is FRESH if it was produced at or after the
   last slot that stage was supposed to run. Example: introspection runs
   daily at 20:00; when you run at 10:30 today, yesterday's 20:05 report IS
   fresh (today's 20:00 slot hasn't happened yet). Flag `"stale_input": true`
   ONLY when an upstream artifact MISSED its latest scheduled slot — that
   means the stage failed or was gated. In that case work from live GitHub
   data; never silently act on a genuinely outdated selection.
   NOTE: `stale_input` hard-stops the downstream implementation stage —
   a false positive here silently kills the whole day's cycle, so judge
   by SLOTS, not by dates.

2. **Momentum first — `needs-work` AND `next-increment` issues are PRIORITY, not
   rejects.** Two non-terminal labels mark issues already judged worth doing and
   already in motion. SELECT both for implementation (give them priority — they
   have momentum) and read the brief in the issue comments. Do NOT reject either
   as "already exists / already tried" — that throws away wanted, in-progress work:
   - **`needs-work`** — a previous PR failed code review and was sent back with a
     rework brief. Implementation reworks it (or consciously drops it).
   - **`next-increment`** — a previous increment of a multi-phase `roadmap` issue
     was MERGED, and a continuation brief (in the issue comments) lists what
     REMAINS. The capability PARTIALLY exists on `main`; the issue tracks the
     deferred slices. Implementation builds the next coherent slice from current
     `main`. ⚠️ CRITICAL: do NOT let the "already-exists" triage (step 3) reject
     it just because increment-1's code is now present — the brief says what is
     still MISSING, and that remaining work is the entire point. This is the gate
     that keeps multi-phase features advancing instead of stalling at slice 1.
   Only skip such an issue if it is now genuinely harmful or obsolete — then close
   it with a reason AND the `rejected` label (see step 3), don't just ignore it.

3. **Viability triage — REJECT before you rank.** Implementing the wrong thing
   costs far more than skipping it. For EACH remaining open issue (NOT already
   handled as `needs-work` / `next-increment` above), first decide whether it
   should exist at all.

   ⚠️ **PR guard (hard rule).** Triage closes ISSUES only. Every number you
   close MUST come from the Phase-A `gh issue list` output above — that list
   never contains pull requests, but `gh issue close <N>` silently accepts PR
   numbers, so a mis-targeted close destroys the pipeline's own deliverable
   (it happened: PR #708, the green fix for accepted issue #669, was closed
   as its own "duplicate"; see #712). If an OPEN PR already covers an issue,
   leave BOTH open — the issue closes automatically via the PR's `Closes #N`
   when integration merges it; you may add the `accepted` label to the issue.

   REJECT it (do not rank, do not implement) if ANY holds:
   - **Already implemented** — the capability already exists. You MUST check the
     codebase before assuming it's new, e.g.:
     ```bash
     grep -rni "<key term from the issue>" --include=*.py . | head
     ```
     **Evidence rule (hard requirement).** An `already-exists` rejection is
     valid ONLY with executable evidence: the `reason` MUST contain the exact
     file path you verified AND you MUST have confirmed it in THIS session
     with a real command (`ls <path>` / `grep` whose output you saw). If your
     grep/ls returned nothing, the capability does NOT exist — do not reject.
     Never cite a file from memory or plausibility: a fabricated path here
     destructively closes a wanted issue (it happened: #83 was closed citing
     a `scripts/evolution_watchdog.sh` that never existed; see #101).
   - **Out of scope / not needed** — it doesn't serve a real user task or the
     project's purpose; speculative "nice to have" with no concrete need.
   - **Harmful** — it would add risk, heavy dependencies, scope creep, a
     security/compatibility regression, or conflict with existing architecture,
     outweighing its value.
   - **Duplicate** — another open issue already covers it. The cited duplicate
     MUST be a DIFFERENT issue number than the one being closed (an item can
     never be its own duplicate), and MUST be an issue — an open PR covering
     the same work is NOT a duplicate, it is the PR guard case above.

   CLOSE every rejected issue with a clear reason + the canonical `rejected`
   status label, so the backlog shows at a glance what was turned down (the
   *why* is in the closing comment) and the same idea isn't re-proposed:
   ```bash
   # Ensure the status label exists (idempotent), then close + label:
   gh label create rejected --color b60205 \
     --description "Not accepted by evolution — see closing comment" 2>/dev/null || true
   gh issue close <N> --repo Lexus2016/hermes-agent-evolution \
     --comment "Rejected by evolution-analysis: <already-exists|out-of-scope|harmful|duplicate> — <one-line reason>."
   gh issue edit <N> --repo Lexus2016/hermes-agent-evolution \
     --add-label rejected --remove-label needs-work 2>/dev/null || true
   ```
   Only issues that SURVIVE triage proceed to scoring. Be conservative.

4. **Evaluate** each SURVIVING issue against the criteria:

### Impact
- Critical: 1.0 (security, critical bugs)
- High: 0.8 (new features)
- Medium: 0.5 (UX improvements)
- Low: 0.2 (minimal changes)

### Effort
- Trivial: 0.1 (< 1 hour)
- Easy: 0.3 (< 4 hours)
- Medium: 0.5 (< 2 days)
- Hard: 0.8 (< 1 week)
- Very Hard: 1.0 (> 1 week)

### Additional factors
- Community interest: 👍 / 10 (max 1.0)
- Age: days / 30 (max 1.0)
- Compatibility: 1.0 (good) / 0.5 (needs refactoring) / 0.1 (breaks)
- Safety: 0.0 (risky) / 0.5 (needs tests) / 1.0 (safe)
- Complexity (codebase health): **+1.0** if it net-REMOVES / refactors / dedups
  (negative LOC, kills dead code); **0.0** if neutral or a well-tested addition;
  **−1.0** if it grows the codebase a lot with no tests/refactor (entropy). A
  self-modifying agent that only ADDS rots its own base until it can no longer
  understand it — so cleanups outrank equal-impact bloat, and pure-growth changes
  are dampened.

5. **Compute Priority Score**

```python
base_priority = impact * 2 * (1.0 - 0.4 * effort)   # effort DAMPENS (≤40%), never divides
final_priority = base_priority + community*0.1 + age*0.15 + compatibility*0.2 + safety*0.3 + complexity*0.2
```

   **All extra terms are bounded penalties/bonuses, never divisors** (a divisor
   reintroduces the calibration bug — see effort below). `complexity ∈ [−1, 1]`,
   weight 0.2, so it shifts a score by at most ±0.2 — enough to make a refactor
   beat equal-impact bloat, not enough to override real impact.

   **Effort is a bounded penalty, not a divisor.** The old `(impact*2)/effort`
   let a trivial-but-easy issue (impact 0.2, effort 0.1 → **4.0**) outrank a
   critical-but-hard one (impact 1.0, effort 0.8 → **2.5**) — so the agent kept
   picking low-value quick wins (the calibration bug). With the bounded form,
   effort only shaves up to 40% off, so **impact drives the ranking**: that same
   critical issue now scores `1.36` base vs the trivial one's `0.38`.
   Consequence: `base_priority` now ranges 0–2.0 (was up to ~20), so the
   `min_priority 0.7` floor below is a **real filter** on weak/risky issues
   instead of a near-vestigial gate everything cleared.

   `age = min(days_since_created / 30, 1.0)`. The age weight is **0.15** (was
   0.05) so a genuinely-valid issue that keeps losing the nightly contest still
   climbs over time instead of rotting forever.

5a. **Selection-capability calibration — pick only what you can land (goal 3).**
    BEFORE the final selection, read the sidecar
    `~/.hermes/profiles/user1/evolution/evolution-health.txt` (one
    `[evolution-metrics] …` line, refreshed by the funnel job; missing → treat as
    signal OK, proceed). This is the longitudinal calibration loop: it reports
    whether what we SELECTED actually merged. It is INTERNAL plumbing — keep it
    OUT of any delivered report (same rule as the realized-impact signal in 6c).

    The line carries an explicit, pre-computed `effort_budget=X` token. **`X` is
    the metric script's decision, not yours** — it is `1.5` when the window trips
    `LOW_SELECTION_EFFICIENCY`, else `3.0`; those are the ONLY two legal values.
    Set this cycle's `max_total_effort` to **exactly `X`, copied verbatim** — never
    derive your own number, never land on a middle value like `2.0`. If the sidecar
    is missing or has no `effort_budget=` token, default to `3.0`.

    When `X` is `1.5` the pipeline is OVER-selecting — a 12% selection_efficiency
    (e.g. 57 selected, 7 merged over the window) means it picks more work than it
    can finish — so spend that smaller budget on the issues with the HIGHEST
    confidence of landing a merge: prefer lower-`effort`, clearly-scoped issues with
    an unambiguous plan over high-`effort` or fuzzy ones, even when a fuzzy one
    scores marginally higher. Land-confidence includes SIZE: the integration merge
    gate only self-merges PRs of ≤ 200 total changed lines
    (`scripts/evolution_merge_gate.py`), so an issue whose smallest coherent
    increment cannot fit that cap will NOT land autonomously no matter how green
    its CI — prefer issues implementable as a ≤ 200-line diff and route the rest
    through the decomposition gate (6b). The merge funnel, not the score, is the binding
    constraint: choose as much as you can actually MERGE, not as much as scores
    "allow".

    The anti-starvation slot (6a) is still honored under the throttled budget: it
    counts toward `max_total_effort` exactly as before. The scoring formula,
    weights, and the decomposition/split rules are UNCHANGED — this step only sets
    the SIZE of the budget the selection in step 6 spends, never how issues score.

6. **Select** the top 8 for implementation (include any `needs-work` issues from
   step 2):
   - Min priority: 0.7
   - Max total effort: the `effort_budget` value copied verbatim from the sidecar
     in step 5a — **3.0** by default, **1.5** when throttled; never a middle value.
     Stop adding issues once their summed `effort_score` reaches this budget; under
     the throttled 1.5 budget, fill it with the highest-land-confidence issues first.

6a. **Anti-starvation slot — guarantee no valid issue rots for days.** Scoring
    alone lets a sound-but-modest issue lose every single night. To prevent that,
    RESERVE one selection slot for age:
    - From the thin list, find the OLDEST **eligible** open issue — eligible =
      not `rejected`, not currently in-flight `accepted` (an open PR already
      exists for it), age **> 3 days**.
    - If that issue was NOT already picked by score in step 6, **select it anyway**
      — bypass the `min_priority 0.7` floor for THIS one slot (it still counts
      toward `max_total_effort`; if it alone blows the effort budget, pick the
      oldest eligible issue that fits).
    - `needs-work` issues are already prioritized (step 2) and don't need this
      slot. This slot is for issues the scorer keeps passing over.
    - Tag the chosen issue's output entry `"selected_reason": "anti-starvation"`;
      all score-selected entries get `"selected_reason": "score"`. This makes
      starvation rescues visible in the report and in funnel metrics.

6b. **Decomposition gate — long-horizon tasks of ANY level (goal 1).** A
    high-effort issue (`effort > 0.6`: new runtime/subsystem rework, multi-file
    capability) — or ANY issue whose smallest coherent increment would clearly
    exceed the 200-changed-line self-merge cap (see 5a) — must NOT be selected
    for monolithic implementation — that is how
    big work fails at the merge gate, so the agent learns to avoid hard tasks
    (the opposite of "best at any level"). Instead, **decompose it first**: open
    linked child issues, label the parent `needs-split` (the canonical
    decomposition label), reference the parent in each child, and make each
    child a shippable slice with `effort ≤ 0.6`, then let those children compete
    normally. Select children, not the monolith. This is how the agent takes on
    complexity without choking on it.

    After creating child issues, update the parent issue with the decomposition so
    the owner can see the plan at a glance:
    ```bash
    gh label create needs-split --color d4c5f9 \
      --description "Wanted, but exceeds one cycle — needs decomposition" 2>/dev/null || true
    gh issue edit <parent#> --repo Lexus2016/hermes-agent-evolution \
      --add-label needs-split 2>/dev/null || true
    gh issue comment <parent#> --repo Lexus2016/hermes-agent-evolution \
      --body "Decomposed into child issues:\n- #<child1>\n- #<child2>"
    ```
    A `needs-split` parent without child issues is NOT ready for implementation
    and will be skipped by the implementation gate (see evolution-implementation
    step 1c).

6c. **Realized-impact feedback — don't evolve blind (goal 3).** Read the sidecar
    `~/.hermes/profiles/user1/evolution/realized-impact.txt` (one
    `[evolution-realized-impact] …` line, refreshed by the funnel job; missing →
    treat as `healthy`). It closes the loop: it reports whether what we MERGED
    actually delivered value. Let its flags set this cycle's bar, silently (this
    is internal plumbing — keep it OUT of any delivered report):
    - `REALIZED_IMPACT_LOW` (last K merges delivered nothing real) → **shift to
      consolidation**: this cycle prefer refactor/cleanup/reliability + the
      anti-starvation rescue; do NOT select new speculative features; note in the
      report that the pipeline is in consolidation mode pending owner review.
    - `REALIZED_RATE_LOW` (<50% of verified merges helped) → predicted impact is
      over-optimistic; **raise the bar** — demand harder evidence of real impact
      and discount your own impact estimates accordingly.
    - `UNVERIFIED_BACKLOG` → verification isn't running; surface it (watchdog also
      alerts) but proceed.
    - healthy / missing → proceed normally.

## Orchestrator-workers evaluator-optimizer gate (research synthesis) — #230

When a single issue is open-ended enough that one reasoning pass under-serves it
(a `[FEATURE]`/`[IMPROVEMENT]` that asks you to *synthesize a recommendation* or
*compare options*, not a one-line fix), run it as an **orchestrator-workers +
evaluator-optimizer** loop instead of a single shot:

1. **Orchestrate** — fan the sub-task out into N independent worker passes
   (`delegate_task`, the `delegation` toolset is enabled here). Each worker
   returns one candidate result.
2. **Evaluate (deterministic)** — score each candidate against a small rubric
   (`relevance`, `evidence`, `specificity`, `correctness`, each 0..1), then let a
   deterministic gate pick the best and decide whether to accept or refine. Do
   NOT grade your own work with the model and declare victory — that converges to
   a false 10/10 and never stops. Use the gate:

```bash
# candidates.json = [{"scores": {"relevance": 0.8, "evidence": 0.7,
#                                "specificity": 0.6, "correctness": 0.9}}, ...]
# (one object per worker pass; "scores" are YOUR rubric grades of that candidate)
python scripts/evolution_evaluator.py --threshold 0.75 --pass 1 --max-passes 3 candidates.json
```
   It prints one JSON verdict and sets a distinct exit code so a shell loop can
   branch without parsing JSON:
   - `ACCEPT` (exit 0) — best candidate clears the bar; use `best_index`, stop.
   - `OPTIMIZE` (exit 10) — below the bar but passes remain; have the optimizer
     refine the best candidate (feed it the gaps), bump `--pass`, re-run workers,
     re-evaluate.
   - `STOP_BUDGET` (exit 11) — below the bar and the pass budget is spent; take
     the best-so-far and flag it unconverged. **The loop ALWAYS terminates** —
     `OPTIMIZE` is only ever returned while `--pass` < `--max-passes`.
3. **Optimize** — on `OPTIMIZE`, the refinement is the model's job (rewrite the
   best candidate addressing its weakest rubric criteria); the gate only decides
   *whether* to keep looping and *which* candidate is current best.

This is a self-contained first increment of #230: the deterministic referee
(`scripts/evolution_evaluator.py`) that keeps the loop honest and bounded. Deep
pipeline wiring — automatic worker fan-out for every issue, persisting per-pass
candidates to the kanban board — is deferred.

## Status labels — accept/reject visible in the issue list

The evolution pipeline tags every issue with ONE canonical status label so the
owner can see, straight from the GitHub issue list, what happened to each idea
(the *reason* is always in a closing comment — click in to read it):

| Label | Color | Meaning | Set by |
|-------|-------|---------|--------|
| `accepted` | green `0e8a16` | Sent to a PR / implemented | evolution-implementation (when the PR is opened) |
| `rejected` | red `b60205` | Turned down — see closing comment | analysis triage, or implementation final re-check / conscious drop |
| `needs-work` | orange `d93f0b` | A PR was bounced back; rework in progress | evolution-integration (code-review gate) |
| `next-increment` | blue `1d76db` | A roadmap increment merged; more deferred — re-queued | evolution-integration (post-merge, partial increment) |

`accepted` and `rejected` are terminal. `needs-work` and `next-increment` are
TRANSIENT — they cycle back into selection (step 2):
- `needs-work` becomes `accepted` once a reworked PR is opened, or `rejected` if
  implementation drops it.
- `next-increment` becomes `accepted` again when the next increment's PR opens,
  and is finally CLOSED (by integration) when an increment's PR carries `Closes
  #N` — i.e. when the multi-phase feature is fully delivered. Without this label
  a `roadmap` issue stalls forever at slice 1 (`accepted` is terminal, and its
  partial code trips the already-exists triage).
**This skill only ever sets `rejected`** (on triage rejects). Do NOT mark an
issue `accepted` / `next-increment` here — selection is a recommendation; those
states mean the code actually went to / came back from a PR, which only
implementation and integration can confirm.

## Output format

Save to `~/.hermes/profiles/user1/evolution/analysis/YYYY-MM-DD.json`:

```json
{
  "date": "2026-06-08",
  "rejected": [
    {
      "issue_number": 120,
      "reason_code": "already-exists",
      "reason": "Hierarchical cache already implemented in tools/cache.py",
      "closed": true
    }
  ],
  "selected_for_implementation": [
    {
      "issue_number": 123,
      "title": "[FEATURE] Better memory",
      "priority_score": 3.3,
      "impact_score": 0.8,
      "effort_score": 0.5,
      "estimated_hours": 24,
      "selected_reason": "score"
    },
    {
      "issue_number": 84,
      "title": "[IMPROVEMENT] Per-cycle funnel metrics",
      "priority_score": 0.62,
      "impact_score": 0.5,
      "effort_score": 0.3,
      "estimated_hours": 4,
      "selected_reason": "anti-starvation"
    }
  ]
}
```

## Security

Verify `gh auth status` works before proceeding — the gh CLI is the primary
auth mechanism. If gh CLI auth is unavailable AND GITHUB_TOKEN is not set,
**ABORT**. Do NOT export tokens into the environment — `gh` handles auth via
its own stored credentials.
