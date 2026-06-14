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

0. **Baseline + accounting model.** State lives in
   `.evolution/upstream-sync-state.json`. `synced_through_date` is the evaluation
   horizon: every upstream PR merged on/before it has been ACCOUNTED for — ported,
   deferred-with-reason (`deferred[]`), or skipped as permanently-irrelevant
   (bulk count by scope). We do NOT chase a contiguous commit cursor or parity
   (upstream is a firehose that diverges heavily from us — parity is impossible
   and undesirable); we track which RELEVANT PRs are handled.
```bash
git fetch upstream --tags
SINCE=$(jq -r '.synced_through_date // empty' .evolution/upstream-sync-state.json 2>/dev/null)
[ -n "$SINCE" ] || SINCE=2026-06-05
echo "evaluating upstream PRs merged >= $SINCE"
```

   ⚠️ **Honesty invariant (from the past dishonesty bug):** scoping by merge date
   is fine ONLY because every PR in the window is explicitly accounted for. The
   old bug claimed "synced through date X" while conflicting commits in that
   window were silently dropped, unrecorded. NEVER silently drop a RELEVANT PR:
   relevant + un-applied → `deferred[]` with a reason (revisit next run);
   irrelevant → bulk-skip by scope (recorded in the report). `synced_through_date`
   advances only once every RELEVANT PR through it is ported or deferred.

1. **Select by PRIORITY across the WHOLE backlog — NOT oldest-first.** Upstream is
   a firehose (~800 commits/week) that diverges heavily from our fork; chasing
   commit-for-commit parity is impossible AND undesirable — most of it is
   desktop / dashboard / tui / unused-platform / cosmetic churn we never want.
   The goal is: **never miss a RELEVANT change, promptly.** Walking oldest-first
   from a cursor is WRONG — it buries new security fixes behind hundreds of
   irrelevant commits the cursor may never reach. Instead:

   a. **List all merged upstream PRs since our baseline** (`synced_through_date`):
```bash
gh pr list --repo nousresearch/hermes-agent --state merged \
  --search "merged:>=<synced_through_date>" --limit 300 \
  --json number,title,labels,mergedAt,mergeCommit
```
   b. **Classify each by RELEVANCE to THIS fork** — headless, server-side,
      self-evolving; we run agent/cron/gateway/skills/mcp and deliver to Telegram;
      we have NO desktop/dashboard/tui and do NOT use discord/whatsapp/matrix/
      weixin/feishu/etc.:
      - **PORT** — security/safety (mandatory); bug/perf fixes in `agent`,
        `gateway`, `cron`, `skills`, `mcp`, `update`, `terminal`, `model`/
        providers we use, and `telegram`.
      - **SKIP permanently** — `desktop`, `dashboard`, `tui`, unused messaging
        platforms, pure `docs`/`style`. Record as a bulk count + scopes in the
        report; do NOT process commit-by-commit and NEVER let them block ports.
   c. **Port in priority order, security FIRST and UNCAPPED:**
      1. **ALL** security/safety PRs — **no per-run limit** (mandatory).
      2. core bug/perf (agent/gateway/cron/skills/mcp/update/terminal).
      3. provider/model + telegram.
      For each PR's commit(s): `git cherry-pick -x <sha>`. Empty/redundant
      (already applied) → `git cherry-pick --skip`. Conflict → append
      `{sha, pr, summary, reason}` to `deferred[]` and MOVE ON — a conflict on one
      must never block the rest. `max_commits_per_sync` bounds tiers 2-3 per run
      (keeps one run's diff reviewable + conflict load sane); **tier-1 security is
      exempt and always fully ported.**
   d. **State = ported/deferred SETS, not a contiguous cursor.** Record ported PR
      numbers + `deferred[]`; advance `synced_through_date` to the newest PR
      `mergedAt` you evaluated. We do NOT claim contiguous commit parity (the
      firehose makes that meaningless) — we claim "every RELEVANT PR through date
      X is ported or deferred-with-reason".

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
unreviewable PR; it happened once and could not even be pushed). Always
cherry-pick selected commits via a branch + PR.

