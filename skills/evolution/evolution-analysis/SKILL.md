---
name: evolution-analysis
description: Analyze issues and PRs to prioritize implementation (PRIVATE mode only)
version: 1.0.0
author: Hermes Evolution
category: evolution
mode: PRIVATE
---

# Evolution Analysis Skill

**Operating mode:** PRIVATE (repository owner only)

## Task

Analyze all created issues and PRs, and determine priority for implementation.

## Process

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

2. **Rework first — `needs-work` issues are PRIORITY, not rejects.** An issue
   labelled `needs-work` was ALREADY judged worth doing and attempted; a PR
   failed code review and was sent back with a rework brief (in the issue
   comments). Do NOT reject it as "already exists / already tried" — that throws
   away a wanted idea. Instead SELECT it for implementation (give it priority, it
   has momentum) so implementation can read the brief and finish it properly (or
   consciously drop it). Only skip a `needs-work` issue if it is now genuinely
   harmful or obsolete — and then close it with a reason AND the `rejected` label
   (see step 3), don't just ignore it.

3. **Viability triage — REJECT before you rank.** Implementing the wrong thing
   costs far more than skipping it. For EACH remaining open issue (NOT already
   handled as `needs-work` above), first decide whether it should exist at all.
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
   - **Duplicate** — another open issue already covers it.

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

5. **Compute Priority Score**

```python
base_priority = impact * 2 * (1.0 - 0.4 * effort)   # effort DAMPENS (≤40%), never divides
final_priority = base_priority + community*0.1 + age*0.15 + compatibility*0.2 + safety*0.3
```

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

6. **Select** the top 8 for implementation (include any `needs-work` issues from
   step 2):
   - Min priority: 0.7
   - Max total effort: 3.0

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

## Status labels — accept/reject visible in the issue list

The evolution pipeline tags every issue with ONE canonical status label so the
owner can see, straight from the GitHub issue list, what happened to each idea
(the *reason* is always in a closing comment — click in to read it):

| Label | Color | Meaning | Set by |
|-------|-------|---------|--------|
| `accepted` | green `0e8a16` | Sent to a PR / implemented | evolution-implementation (when the PR is opened) |
| `rejected` | red `b60205` | Turned down — see closing comment | analysis triage, or implementation final re-check / conscious drop |
| `needs-work` | orange `d93f0b` | A PR was bounced back; rework in progress | evolution-integration (code-review gate) |

`accepted` and `rejected` are terminal. `needs-work` is transient: it becomes
`accepted` once a reworked PR is opened, or `rejected` if implementation drops
it. **This skill only ever sets `rejected`** (on triage rejects). Do NOT mark an
issue `accepted` here — selection is a recommendation; `accepted` means the code
actually went to a PR, which only implementation can confirm.

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

If GITHUB_PRIVATE_TOKEN is not set — **ABORT**. This skill only works in PRIVATE mode.
