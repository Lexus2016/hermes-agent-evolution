#!/bin/bash
# One-line upgrade script for Hermes Evolution
# Source: https://github.com/Lexus2016/hermes-agent-evolution

set -e
echo "🧬 Upgrading to Hermes Evolution..."
echo "Creating backup..."
BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
cp -r ~/.hermes ~/.hermes.backup.$BACKUP_DATE
echo "✅ Backup: ~/.hermes.backup.$BACKUP_DATE"
echo "Cloning Hermes Evolution..."
git clone https://github.com/Lexus2016/hermes-agent-evolution.git /tmp/hermes-evolution
echo "Running migration..."
bash /tmp/hermes-evolution/scripts/migrate-from-hermes.sh
echo "Verifying migration..."
python3 /tmp/hermes-evolution/scripts/verify-migration.py ~/.hermes.backup.$BACKUP_DATE
echo "🎉 Upgrade complete! Test with: hermes --help"
