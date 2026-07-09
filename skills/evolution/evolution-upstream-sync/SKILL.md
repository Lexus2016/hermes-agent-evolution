---
name: evolution-upstream-sync
description: Keep the fork current with upstream Hermes Agent by merging its published RELEASE tags (not bleeding-edge main)
version: 3.0.0
author: Hermes Evolution
category: evolution
mode: PRIVATE
---

# Evolution Upstream Sync Skill

**Operating mode:** PRIVATE (repository owner only)

## Task

Keep this fork current with the original Hermes Agent (upstream) by merging its
**published RELEASE tags** — our fork is the base (our evolution work) and we roll
each stabilized upstream release into it. We want everything a release contains
(desktop `apps/desktop`, TUI `ui-tui`, gateway, web, plugins, every platform).
Nothing in a release is skipped; nothing of ours is lost.

> **Model change (v3.0.0 — 2026-07-01):** we track upstream **releases**, NOT
> `upstream/main`. Upstream lands ~300 commits/day; chasing every commit made the
> daily wholesale `git merge upstream/main` exceed the escalation ceiling and
> stall — the fork drifted hundreds of commits behind and every run escalated
> instead of merging. Upstream cuts a tagged release ~weekly/biweekly (the
> `v2026.M.D` tags = GitHub releases); we merge to the LATEST such tag. This is a
> bounded, stabilized batch instead of an unwinnable daily churn chase. The old
> `git merge upstream/main` model (v2.0.0) is OBSOLETE. Security/critical fixes
> upstream ships as patch releases (e.g. `v2026.5.29.2`) are picked up the same
> way; anything more urgent than the next release goes through the curated
> critical-backport lane (see issue tracker), never an unattended main merge.

## Process

### 0. Baseline — find the latest upstream RELEASE and measure the gap

```bash
gh auth setup-git                      # route git auth through gh (no GH_TOKEN export)
# Guard against a SHALLOW runtime clone (#823): a shallow clone carries an
# artificial root commit with NO common ancestor to upstream release tags, so the
# later `git merge <release>` fails with "refusing to merge unrelated histories"
# and every sync escalates. Deepen to full history BEFORE any fetch/merge so
# merge-base (and the BEHIND/AHEAD counts below) resolve correctly. Idempotent —
# a no-op once the clone is full.
[ -f .git/shallow ] && { echo "[upstream-sync] shallow clone — unshallowing (#823)"; git fetch origin --unshallow --quiet || true; }
git fetch upstream --tags --quiet && git fetch origin --quiet
git checkout main && git pull --ff-only origin main
# The latest PUBLISHED upstream release (the v2026.M.D tags ARE the GitHub releases).
LR=$(gh release view --repo nousresearch/hermes-agent --json tagName --jq .tagName)
git fetch upstream "refs/tags/$LR:refs/tags/$LR" --quiet 2>/dev/null || true
BEHIND=$(git rev-list --count "main..$LR")   # release commits not yet in the fork
AHEAD=$(git rev-list --count "$LR..main")    # our evolution work + newer merges
echo "latest release: $LR | behind release: $BEHIND | our commits ahead: $AHEAD"
```

If `BEHIND == 0` → the fork already contains the latest release; nothing to do,
write a one-line report and stop. This is the NORMAL steady state on most days (a
release lands only ~weekly/biweekly). Do **NOT** merge `upstream/main`: being
behind bleeding-edge main is expected under release-tracking and is not a backlog.

### 1. Size the merge — decide autonomous vs escalate

```bash
# Enable the `theirs` merge driver that .gitattributes maps generated upstream
# artifacts to (e.g. website/static/api/model-catalog.json, whose `updated_at`
# timestamp collides every single sync). Without this the driver name is unknown
# and git falls back to a normal conflict — which, on an hermes_cli-adjacent
# file, would force escalation. Idempotent:
git config merge.theirs.driver 'cp -f "%B" "%A"'
git merge --no-ff --no-commit "$LR" || true   # stage the RELEASE merge, surface conflicts
CONFLICTS=$(git diff --name-only --diff-filter=U | wc -l)
echo "conflicted files: $CONFLICTS"
git diff --name-only --diff-filter=U
```

- **`CONFLICTS` ≤ 10, none in core persistence/agent runtime** → resolve
  autonomously (step 2) and open a normal PR. A single release is a bounded batch,
  so `BEHIND` is naturally small; the `escalate_if_commits_over` guard still applies.
