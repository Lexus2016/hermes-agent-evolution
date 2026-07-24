#!/bin/bash
# enable_branch_protection.sh — turn ON the branch protection that makes the
# evolution self-merge safety REAL. Without it, BOTH machine controls are inert:
# the deterministic merge-gate (scripts/evolution_merge_gate.py) is only advisory
# (the agent is merely asked to run it), and .github/CODEOWNERS does nothing
# unless "Require review from Code Owners" is enforced on `main`. This is the
# manual step EVOLUTION_README.md flags as REQUIRED — wrapped in one command
# instead of a copy-paste `gh api` blob so it is far less likely to be skipped.
#
# Run it YOURSELF, as the repo OWNER (needs an admin token). It is idempotent and
# is NEVER invoked automatically: enabling protection on a GitHub repo is a
# deliberate owner action, not an install/update side effect (an install script
# silently mutating your repo's protection — or clobbering settings you already
# have — would be worse than the gap it closes).
#
# Usage:
#   scripts/enable_branch_protection.sh                        # Lexus2016/hermes-agent-evolution, main
#   GITHUB_EVOLUTION_REPO=you/your-fork scripts/enable_branch_protection.sh
#   BRANCH=main CHECK_CONTEXTS='Tests,lint' scripts/enable_branch_protection.sh
#   REQUIRED_APPROVING_REVIEWS=1 scripts/enable_branch_protection.sh   # full human-in-the-loop
#
# Effect (matches EVOLUTION_README.md "Enable branch protection"):
#   * required, strict (up-to-date) status checks — default: the "Tests" check
#   * require Code Owner review  → CODEOWNERS paths (self-update, scheduler, CI,
#     the merge-gate machinery, evolution skills) need @owner approval
#   * enforce_admins: true       → even the owner/agent token can't force past it
#   * no force-pushes, no branch deletion
# With REQUIRED_APPROVING_REVIEWS=0 (default) ordinary green PRs still auto-merge
# (autonomy); only CODEOWNERS critical paths block on human review.

set -euo pipefail

REPO="${GITHUB_EVOLUTION_REPO:-Lexus2016/hermes-agent-evolution}"
BRANCH="${BRANCH:-main}"
# Comma-separated required CI check names, exactly as they appear in Actions.
CHECK_CONTEXTS="${CHECK_CONTEXTS:-Tests}"
# 0 = CODEOWNERS-only review (autonomy for ordinary PRs); 1 = every PR needs a human approval.
REVIEWS="${REQUIRED_APPROVING_REVIEWS:-0}"

if ! command -v gh >/dev/null 2>&1; then
    echo "❌ gh CLI not found — install it and run 'gh auth login' as the repo owner." >&2
    exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
    echo "❌ gh is not authenticated — run 'gh auth login' as the repo owner (needs admin on $REPO)." >&2
    exit 1
fi

# Build a JSON array of check contexts from the comma-separated list (trim spaces).
contexts_json="$(printf '%s' "$CHECK_CONTEXTS" | awk -F',' '{
    printf "[";
    for (i = 1; i <= NF; i++) { gsub(/^ +| +$/, "", $i); printf "%s\"%s\"", (i > 1 ? "," : ""), $i }
    printf "]"
}')"

echo "🔒 Enabling branch protection on ${REPO}@${BRANCH} ..."
echo "   required checks: ${CHECK_CONTEXTS} | code-owner review: yes | approvals: ${REVIEWS} | enforce_admins: yes"

if gh api -X PUT "repos/${REPO}/branches/${BRANCH}/protection" --input - >/dev/null <<JSON
{
  "required_status_checks": { "strict": true, "contexts": ${contexts_json} },
  "enforce_admins": true,
  "required_pull_request_reviews": { "require_code_owner_reviews": true, "required_approving_review_count": ${REVIEWS} },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
then
    echo "✅ Branch protection enabled on ${REPO}@${BRANCH}."
    echo "   CODEOWNERS + the merge-gate now actually gate self-merges (they were inert without this)."
else
    echo "❌ Failed to set branch protection (need admin on ${REPO}, and the check name(s) must exist in Actions)." >&2
    exit 1
fi
