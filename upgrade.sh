#!/bin/bash
# upgrade.sh — switch an EXISTING Hermes Agent install onto Hermes Evolution and
# keep it auto-updating. One command, idempotent, safe to re-run.
#
# It uses the OFFICIAL `hermes update` (no clone hacks, no second copy). It also
# heals the legacy issues seen on real upgrades: stale flat evolution *.md,
# old "local" evolution skills not refreshed by skills_sync, the manifest
# "deleted-respected" quirk on re-seed, and a missing ~/.local/bin symlink.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/upgrade.sh | bash
#   # or:  bash upgrade.sh [--no-auto-update]
#
# Your data in $HERMES_HOME (sessions, memories, config) is never modified; a
# timestamped backup is taken anyway.

set -euo pipefail

FORK_URL="https://github.com/Lexus2016/hermes-agent-evolution.git"
UPSTREAM_URL="https://github.com/nousresearch/hermes-agent.git"
WITH_AUTO_UPDATE=1
WITH_STAR=1
for arg in "$@"; do
    case "$arg" in
        --no-auto-update) WITH_AUTO_UPDATE=0 ;;
        --no-star)        WITH_STAR=0 ;;
        *) echo "Unknown option: $arg (use --no-auto-update / --no-star)" >&2; exit 2 ;;
    esac
done

echo "🧬 Hermes Evolution — upgrade existing Hermes onto the fork"
echo "=========================================================="

# 1. Locate the existing install — or do a FRESH install of the fork ---------
# No Hermes yet? Don't make the user install the original first — install OUR
# fork directly (install.sh defaults to this repo), then finish evolution setup.
if ! command -v hermes >/dev/null 2>&1; then
    echo "ℹ️  No Hermes found — installing Hermes Evolution fresh (from this fork)..."
    curl -fsSL https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/scripts/install.sh | bash
    hash -r 2>/dev/null || true
    if ! command -v hermes >/dev/null 2>&1; then
        echo "❌ Fresh install finished but 'hermes' isn't on PATH yet."
        echo "   Open a NEW terminal (so PATH refreshes) and re-run this command."
        exit 1
    fi
    echo "✅ Fresh install complete — continuing with evolution setup."