**Select by PRIORITY, not oldest-first (see step 1).** Upstream's volume makes
commit-for-commit parity impossible and pointless; we port the RELEVANT subset.
**Tier-1 security is UNCAPPED** — port every security/safety fix every run, no
limit. `max_commits_per_sync` bounds only tiers 2-3 (core/provider/telegram) per
run, to keep one PR reviewable and conflict-load sane; the rest of the relevant
backlog continues next run. Irrelevant scopes (desktop/dashboard/tui/unused
platforms) are skipped permanently, never queued. "Behind on cosmetic churn" is
fine and expected; "behind on a security fix" is not.

⛔ Do NOT merge upstream directly into `main`. Like `evolution-implementation`,
upstream changes go **through a separate branch + PR + CI** — NEVER a direct merge:

```bash
# 0. `gh` is authorized via persistent `gh auth login` (~/.config/gh) from
#    setup-hermes.sh. Do NOT export GH_TOKEN (Hermes strips it from the agent
#    terminal). Just route git auth through gh:
gh auth setup-git

# 1. Separate branch from the current main:
git checkout main && git pull && git checkout -b sync/upstream-YYYY-MM-DD

# 2. Pick by PRIORITY (security first, then core/provider/telegram) — cherry-pick
#    ONLY, never a bare `git merge upstream/main`. Map each relevant PR (step 1)
#    to its merge commit(s) and cherry-pick those:
gh pr view <pr> --repo nousresearch/hermes-agent --json mergeCommit -q .mergeCommit.oid
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
  --title "[UPSTREAM] Sync: <N> relevant commits (priority-first, through <date>)" \
  --body "Cherry-picked selected relevant upstream changes (priority-first: security, then core/provider/telegram). See upstream sync report."
```

Merging into `main` happens only after green CI (`tests.yml`/`lint.yml`) and with branch
protection. Changes in critical paths (`.github/CODEOWNERS`) require owner
review. This is the same gate that protects the entire self-evolution — upstream code is also
untrusted until it has passed CI + review. A `sync/*` branch is NOT
`evolution/issue-*`, so evolution-integration never auto-merges it — upstream
PRs always wait for the owner.

## Advance the horizon + inherit version (do this IN the sync PR)

After every RELEVANT PR in the window is accounted for (ported / deferred /
bulk-skipped), advance the date horizon and stamp the version.

```bash
# New horizon = the newest mergedAt among the PRs you evaluated this run.
SINCE_NEW=<newest evaluated PR mergedAt, YYYY-MM-DD>
# Newest upstream release tag merged on/before the new horizon (for the banner):
TAG=$(for t in $(git tag -l 'v20*' | sort -Vr); do \
        td=$(git log -1 --format=%cs "$t" 2>/dev/null); \
        case "$td" in [0-9]*) [ "$td" \> "$SINCE_NEW" ] || { echo "$t"; break; };; esac; \
      done)
DATE=$(echo "$TAG" | sed 's/^v//')                          # e.g. 2026.6.5
```

1. **Rewrite `.evolution/upstream-sync-state.json`** — date horizon + the
   ported/deferred/skipped accounting (NOT a contiguous commit cursor):
```json
{
  "synced_through_date": "<SINCE_NEW as YYYY-MM-DD>",
  "synced_through_tag": "<TAG>",
  "ported_prs": [<upstream PR numbers ported, cumulative>],
  "our_version": "0.16.0",
  "synced_at": "<run date>",
  "deferred": [ {"pr": 0, "sha": "...", "summary": "...", "reason": "conflict|workflow-scope"} ],
  "skipped_scopes": {"desktop": 0, "dashboard": 0, "tui": 0, "<unused-platform>": 0}
}
```
   The honesty invariant holds: every RELEVANT PR ≤ `synced_through_date` is in
   `ported_prs` or `deferred[]`; irrelevant ones are tallied in `skipped_scopes`.
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
