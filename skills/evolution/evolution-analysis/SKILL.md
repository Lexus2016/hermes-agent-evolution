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

2. **Evaluate** each issue against the criteria:

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

3. **Compute Priority Score**

```python
base_priority = (impact * 2) / effort
final_priority = base_priority + community*0.1 + age*0.05 + compatibility*0.2 + safety*0.3
```

4. **Select** the top 5 for implementation:
   - Min priority: 0.7
   - Max total effort: 2.0

## Output format

Save to `~/.hermes/profiles/user1/evolution/analysis/YYYY-MM-DD.json`:

```json
{
  "date": "2026-06-08",
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