fi
HERMES_BIN="$(readlink -f "$(command -v hermes)" 2>/dev/null || command -v hermes)"
# Newer installs ship a bash shim (not a symlink — #21454), so readlink alone
# lands on the shim itself. Parse its `exec "<venv>/bin/hermes"` target.
case "$HERMES_BIN" in
    */venv/bin/hermes) : ;;
    *)
        SHIM_TARGET="$(sed -n 's/^exec "\([^"]*\)".*/\1/p' "$HERMES_BIN" 2>/dev/null | head -n 1)"
        if [ -n "$SHIM_TARGET" ] && [ -x "$SHIM_TARGET" ]; then
            HERMES_BIN="$SHIM_TARGET"
        fi
        ;;
esac
INSTALL_DIR="$(dirname "$(dirname "$(dirname "$HERMES_BIN")")")"
if [ ! -d "$INSTALL_DIR/.git" ]; then
    # Last resort: the default location install.sh uses.
    FALLBACK="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
    if [ -d "$FALLBACK/.git" ] && [ -x "$FALLBACK/venv/bin/hermes" ]; then
        INSTALL_DIR="$FALLBACK"
        HERMES_BIN="$FALLBACK/venv/bin/hermes"
    else
        echo "❌ $INSTALL_DIR is not a git checkout — cannot switch remotes."
        echo "   (Expected layout <INSTALL_DIR>/venv/bin/hermes.)"
        exit 1
    fi
fi
PY="$INSTALL_DIR/venv/bin/python"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
echo "📂 Install dir: $INSTALL_DIR"
echo "📂 Data dir:    $HOME_DIR"

# 2. Backup the data dir (best-effort, keep only the 3 newest to avoid disk
#    bloat when the script is re-run).
if [ -d "$HOME_DIR" ]; then
    BACKUP="$HOME_DIR.backup.$(date +%Y%m%d_%H%M%S)"
    cp -r "$HOME_DIR" "$BACKUP" && echo "✅ Backup: $BACKUP"
    ls -dt "$HOME_DIR".backup.* 2>/dev/null | tail -n +4 | while read -r old; do
        rm -rf "$old" && echo "   pruned old backup: $old"
    done
fi

# 3. Point origin at the fork, keep upstream at the original ---------------
if [ "$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || echo)" = "$FORK_URL" ]; then
    echo "ℹ️  Already on the Hermes Evolution fork (re-run) — proceeding safely."
fi
git -C "$INSTALL_DIR" remote set-url origin "$FORK_URL"
if ! git -C "$INSTALL_DIR" remote get-url upstream >/dev/null 2>&1; then
    git -C "$INSTALL_DIR" remote add upstream "$UPSTREAM_URL"
fi
echo "✅ origin → fork, upstream → original"

# 4. Remove legacy flat evolution *.md so `hermes update` won't autostash ---
rm -f "$INSTALL_DIR"/skills/evolution/analysis.md \
      "$INSTALL_DIR"/skills/evolution/implementation.md \
      "$INSTALL_DIR"/skills/evolution/issues.md \
      "$INSTALL_DIR"/skills/evolution/research.md \
      "$INSTALL_DIR"/skills/evolution/upstream-sync.md 2>/dev/null || true

# 5. Update onto the fork (official, fork-aware, with rollback) -------------
echo ""
echo "🔄 Running hermes update (pulls the fork; preserves evolution)..."
PRE_HEAD="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo none)"
hermes update --yes
POST_HEAD="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo none)"
CODE_CHANGED=0
if [ "$PRE_HEAD" != "$POST_HEAD" ]; then
    CODE_CHANGED=1
else
    echo "ℹ️  No code change — already current (re-run is harmless)."
fi

# 5b. Ensure Turbo-Quant Memory (tqmemory) on the documented install path ----
# `hermes update` reconciles tqmemory internally (install if missing + register
# in every profile), but on a FRESH install the checkout install.sh just cloned
# is already current, so the update above is a no-op and may skip that internal
# reconcile. Run it explicitly here when nothing changed, so brand-new installs
# still get the memory installed + registered. Idempotent + non-fatal; honours
# HERMES_NO_TQMEMORY=1 (and the persistent memory.tqmemory_autoinstall flag).
if [ "$CODE_CHANGED" = "0" ] && [ "${HERMES_NO_TQMEMORY:-0}" != "1" ]; then
    echo ""
    echo "🧠 Ensuring Turbo-Quant Memory (tqmemory)..."
    HERMES_HOME="$HOME_DIR" "$PY" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from hermes_cli.tqmemory_setup import reconcile_tqmemory; reconcile_tqmemory()" 2>&1 | tail -2 \
        || echo "ℹ️  tqmemory setup skipped (optional)."
fi

# 6. Force-fresh the evolution skills (heals legacy 'local' copies) ---------
# Drop evolution entries from the bundled manifest, remove the dir, re-seed —
# otherwise skills_sync may keep stale 'local' copies or skip re-adding ones
# it thinks the user deleted.
echo ""
echo "🧩 Refreshing evolution skills from the fork..."
MAN="$HOME_DIR/skills/.bundled_manifest"
if [ -f "$MAN" ]; then
    grep -v "^evolution-" "$MAN" > "$MAN.tmp" 2>/dev/null && mv "$MAN.tmp" "$MAN" || true
fi
rm -rf "$HOME_DIR/skills/evolution"
HERMES_HOME="$HOME_DIR" "$PY" -c "import sys; sys.path.insert(0,'$INSTALL_DIR'); from tools.skills_sync import sync_skills; sync_skills()" >/dev/null 2>&1 \
    || echo "⚠️  skills re-seed reported issues (check: hermes skills list)"

# 7. Register evolution cron jobs into the native scheduler -----------------
echo "⏰ Registering evolution cron jobs..."
HERMES_HOME="$HOME_DIR" "$PY" "$INSTALL_DIR/scripts/register_evolution_cron.py" 2>&1 | tail -1 \
    || echo "⚠️  cron registration reported issues"

# 8. Schedule the daily self-update ---------------------------------------
if [ "$WITH_AUTO_UPDATE" = "1" ]; then
    echo "🔁 Scheduling daily self-update..."
    bash "$INSTALL_DIR/scripts/install_auto_update.sh" >/dev/null 2>&1 \
        && echo "✅ Daily 'hermes update' scheduled" \
        || echo "⚠️  Could not schedule auto-update (run scripts/install_auto_update.sh manually)"
fi

# 9. Heal the command symlink; restart gateway ONLY if code actually changed
#    (avoids needlessly interrupting a running gateway on a no-op re-run).
hermes doctor --fix >/dev/null 2>&1 || true
if [ "$CODE_CHANGED" = "1" ] && hermes gateway status >/dev/null 2>&1; then
    # When no systemd/launchd service is installed (macOS default), `hermes
    # gateway restart` falls back to running the gateway in the FOREGROUND —
    # it never returns and would block this script forever. Detach it: with a
    # service manager the command exits quickly anyway; without one the
    # detached process simply becomes the new gateway.
    (nohup hermes gateway restart >/dev/null 2>&1 &)
    echo "✅ Gateway restart initiated (picked up new code)"
elif [ "$CODE_CHANGED" = "0" ]; then
    echo "ℹ️  No code change — gateway left running (not restarted)."
fi

# 9b. Star the repo (community signal) — transparent + opt-out (--no-star).
#     Best-effort: needs a token/login with user scope; silently skips otherwise.
if [ "$WITH_STAR" = "1" ]; then
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        gh api -X PUT /user/starred/Lexus2016/hermes-agent-evolution >/dev/null 2>&1 \
            && echo "⭐ Starred Lexus2016/hermes-agent-evolution — thanks for the support! (skip with --no-star)" || true
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        curl -fsS -X PUT -H "Authorization: token $GITHUB_TOKEN" -H "Content-Length: 0" \
            https://api.github.com/user/starred/Lexus2016/hermes-agent-evolution >/dev/null 2>&1 \
            && echo "⭐ Starred the repo — thanks! (skip with --no-star)" || true
    fi
fi

# 10. Verify ---------------------------------------------------------------
echo ""
echo "=========================================================="
echo "✅ Done. Verifying:"
EVO="$(hermes skills list 2>/dev/null | grep -i evolution || true)"
if [ -n "$EVO" ]; then
    echo "$EVO" | sed 's/^/   /'
    echo "✅ Evolution skills active."
else
    echo "⚠️  Evolution skills not visible — run: hermes skills list | grep evolution"
fi
echo ""
echo "Next (optional): set GITHUB_TOKEN in $HOME_DIR/.env for research/issues jobs."
echo "Your Hermes now runs Hermes Evolution and self-updates daily. 🧬🚀"
