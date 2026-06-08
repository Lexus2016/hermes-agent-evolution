# 🧬 Hermes Evolution — Install & Update Guide

**Run the original Hermes Agent as our self-evolving fork, and keep it updated —
using the OFFICIAL `hermes update`, not custom scripts.**

---

## 🎯 What this does

Hermes Evolution is a fork of [Hermes Agent](https://github.com/nousresearch/hermes-agent).
The right way to install and update it is the **official Hermes tooling**
(`scripts/install.sh` + `hermes update`), with the install's git `origin`
pointed at this fork. Then `hermes update` pulls evolution automatically, on
every platform, with built-in backup and rollback.

> **Why not a custom upgrade script?** Earlier versions of `upgrade.sh` /
> `auto_update.sh` reinvented the updater: they cloned a *second* copy to
> `~/hermes-agent-evolution` (separate from the real install at
> `/usr/local/lib/hermes-agent`) and made a conflicting `~/.local/bin/hermes`
> symlink — which broke the `hermes` command on a real install. Those scripts
> are now deprecated. **`hermes update` is the supported mechanism.**

---

## 📋 Prerequisites

- Python 3.11+, Git, `gh` (for the bot/PR flow), internet access.
- The `hermes` command on PATH (install it below if absent).

---

## 🔑 How updates flow (so the behavior is predictable)

`hermes update` applies from **`origin/<branch>`** (default `main`) of the
**real install dir**, and:

- If `origin` = **this fork** → it pulls our evolution. ✅
- It is fork-aware: because our fork has its own commits, the built-in
  "sync from upstream" step **skips** to preserve our changes
  (it never overwrites evolution with the original).
- It takes a pre-update snapshot and **rolls back automatically** if the update
  fails.

So the whole job is: **point `origin` at the fork, then run `hermes update`.**

---

## 🅰️ Switch an EXISTING Hermes install onto the fork

If the original Hermes Agent is already installed:

```bash
# 1. Find the real install dir from the hermes binary (works on any layout):
HERMES_BIN="$(readlink -f "$(command -v hermes)")"
INSTALL_DIR="$(dirname "$(dirname "$(dirname "$HERMES_BIN")")")"
echo "Install dir: $INSTALL_DIR"     # e.g. /usr/local/lib/hermes-agent

# 2. Point origin at THIS fork; keep upstream at NousResearch (for sync/compare):
git -C "$INSTALL_DIR" remote set-url origin https://github.com/Lexus2016/hermes-agent-evolution.git
git -C "$INSTALL_DIR" remote add  upstream https://github.com/nousresearch/hermes-agent.git 2>/dev/null || true
git -C "$INSTALL_DIR" remote -v    # verify origin = fork, upstream = original

# 3. Update onto the fork (cross-platform, with snapshot/rollback):
hermes update --check     # preview
hermes update --yes       # apply

# 4. Register evolution cron jobs into the native scheduler (jobs.json):
"$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/scripts/register_evolution_cron.py"

# 5. Schedule the daily self-update (see below):
bash "$INSTALL_DIR/scripts/install_auto_update.sh"

# 6. Verify:
hermes doctor
hermes cron list | grep -i evolution
```

> **First update note:** if the existing install was on a much newer original
> `main` than the fork's base, the first `hermes update` may `reset --hard` to
> the fork (a *replacement*, not a merge). Your data in `~/.hermes` is never
> touched. To avoid shipping a stale base, keep the fork synced with upstream
> (see "Keeping the fork current").

---

## 🅱️ Fresh install, then switch to the fork

The official installer clones the **original** repo, so install first, then do
section 🅰️ steps 2–6:

```bash
# Official install (root → /usr/local/lib/hermes-agent, non-root → ~/.hermes/hermes-agent):
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
# then run section 🅰️ (point origin at the fork, hermes update, register cron, schedule).
```

---

## 🔁 Daily self-update (scheduled)

Self-evolution = run `hermes update` on a schedule. It's the official updater,
so it's safe and cross-platform.

**Linux / macOS (cron):**
```bash
bash "$INSTALL_DIR/scripts/install_auto_update.sh"      # daily ~04:17
# custom time:
AUTO_UPDATE_SCHEDULE="30 5 * * *" bash "$INSTALL_DIR/scripts/install_auto_update.sh"
# remove:
bash "$INSTALL_DIR/scripts/install_auto_update.sh" --remove
```
This installs one cron line: `hermes update --yes` (non-interactive, keeps the
pre-update backup).

**Windows (Task Scheduler):**
```powershell
# Run hermes update daily at 04:17 (native, no WSL needed):
schtasks /Create /SC DAILY /ST 04:17 /TN "HermesEvolutionUpdate" /TR "hermes update --yes" /F
# remove:
schtasks /Delete /TN "HermesEvolutionUpdate" /F
```

---

## ⏰ Evolution cron jobs

Evolution's scheduled tasks (research/issues/analysis/implementation/upstream-sync)
live as YAML in `cron/evolution/*.yaml`, but Hermes schedules from its native
registry `~/.hermes/cron/jobs.json`. Register them (idempotent):

```bash
"$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/scripts/register_evolution_cron.py"
hermes cron list | grep -i evolution
```

---

## 🔄 Keeping the fork current (upstream → fork)

The fork can fall behind the original. `hermes update` will NOT auto-merge
upstream into a fork that has its own commits (by design — it preserves
evolution). To pull upstream improvements, do it deliberately on the fork via a
PR (so CI + the safety gate run), not on the server. This is the
`evolution-upstream-sync` job's purpose.

---

## ↩️ Rollback

`hermes update` snapshots before applying and rolls back automatically on
failure. For a manual rollback it prints the exact `git reset --hard <pre-sha>`
command; data in `~/.hermes` is independent of code and is not changed by code
updates.

---

## 🛡️ Safety gate

Autonomous self-modification is gated (see EVOLUTION_README "Гейт безпечної
самоеволюції"): the agent opens PRs only (never merges to `main`), CI must pass,
`.github/CODEOWNERS` protects critical paths, and `main` requires branch
protection. Configure a separate **bot account** for the agent so the owner can
review its PRs (`scripts/setup_evolution_bot.sh`).

---

## 📖 More

- `EVOLUTION_README.md` — evolution architecture, modes, safety gate.
- `MIGRATION_GUIDE.md` — data-preserving migration details.
- Upstream: https://github.com/nousresearch/hermes-agent
