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

1. **Retrieve** all open issues via the `gh` CLI (terminal tool). `gh` is
   authorized via persistent `gh auth login` (~/.config/gh), set up by
   setup-hermes.sh — do NOT export GH_TOKEN from env (Hermes strips GitHub
   tokens from the agent terminal). Just call gh directly:

```bash
gh issue list --repo Lexus2016/hermes-agent-evolution --state open \
  --limit 50 --json number,title,body,labels,createdAt
```

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
base_priority = (impact * 2) / effort
final_priority = base_priority + community*0.1 + age*0.05 + compatibility*0.2 + safety*0.3
```

6. **Select** the top 5 for implementation (include any `needs-work` issues from
   step 2):
   - Min priority: 0.7
   - Max total effort: 2.0

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
      "estimated_hours": 24
    }
  ]
}
```

## Security

If GITHUB_PRIVATE_TOKEN is not set — **ABORT**. This skill only works in PRIVATE mode.
