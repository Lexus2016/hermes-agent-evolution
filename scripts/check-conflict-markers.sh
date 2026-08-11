#!/bin/bash
# check-conflict-markers.sh
# Fails if any tracked file contains an unresolved git merge conflict marker
# (^<<<<<<< X or ^>>>>>>> X).
#
# Motivating incident (2026-08-10): an aborted `git stash pop` left conflict
# markers inside tools/skill_provenance.py. Python raised a bare SyntaxError
# on import, breaking `hermes` startup with no hint of the root cause. This
# check catches the class of bug (a) in CI so it can never reach main, and
# (b) locally when wired as a git hook.
#
# Usage:
#   # Manual scan
#   bash scripts/check-conflict-markers.sh
#
#   # Wire as a local git hook (any of these)
#   ln -sf ../../scripts/check-conflict-markers.sh .git/hooks/post-merge
#   ln -sf ../../scripts/check-conflict-markers.sh .git/hooks/post-checkout
#   ln -sf ../../scripts/check-conflict-markers.sh .git/hooks/post-rewrite
#
#   # Wire as a launcher guard (any wrapper script)
#   bash scripts/check-conflict-markers.sh || exit 78
#
# Regex rationale: matches only real git markers (7 angle brackets + space +
# label). Deliberately does NOT match bare "=======" so Markdown header
# underlines inside docstrings do not false-positive.

set -u

# Locate repo root regardless of caller cwd.
if repo=$(git rev-parse --show-toplevel 2>/dev/null); then
    cd "$repo" || exit 0
else
    echo "check-conflict-markers: not a git repository" >&2
    exit 0
fi

markers=$(git grep -lE '^(<<<<<<<|>>>>>>>) ' 2>/dev/null | head -50)

[ -z "$markers" ] && exit 0

echo "" >&2
echo "check-conflict-markers: unresolved merge conflict markers in $repo:" >&2
echo "$markers" | sed "s|^|  |" >&2
echo "" >&2
echo "Fix each file (remove <<<<<<< ... >>>>>>> blocks) or run:" >&2
echo "  git checkout HEAD -- <file>   # restore from index" >&2
echo "" >&2
exit 1
