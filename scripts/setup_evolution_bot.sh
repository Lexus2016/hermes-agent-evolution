#!/bin/bash
# setup_evolution_bot.sh — make THIS machine act as the evolution BOT account.
#
# Why: branch protection on main forbids a PR author from approving their own PR
# and requires Code Owner review on critical paths. If the agent pushes as the
# repo OWNER, the owner can't review the agent's critical PRs -> deadlock. So the
# agent must push as a SEPARATE bot account; the owner then reviews bot PRs.
#
# This configures git/gh on the server to authenticate as the bot. It does NOT
# create the account or the token — you do that on GitHub first (see
# EVOLUTION_README "Bot-акаунт для агента").
#
# The token is read from the environment ONLY and is never printed, logged, or
# accepted as a CLI argument (which would leak into shell history / ps).
#
# Usage:
#   export GITHUB_EVOLUTION_TOKEN=<bot fine-grained PAT>   # bot account, not yours
#   scripts/setup_evolution_bot.sh
# Env:
#   GITHUB_EVOLUTION_TOKEN   (required) bot's fine-grained PAT
#   GITHUB_EVOLUTION_REPO    (optional) owner/repo, default Lexus2016/hermes-agent-evolution

set -euo pipefail

TOKEN="${GITHUB_EVOLUTION_TOKEN:-}"
REPO="${GITHUB_EVOLUTION_REPO:-Lexus2016/hermes-agent-evolution}"

if [ -z "$TOKEN" ]; then
    echo "❌ GITHUB_EVOLUTION_TOKEN is not set." >&2
    echo "   Set the BOT account's fine-grained PAT in the environment first:" >&2
    echo "     export GITHUB_EVOLUTION_TOKEN=<bot-pat>" >&2
    echo "   Never pass it as an argument — it would leak into history/ps." >&2
    exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
    echo "❌ gh CLI is required (https://cli.github.com/)." >&2
    exit 1
fi

# Authenticate as the bot. Token via stdin only; gh never echoes it.
if ! printf '%s' "$TOKEN" | gh auth login --hostname github.com --git-protocol https --with-token; then
    echo "❌ gh auth login failed — is the token valid and not expired?" >&2
    exit 1
fi

# Route git's github.com auth through gh (so pushes use the bot identity).
gh auth setup-git

# Identify the bot and set a matching git identity for commits.
BOT="$(gh api user --jq .login 2>/dev/null || echo "")"
if [ -z "$BOT" ]; then
    echo "❌ Could not resolve the bot login from the token." >&2
    exit 1
fi
git config --global user.name "$BOT"
git config --global user.email "${BOT}@users.noreply.github.com"

echo "✅ This machine now acts as evolution bot: $BOT"
echo "   Commits/PRs will be authored by $BOT (not the repo owner)."
echo "   The owner can now review the bot's critical-path PRs."

# Sanity: confirm the bot actually has write access to the repo.
PERMS="$(gh api "repos/$REPO" --jq '.permissions // {}' 2>/dev/null || echo '{}')"
echo "   Repo permissions for $BOT on $REPO: $PERMS"
case "$PERMS" in
    *'"push":true'*|*'"maintain":true'*|*'"admin":true'*)
        echo "   ✅ Write access confirmed." ;;
    *)
        echo "   ⚠️  No write access detected — add $BOT as a collaborator (write) on $REPO." ;;
esac
