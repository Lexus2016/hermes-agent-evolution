---
name: evolution-upstream-sync
description: Sync with upstream Hermes Agent and integrate relevant changes
version: 1.0.0
author: Hermes Evolution
category: evolution
mode: PRIVATE
---

# Evolution Upstream Sync Skill

**Operating mode:** PRIVATE (repository owner only)

## Task

Synchronize with the original Hermes Agent (upstream) and determine which changes should be integrated.

## Process

1. **Fetch changes from upstream:**

```bash
git fetch upstream
git log main..upstream/main --oneline
```

2. **Analyze the changes:**

Change categories:
- **Bug fixes** — critical fixes, should be integrated
- **Security fixes** — security fixes, mandatory
- **Performance improvements** — performance improvements
- **New features** — new features of the original Hermes
- **Refactoring** — refactoring, may conflict with our changes
- **Documentation** — documentation updates
- **Tests** — test updates

3. **Evaluate each change:**

### Impact on evolution changes
- **Conflicts** — conflicts with our modifications → needs manual merge
- **Compatible** — compatible → can be merged automatically
- **Enhances** — improves our changes → priority

### Integration priority
1. **Critical**: Security, bug fixes (must have)
2. **High**: Performance, critical features (should have)
3. **Medium**: New features (nice to have)
4. **Low**: Documentation, tests (optional)

4. **Create proposals:**

For each relevant change, create an issue:

```markdown
# [UPSTREAM] Integrate upstream fix: description

## Upstream Change
- Commit: abc123
- Author: original author
- PR: link to upstream PR

## Description
What changed in upstream...

## Impact on Evolution
- Conflicts: Yes/No
- Enhances evolution: Yes/No
- Breaking: Yes/No

## Recommendation
- [ ] Auto-merge (if compatible)
- [ ] Manual merge (if conflicts)
- [ ] Skip (if not relevant)

## Implementation Plan
1. Cherry-pick commit
2. Resolve conflicts
3. Test evolution features
4. Update docs
```

## Sync frequency

Recommended:
- **Weekly** — full sync and analysis
- **After critical updates** — if there are critical fixes in upstream

## Security

1. **Always work in a separate branch:**
```bash
git checkout -b sync/upstream-YYYY-MM-DD
```

2. **Test after merge:**
- Make sure evolution features work
- Run the tests

3. **Rollback if something broke:**
```bash
git revert -m 1 <merge-commit>
```

## Merge strategy — ONLY via PR (safety gate)

⛔ Do NOT merge upstream directly into `main`. Like `evolution-implementation`,
upstream changes go **through a separate branch + PR + CI** — NOT a direct merge:

```bash
# 0. `gh` is authorized via persistent `gh auth login` (~/.config/gh) from
#    setup-hermes.sh. Do NOT export GH_TOKEN (Hermes strips it from the agent
#    terminal). Just route git auth through gh:
gh auth setup-git

# 1. Separate branch from the current main:
git checkout main && git pull && git checkout -b sync/upstream-YYYY-MM-DD

# 2. Bring over only the NEEDED commits:
git cherry-pick <commit-hash>          # for compatible changes
# or for conflicting ones:
git merge upstream/main --no-commit    # resolve conflicts, then: git commit

# 3. Create a PR (do NOT merge into main manually):
git push origin sync/upstream-YYYY-MM-DD
gh pr create --base main --head sync/upstream-YYYY-MM-DD \
  --title "[UPSTREAM] Sync: <summary>" \
  --body "Cherry-picked relevant upstream changes. See upstream sync report."
```

Merging into `main` happens only after green CI (`tests.yml`/`lint.yml`) and with branch
protection. Changes in critical paths (`.github/CODEOWNERS`) require owner
review. This is the same gate that protects the entire self-evolution — upstream code is also
untrusted until it has passed CI + review.

## Output format

Save the report to `~/.hermes/profiles/user1/evolution/upstream/YYYY-MM-DD.md`:

```markdown
# Upstream Sync Report - YYYY-MM-DD

## Summary
- Total commits: 42
- Relevant changes: 8
- Conflicts: 2
- Auto-merge candidates: 5

## Relevant Changes

### [CRITICAL] Security fix in auth
- Commit: def456
- Conflicts: No
- Action: Auto-merge

### [FEATURE] New tool integration
- Commit: ghi789
- Conflicts: Yes (with evolution/tools)
- Action: Manual merge

## Implementation Plan
1. Cherry-pick def456 (auto)
2. Manual merge ghi789
...
```

## Limits

- No more than 10 upstream commits at a time
- Critical changes — priority
- Breaking changes — always manual review
