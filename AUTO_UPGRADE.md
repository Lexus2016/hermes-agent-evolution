# 🧬 Hermes Evolution — Auto-Upgrade Instructions

**Read this file to automatically upgrade from Hermes Agent to Hermes Evolution without data loss.**

---

## 🎯 What This Does

This script upgrades your existing Hermes Agent installation to **Hermes Evolution** — a self-improving version with autonomous research, proposal generation, and self-update capabilities.

**Zero data loss:** All your profiles, skills, cron jobs, memories, and configurations are preserved.

---

## 📋 Prerequisites

Your system needs:
- ✅ Python 3.11+
- ✅ Git installed
- ✅ Active internet connection
- ✅ Existing Hermes Agent installation at `~/.hermes/`

---

## 🚀 Automatic Upgrade Process

### Step 1: Clone Hermes Evolution

```bash
# Clone to temporary location
git clone https://github.com/Lexus2016/hermes-agent-evolution.git /tmp/hermes-evolution
```

### Step 2: Create Backup

```bash
# Automatic backup with timestamp
BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
cp -r ~/.hermes ~/.hermes.backup.$BACKUP_DATE
echo "Backup created at: ~/.hermes.backup.$BACKUP_DATE"
```

### Step 3: Run Migration

```bash
# Run automatic migration script
bash /tmp/hermes-evolution/scripts/migrate-from-hermes.sh
```

### Step 4: Verify Migration

```bash
# Verify all data is preserved
python3 /tmp/hermes-evolution/scripts/verify-migration.py ~/.hermes.backup.$BACKUP_DATE
```

### Step 5: Test Installation

```bash
# Test that Hermes Evolution works
hermes --help

# Test a query
hermes "What is 2+2?"

# Check evolution skills are available
hermes skills list | grep evolution
```

### Step 6: Configure Evolution (Optional)

If you want to enable autonomous evolution:

```bash
# For PUBLIC mode (research + proposals)
export GITHUB_TOKEN=*** your_new_token_here

# For PRIVATE mode (repository owner only - implementation + self-update)
export GITHUB_PRIVATE_TOKEN=*** your_private_token_here

# Add these to ~/.bashrc or ~/.zshrc for persistence
echo 'export GITHUB_TOKEN=your_token_here' >> ~/.bashrc
echo 'export GITHUB_PRIVATE_TOKEN=your_private_token_here' >> ~/.bashrc
```

---

## ✅ Verification

After upgrade, verify everything works:

```bash
# Check profiles preserved
hermes profile list

# Check custom skills preserved
ls ~/.hermes/skills/

# Check cron jobs preserved
hermes cron list

# Test evolution skills
hermes --skill evolution/research "What's new in AI agents?"
```

---

## 🔄 Rollback (If Needed)

If anything goes wrong, automatic rollback is available:

```bash
# Find your backup
ls -la ~/.hermes.backup.*

# Rollback to backup
python3 /tmp/hermes-evolution/scripts/rollback-migration.py ~/.hermes.backup.$BACKUP_DATE
```

---

## 📚 What's New

After upgrading to Hermes Evolution, you get:

### New Evolution Skills
- **evolution/research** — Research other AI agents and papers
- **evolution/issues** — Create GitHub issues with proposals
- **evolution/analysis** — Analyze and prioritize improvements
- **evolution/implementation** — Implement and self-update
- **evolution/upstream-sync** — Sync with upstream Hermes Agent

### Automated Cron Jobs
- Daily research (9 AM)
- Daily issue creation (12 PM)
- Daily analysis (9 PM, PRIVATE mode)
- Daily implementation (10 PM, PRIVATE mode)
- Weekly upstream sync (Sunday 8 AM, PRIVATE mode)

### New Documentation
- EVOLUTION_README.md — Evolution capabilities
- MIGRATION_GUIDE.md — This guide
- CONTRIBUTING_EVOLUTION.md — Contribution guidelines
- SECURITY_EVOLUTION.md — Security policy

---

## 🎯 How Evolution Works

### PUBLIC Mode (All installations)
- ✅ Research other agents and papers daily
- ✅ Create GitHub issues with improvement proposals
- ✅ Use all Hermes Agent features
- ❌ Cannot modify code or self-update

### PRIVATE Mode (Repository owner only)
- ✅ Everything in PUBLIC mode, plus:
- ✅ Analyze all proposals
- ✅ Prioritize by impact/effort
- ✅ Implement selected improvements
- ✅ Create versions and self-update
- ✅ Sync with upstream Hermes Agent

---

## 📞 Support

If you encounter issues:

1. **Check logs**: `~/.hermes/logs/`
2. **Verify backup**: Ensure `~/.hermes.backup.*` exists
3. **Run verification**: `python3 /tmp/hermes-evolution/scripts/verify-migration.py`
4. **Rollback if needed**: Use rollback script
5. **Create issue**: https://github.com/Lexus2016/hermes-agent-evolution/issues

---

## 🔐 Security Notes

- Your data is backed up automatically before migration
- No data is sent to external servers
- GitHub tokens are stored locally only
- You can rollback at any time

---

## 📖 More Information

- **Repository**: https://github.com/Lexus2016/hermes-agent-evolution
- **Documentation**: https://github.com/Lexus2016/hermes-agent-evolution/blob/main/EVOLUTION_README.md
- **Upstream**: https://github.com/nousresearch/hermes-agent

---

**Upgrade complete! Welcome to Hermes Evolution!** 🧬🚀

Your data is safe, everything is preserved, and you now have evolution capabilities.
