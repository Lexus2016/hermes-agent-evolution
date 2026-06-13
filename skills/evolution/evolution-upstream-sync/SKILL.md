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

0. **Read where we last synced through** (the baseline). This is recorded in
   `.evolution/upstream-sync-state.json` at the repo root (created/updated in the
   version-stamp step below). Use its `synced_through_date` to scope the PR query:
```bash
SYNCED_DATE=$(jq -r '.synced_through_date // "2026-01-01"' \
  .evolution/upstream-sync-state.json 2>/dev/null || echo "2026-01-01")
```

1. **Fetch upstream and analyze at the PULL-REQUEST level (preferred), with the
   commit log as a fallback.** A merged upstream PR carries a title, description
   and labels — far richer grounds to judge relevance than an opaque commit:
```bash
git fetch upstream --tags
# PRIMARY: upstream merged PRs since our baseline (richest context):
gh pr list --repo nousresearch/hermes-agent --state merged --limit 50 \
  --search "merged:>=$SYNCED_DATE" \
  --json number,title,mergedAt,labels,url,mergeCommit
# FALLBACK: raw commits not in our main (catches direct-push / PR-less commits):
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

- **Mon / Wed / Fri** — full sync and analysis (the cron schedule). At up to 25
  commits/run this closes a large backlog (e.g. 300+ commits behind) in weeks,
  not months, then keeps pace.
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

## Merge strategy — bounded, cherry-pick, ONLY via PR (safety gate)

⛔ **HARD RULE — NEVER wholesale-merge.** Do NOT run `git merge upstream/main`
(it pulls the ENTIRE backlog — hundreds of commits / 600+ files — into one
unreviewable PR; it happened once and could not even be pushed). No matter how
far behind we are, a single run integrates **at most `max_commits_per_run` (25)**
commits, the OLDEST first (closest to our baseline), via cherry-pick. The rest
wait for the next run — the Mon/Wed/Fri cadence drains the backlog over a few
weeks. Falling behind is fine; an enormous merge is not.

⛔ Do NOT merge upstream directly into `main`. Like `evolution-implementation`,
upstream changes go **through a separate branch + PR + CI** — NEVER a direct merge:

```bash
# 0. `gh` is authorized via persistent `gh auth login` (~/.config/gh) from
#    setup-hermes.sh. Do NOT export GH_TOKEN (Hermes strips it from the agent
#    terminal). Just route git auth through gh:
gh auth setup-git

# 1. Separate branch from the current main:
git checkout main && git pull && git checkout -b sync/upstream-YYYY-MM-DD

# 2. Pick the OLDEST <=25 relevant commits — cherry-pick ONLY, never a bare
#    `git merge upstream/main`. Oldest-first keeps history linear and conflicts small:
git log --reverse --oneline main..upstream/main | head -25   # candidates, oldest first
git cherry-pick <hash>                 # one commit (or a contiguous range) at a time
# On conflict: resolve THAT commit (keep our evolution changes), `git add`,
# `git cherry-pick --continue`. If a commit is too entangled to resolve cleanly,
# `git cherry-pick --skip` and note it in the report for a future run — do NOT
# fall back to a full merge to "get everything".

# 2a. WORKFLOW FILES: pushing a branch that edits `.github/workflows/**` needs the
#     `workflow` token scope. If a picked commit touches workflows and the push is
#     rejected for missing scope, drop those files from this sync
#     (`git checkout HEAD~ -- .github/workflows && git commit --amend`) and flag
#     them for an owner-gated follow-up — do not fail the whole sync.

# 3. Create a PR (do NOT merge into main manually):
git push origin sync/upstream-YYYY-MM-DD
gh pr create --base main --head sync/upstream-YYYY-MM-DD \
  --title "[UPSTREAM] Sync: <N> commits (<from>..<to>)" \
  --body "Cherry-picked <=25 oldest relevant upstream changes. See upstream sync report."
```

Merging into `main` happens only after green CI (`tests.yml`/`lint.yml`) and with branch
protection. Changes in critical paths (`.github/CODEOWNERS`) require owner
review. This is the same gate that protects the entire self-evolution — upstream code is also
untrusted until it has passed CI + review. A `sync/*` branch is NOT
`evolution/issue-*`, so evolution-integration never auto-merges it — upstream
PRs always wait for the owner.

## Inherit upstream version numbering (do this IN the sync PR)

Upstream releases are tagged by calendar date (`vYYYY.M.D`, e.g. `v2026.6.5`),
roughly weekly; their `pyproject.toml` version is static. So the meaningful
"version" to inherit is **the newest upstream release tag your synced commits
reach**. Stamp it as PART of the sync PR (same branch, so CI covers it):

```bash
# Newest upstream release tag that is actually an ancestor of THIS sync branch
# (i.e. reached by the commits you just cherry-picked/merged) — NOT upstream's
# tip, which may be tags ahead of the ≤25 commits you took this run:
git fetch upstream --tags
TAG=$(for t in $(git tag -l 'v20*' | sort -Vr); do \
        git merge-base --is-ancestor "$t" HEAD 2>/dev/null && { echo "$t"; break; }; \
      done)
COMMIT=$(git rev-parse HEAD)
DATE=$(echo "$TAG" | sed 's/^v//')                          # e.g. 2026.6.5
# If TAG is unchanged from the current marker, the synced commits are post-release
# (untagged) work — keep the existing tag/date, just bump synced_through_commit.
```

1. Update `hermes_cli/__init__.py` → set `__release_date__ = "<DATE>"` (the banner
   already renders `Hermes Agent v<__version__> (<__release_date__>)`, so the
   correspondence becomes visible immediately). Leave `__version__` (our own
   semver) as-is.
2. Write the baseline marker `.evolution/upstream-sync-state.json`:
```json
{
  "synced_through_tag": "v2026.6.5",
  "synced_through_commit": "<full sha>",
  "synced_through_date": "2026-06-05",
  "our_version": "0.16.0",
  "synced_at": "<run date>"
}
```
   `synced_through_date` (ISO `YYYY-MM-DD`, derived from the tag) is what step 0
   reads next run to scope the PR query — so each run only looks at NEW upstream
   work. Commit both files on the `sync/upstream-*` branch so they ride the same
   PR + CI as the code.

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

- No more than 25 upstream commits per run
- Critical changes — priority
- Breaking changes — always manual review
