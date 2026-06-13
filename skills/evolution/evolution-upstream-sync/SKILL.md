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

0. **Read the cursor** — the upstream commit through which we've ACCOUNTED for
   every commit so far. It lives in `.evolution/upstream-sync-state.json`
   (`synced_through_commit`). This is a LOGICAL cursor, NOT git ancestry: we
   cherry-pick (which makes new SHAs), so `git rev-list`/merge-base never reflect
   applied work — the cursor in the file is the source of truth.
```bash
git fetch upstream --tags
CURSOR=$(jq -r '.synced_through_commit // empty' .evolution/upstream-sync-state.json 2>/dev/null)
# Fallback only if the file is missing: the honest contiguous point.
[ -n "$CURSOR" ] || CURSOR=$(git merge-base main upstream/main)
echo "cursor: $CURSOR  | remaining: $(git rev-list --count $CURSOR..upstream/main)"
```

   ⚠️ **NEVER scope by date** (e.g. `gh pr list --search merged:>=<date>`). A date
   window silently drops upstream commits you merely didn't cherry-pick this run —
   the exact bug that left the marker at a run-date with most of the window
   unprocessed. The cursor advances ONLY past commits you actually account for.

1. **Take the next bounded batch AFTER the cursor, oldest-first, and ACCOUNT FOR
   EVERY commit in it** (apply OR defer — never ignore):
```bash
# The driver is the commit list (catches everything, incl. PR-less commits).
git log --reverse --format='%H %s' "$CURSOR"..upstream/main | head -25
```
   For richer relevance judgement, look up the PR a commit belongs to (title /
   labels / description):
```bash
gh pr list --repo nousresearch/hermes-agent --state merged --search "<sha>" \
  --json number,title,labels,url
```
   For EACH commit in the batch, decide and record one of:
   - **already-applied** — its change is already in our main (a previous
     cherry-pick). Detect via an empty/redundant cherry-pick and skip:
     `git cherry-pick -x <sha>` → if it reports "nothing to commit"/empty, run
     `git cherry-pick --skip`. Counts as accounted.
   - **apply** — relevant + conflict-free → `git cherry-pick -x <sha>`.
   - **defer** — conflicting, irrelevant, or `.github/workflows/**` without scope
     → do NOT apply; append `{sha, summary, reason}` to `deferred[]` in the state
     file. Counts as accounted (recorded, not lost; owner can revisit).

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

## Advance the cursor + inherit version (do this IN the sync PR)

After accounting for the batch, advance the cursor and stamp the version. This
is the step that makes the sync CONVERGE — the cursor moves forward by exactly
the batch you processed, every run, so the backlog drains deterministically.

```bash
# LAST commit of the batch you just processed (the 25th, or fewer at the end).
# This is the new cursor — you accounted for everything up to and including it.
LAST=$(git log --reverse --format='%H' "$CURSOR"..upstream/main | head -25 | tail -1)

# Newest upstream release tag that is an ancestor of the NEW cursor (NOT of
# upstream's tip). Tags advance only when the cursor actually crosses one:
TAG=$(for t in $(git tag -l 'v20*' | sort -Vr); do \
        git merge-base --is-ancestor "$t" "$LAST" 2>/dev/null && { echo "$t"; break; }; \
      done)
DATE=$(echo "$TAG" | sed 's/^v//')                          # e.g. 2026.6.5
```

1. **Rewrite `.evolution/upstream-sync-state.json`** with the advanced cursor,
   the tag/date for that cursor, and append any commits you deferred this run:
```json
{
  "synced_through_commit": "<LAST>",
  "synced_through_tag": "<TAG>",
  "synced_through_date": "<DATE as YYYY-MM-DD>",
  "our_version": "0.16.0",
  "synced_at": "<run date>",
  "deferred": [ {"sha": "...", "summary": "...", "reason": "conflict|irrelevant|workflow-scope"} ]
}
```
2. **Only if `TAG` changed** from the previous marker, update `hermes_cli/__init__.py`
   `__release_date__ = "<DATE>"` (the banner renders `Hermes Agent v<__version__>
   (<__release_date__>)`). If the batch was post-release untagged work, `TAG` is
   unchanged — leave `__release_date__` as-is, just advance the cursor.

Commit the state file (and `__init__.py` if changed) on the `sync/upstream-*`
branch so they ride the same PR + CI as the cherry-picked code. The cursor only
moves forward when this PR's commits are real — so a failed/empty run leaves the
cursor untouched and the next run retries the same batch.

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
