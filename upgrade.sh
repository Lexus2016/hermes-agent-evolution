#!/bin/bash
# upgrade.sh — DEPRECATED. Do not use.
#
# Earlier versions reinvented `hermes update`: they cloned the repo to
# ~/hermes-agent-evolution (a SECOND copy, separate from the real install at
# /usr/local/lib/hermes-agent) and created a conflicting ~/.local/bin/hermes
# symlink. On a real install that broke the `hermes` command (dangling symlink)
# and produced ImportErrors.
#
# The correct mechanism is the OFFICIAL `hermes update`, pointed at THIS fork.
# It updates the real install dir, is cross-platform (Linux/macOS/Windows), and
# has built-in snapshot/rollback. This script now only prints the correct steps.
#
# Full guide: AUTO_UPGRADE.md

set -euo pipefail

cat <<'EOF'
⛔ upgrade.sh is DEPRECATED and intentionally does nothing.

It reinvented `hermes update` incorrectly and could break your install.
Use the official updater pointed at this fork instead:

  # 1. Find your install dir (from the hermes binary):
  HERMES_BIN="$(readlink -f "$(command -v hermes)")"
  INSTALL_DIR="$(dirname "$(dirname "$(dirname "$HERMES_BIN")")")"
  echo "Install dir: $INSTALL_DIR"

  # 2. Point it at THIS fork (origin); keep upstream at NousResearch:
  git -C "$INSTALL_DIR" remote set-url origin https://github.com/Lexus2016/hermes-agent-evolution.git
  git -C "$INSTALL_DIR" remote add  upstream https://github.com/nousresearch/hermes-agent.git 2>/dev/null || true

  # 3. Update onto the fork (cross-platform, with rollback):
  hermes update --yes

  # 4. Register evolution cron jobs:
  "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/scripts/register_evolution_cron.py"

  # 5. Schedule daily self-update:
  bash "$INSTALL_DIR/scripts/install_auto_update.sh"

Full guide: AUTO_UPGRADE.md
EOF
exit 1