- **`BEHIND` unusually large (a multi-release catch-up) OR `CONFLICTS` > 10 OR any
  conflict in `run_agent.py` / `agent/` / `cron/scheduler.py` / `hermes_cli/`
  persistence** → this needs owner judgement. `git merge --abort`, then open a
  **draft** PR / file an issue describing the release + conflict surface and STOP.
  Do NOT blind-resolve a judgement-heavy merge autonomously — that is how features
  get silently dropped.

### 2. Resolve conflicts — authorship-driven, keep OURS, follow upstream

For EACH conflicted file, decide by **who authored each side**, not by guesswork.
The discriminator (run per ambiguous symbol/hunk):

```bash
# Which commit introduced this code, and is it upstream's or ours?
C=$(git log -S '<distinctive string>' --format='%H' HEAD -- <file> | tail -1)
git merge-base --is-ancestor "$C" upstream/main \
  && echo "UPSTREAM-authored" || echo "OURS"
```

Resolution rules:
- **Upstream-domain files we don't customize** (`apps/desktop/**`, `ui-tui/**`,
  `web/**`, platforms we don't run): take upstream — `git checkout --theirs <f>`.
  These must always be current.
- **Trivial conflicts are NOT a reason to escalate** — auto-resolve by taking
  upstream: generated/published artifacts (e.g. `website/static/api/model-catalog.json`
  — handled by the merge driver above so it shouldn't even appear), pure
  whitespace/alignment differences, and timestamp-only hunks. Only the COUNT of
  SUBSTANTIVE conflicts (real logic / our features at stake) gates escalation.
- **Our evolution additions** (telemetry, dotenv-secrets, reasoning-strip,
  docs-only CI, evolution skills/cron/scripts): keep ours — but they are usually
  ADDITIVE (new files / new lines) and rarely conflict. When they do, keep both
  sides (our addition + upstream's change).
- **Upstream feature that upstream itself REVERTED** (the code is in the
  merge-base + in our HEAD, but `git show upstream/main:<file>` no longer has it):
  FOLLOW the revert — drop it. It is NOT our feature; we forked before the revert.
  (Example: per-job cron profile, added by upstream then reverted in #43956 — we
  correctly dropped it.)
- **Our fix vs upstream's fix for the same bug**: prefer upstream's current
  approach unless ours is demonstrably more correct AND has a test proving it.
  If we keep ours, the divergence must be deliberate and documented.
- **Generated artifacts** (`website/static/api/model-catalog.json`): take upstream
  then regenerate from our source (`python scripts/build_model_catalog.py`).
- **`uv.lock`**: after resolving `pyproject.toml`, run `uv lock` so it matches; CI
  runs `uv lock --locked`.

⛔ **NEVER COMMIT CONFLICT MARKERS.** After resolving, the worktree MUST be
marker-free. Match ONLY the unambiguous sentinels `<<<<<<<` / `>>>>>>>` (a real
conflict always has both); do NOT match bare `=======` (it false-positives on
legitimate dividers). This guard MUST print nothing before you commit:

```bash
git grep -lnE '^(<<<<<<<|>>>>>>>)' 2>/dev/null
```

A committed `>>>>>>>` is invalid code (it broke a sync PR once — 99 syntax errors).
If any marker remains, you have NOT resolved cleanly — fix it before committing.

### 3. Detect silent drops (a wholesale merge CAN drop our code)

A 3-way merge silently FOLLOWS upstream's deletion of a base feature when our
side didn't modify it — usually correct (upstream reverts), but verify nothing of
ours that is genuinely additive vanished. Our purely-new files cannot be dropped
(upstream's diff never mentions them), so focus on files we edited that upstream
also changed:

```bash
# functions/classes present in our HEAD but missing from the merged tree:
for f in run_agent.py cron/scheduler.py cron/jobs.py hermes_cli/*.py agent/*.py tools/*.py; do
  miss=$(comm -23 \
    <(git show HEAD:"$f" 2>/dev/null | grep -oE '^(def|class|    def) [a-zA-Z_]+' | sort -u) \
    <(grep -oE '^(def|class|    def) [a-zA-Z_]+' "$f" 2>/dev/null | sort -u))
  [ -n "$miss" ] && echo "### $f drops:" && echo "$miss"
done
```
For each flagged symbol: run the authorship check (step 2). OUR symbol missing →
restore it. Upstream symbol missing because upstream reverted → leave it dropped.
Files we never touched flagged here are upstream refactors — ignore.

### 4. Verify

```bash
python3 -m compileall -q cron hermes_cli agent tools scripts *.py
uv run --extra dev python -m pytest tests/cron tests/run_agent tests/tools tests/hermes_cli -q --timeout=90
```
Evolution features must still pass (telemetry, dotenv-secrets, flush, docs-only CI,
skills sync). CI's 6-shard suite is the full gate.

### 5. Commit + PR (never a direct merge into `main`)

```bash
git checkout -b sync/upstream-release-$LR
git commit -m "Merge upstream release $LR into fork — sync (<BEHIND> commits)"   # the staged merge
git push origin sync/upstream-release-$LR
gh pr create --base main --head sync/upstream-release-$LR \
  --title "[UPSTREAM] Sync upstream release $LR (<BEHIND> commits)" \
  --body "git merge $LR (latest upstream release). Conflicts resolved authorship-first (keep ours, follow upstream incl. reverts). See sync report."
```

- Merge into `main` only after **green CI** (`tests.yml` 6 shards + `lint.yml` +
  `typecheck`) and owner review. Use `gh pr merge <n> --merge` (NOT squash/rebase —
  preserve upstream's history + our commits). A `sync/*` branch is NOT
  `evolution/issue-*`, so evolution-integration never auto-merges it.
- **Attribution:** a wholesale merge brings new upstream contributors. If
  `check-attribution` fails, map each unmapped email in `scripts/release.py`
  `AUTHOR_MAP` (resolve the username via the PR author:
  `gh pr view <PR> --repo nousresearch/hermes-agent --json author -q .author.login`,
  or `gh api repos/nousresearch/hermes-agent/commits/<sha> --jq .author.login`).
- **Workflow scope:** pushing a branch that edits `.github/workflows/**` needs the
  `workflow` token scope. If the push is rejected, the merge legitimately updated
  workflows — flag for an owner-gated push rather than dropping them.

### 6. Inherit the upstream version marker (in the sync PR)

After merging, stamp the banner from the release we just merged (it is now the
newest upstream release tag reachable from `main`):

```bash
TAG=$LR                                    # the release tag we just merged
DATE=$(echo "$TAG" | sed 's/^v//')        # e.g. 2026.6.19
```
If `TAG` advanced, update `hermes_cli/__init__.py` `__release_date__ = "<DATE>"`
(the banner renders `Hermes Agent v<__version__> (<__release_date__>)`). Commit on
the `sync/*` branch so it rides the same PR + CI.

## Sync frequency

Checked daily (`0 8 * * *`, before research at 09:00), but a run only MERGES when
upstream has published a new release since the last sync — most days it finds
`BEHIND == 0` and stops silently. Because a release is a bounded, upstream-
stabilized batch, each real merge is small and almost always conflict-free.
Upstream ships security/critical fixes as patch releases (e.g. `v2026.5.29.2`),
so those are picked up on the next daily check; anything more urgent than the next
release is handled by the curated critical-backport lane, never an unattended
`upstream/main` merge.

## Rollback

A merge commit is reverted with `git revert -m 1 <merge-commit>`. On the server,
`hermes update` keeps the previous state for rollback; deploy is gated on green CI.

## Security

Upstream code is untrusted until it has passed CI + owner review — the same gate
that protects the whole self-evolution pipeline. Always work on a `sync/*` branch,
never commit conflict markers, never merge directly into `main`.

## Output format

Save the report to `~/.hermes/evolution/upstream/YYYY-MM-DD.md`:

```markdown
# Upstream Sync Report - YYYY-MM-DD

## Summary
- Commits merged: <BEHIND>
- Conflicts resolved: <n>  (autonomous | escalated-to-owner)
- Our features verified: telemetry, dotenv-secrets, flush, docs-only CI, skills
- Banner version: v<tag>

## Conflicts (if any)
### <file> — <resolution: theirs|ours|both|reverted-follow>
- rationale (authorship: upstream-reverted / our-feature / upstream-domain)

## Verification
- compile: ok | pytest cron/run_agent/tools/hermes_cli: <n> passed | CI: <link>
```
