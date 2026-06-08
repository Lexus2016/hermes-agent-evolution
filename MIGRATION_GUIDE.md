# Migration Guide: Hermes Agent → Hermes Evolution

## 🎯 Overview

This guide helps you migrate from the original [Hermes Agent](https://github.com/nousresearch/hermes-agent) to [Hermes Evolution](https://github.com/Lexus2016/hermes-agent-evolution) **without losing any data, skills, configurations, or customizations**.

### What Preserves

✅ **Your data will be preserved:**
- All your profiles and user data
- All your custom skills
- All your cron jobs
- All your memories and session history
- All your configurations and settings
- All your plugins and custom tools

### What Changes

🔄 **What will be updated:**
- Base agent code (Hermes Evolution version)
- Evolution skills will be added
- Evolution cron jobs will be added (optional)
- Documentation will be updated

---

## 📋 Prerequisites

### Before You Begin

1. **Verify your current installation:**
```bash
# Check where Hermes is installed
which hermes

# Check current version (if available)
hermes --version

# List your profiles
ls -la ~/.hermes/profiles/
```

2. **Identify your customizations:**
```bash
# List custom skills
ls -la ~/.hermes/skills/
ls -la ~/.hermes/profiles/*/skills/

# List cron jobs
hermes cron list

# List memories
ls -la ~/.hermes/profiles/*/memories/
```

3. **Backup your data** (automatic during migration, but good practice):
```bash
# Create manual backup
cp -r ~/.hermes ~/.hermes.manual.backup.$(date +%Y%m%d)
```

---

## 🚀 Migration Methods

### Method 1: Automatic Migration (Recommended)

**Best for:** Most users, smooth migration, minimal downtime

```bash
# 1. Clone Hermes Evolution
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution

# 2. Run automatic migration
./scripts/migrate-from-hermes.sh

# 3. Verify migration
./scripts/verify-migration.py

# 4. Test the new installation
hermes --help
```

**What it does:**
- Detects your current Hermes installation
- Backs up all data automatically
- Installs Hermes Evolution
- Migrates all profiles, skills, cron jobs
- Verifies integrity
- Preserves all configurations

### Method 2: Manual Migration

**Best for:** Advanced users with complex setups

#### Step 1: Backup

```bash
# Create timestamped backup
BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
cp -r ~/.hermes ~/.hermes.backup.$BACKUP_DATE

# Verify backup
ls -la ~/.hermes.backup.$BACKUP_DATE/profiles/
```

#### Step 2: Clone Hermes Evolution

```bash
# Clone to temporary location
git clone https://github.com/Lexus2016/hermes-agent-evolution.git /tmp/hermes-evolution
cd /tmp/hermes-evolution
```

#### Step 3: Run Setup

```bash
# Run setup (this won't overwrite your data)
./setup-hermes.sh
```

#### Step 4: Verify Migration

```bash
# Check profiles are preserved
hermes profile list

# Check custom skills
ls -la ~/.hermes/skills/

# Check cron jobs
hermes cron list

# Test a query
hermes "test"
```

#### Step 5: Clean Up (Optional)

```bash
# Once verified, you can remove the backup
# rm -rf ~/.hermes.backup.$BACKUP_DATE
```

### Method 3: Side-by-Side Installation

**Best for:** Testing before fully switching

```bash
# Install Hermes Evolution alongside
git clone https://github.com/Lexus2016/hermes-agent-evolution.git ~/hermes-evolution
cd ~/hermes-evolution
./setup-hermes.sh --prefix ~/hermes-evolution-install

# Use with separate profile
hermes --profile evolution-profile
```

---

## 🔄 What Gets Migrated

### Profiles and User Data

```bash
# All profiles are migrated
~/.hermes/profiles/user1/          → preserved
~/.hermes/profiles/user2/          → preserved
~/.hermes/profiles/work/           → preserved
```

**What's preserved:**
- Profile configurations
- Session history
- Memories
- Custom settings

### Skills

```bash
# Custom skills are preserved
~/.hermes/skills/custom-skill/     → preserved
~/.hermes/profiles/user1/skills/   → preserved
```

**What's added:**
- Evolution skills (5 new skills)

### Cron Jobs

```bash
# Existing cron jobs are preserved
hermes cron list                    → all jobs preserved
```

**What's added:**
- Evolution cron jobs (optional, you choose which to enable)

### Configuration

```bash
# All configs preserved
~/.hermes/profiles/*/config.yaml  → preserved
~/.hermes/config.yaml             → preserved
```

**What's added:**
- Evolution-specific configs

---

## ⚙️ Post-Migration Setup

### 1. Configure Evolution Tokens (Optional)

If you want to enable evolution features:

```bash
# For PUBLIC mode (all users)
export GITHUB_TOKEN=*** For PRIVATE mode (repository owner only)
export GITHUB_PRIVATE_TOKEN=*** # Add these to ~/.bashrc or ~/.zshrc for persistence
echo 'export GITHUB_TOKEN=*** echo 'export GITHUB_PRIVATE_TOKEN=***# 2. Enable Evolution Cron Jobs (Optional)

```bash
# Add evolution jobs to your existing cron setup
hermes cron create --name evolution-research \
  --schedule "0 9 * * *" \
  --prompt "$(cat ~/hermes-evolution/cron/evolution/research.yaml)" \
  --skills evolution/research
```

**Note:** Evolution cron jobs are optional. You can continue using Hermes Evolution without them.

### 3. Verify Everything Works

```bash
# Test basic functionality
hermes "What is 2+2?"

# Check profiles
hermes profile list

# List skills (should see evolution skills)
hermes skills list

# If you enabled cron jobs, verify
hermes cron list
```

### 4. Update References (If Needed)

If you have scripts referencing Hermes:

```bash
# Update shebang lines if using custom paths
# Old: #!/usr/local/bin/hermes
# New: #!/usr/bin/hermes (or wherever it's installed)
```

---

## 🔍 Verification

### Automatic Verification

```bash
# Run verification script
./scripts/verify-migration.py ~/.hermes.backup.*
```

### Manual Verification Checklist

- [ ] `hermes --help` works
- [ ] All profiles are listed: `hermes profile list`
- [ ] Custom skills are present: `ls ~/.hermes/skills/`
- [ ] Cron jobs are preserved: `hermes cron list`
- [ ] Can run a query: `hermes "test"`
- [ ] Session history is intact
- [ ] Memories are preserved

### Test Each Profile

```bash
# Test each profile you have
for profile in user1 user2 work; do
    echo "Testing profile: $profile"
    hermes --profile $profile "What is your name?"
done
```

---

## 🔄 Rollback (If Needed)

If anything goes wrong, you can easily rollback:

### Automatic Rollback

```bash
# The migration script creates automatic backups
./scripts/rollback-migration.sh ~/.hermes.backup.*
```

### Manual Rollback

```bash
# Stop any running hermes processes
pkill -f hermes

# Restore from backup
rm -rf ~/.hermes
cp -r ~/.hermes.backup.* ~/.hermes

# Reinstall original Hermes Agent (if needed)
cd /path/to/hermes-agent
./setup-hermes.sh
```

---

## 📊 Migration Scenarios

### Scenario 1: Simple Installation

**Current setup:** Basic Hermes with default profile

**Migration:**
```bash
./scripts/migrate-from-hermes.sh
```

**Result:** Everything preserved, evolution skills added

### Scenario 2: Multiple Profiles

**Current setup:** Hermes with profiles for work, personal, testing

**Migration:**
```bash
./scripts/migrate-from-hermes.sh

# Verify all profiles
hermes profile list
```

**Result:** All profiles preserved, can switch between them

### Scenario 3: Custom Skills

**Current setup:** Hermes with 5 custom skills

**Migration:**
```bash
./scripts/migrate-from-hermes.sh

# Verify custom skills
ls ~/.hermes/skills/custom-*/
```

**Result:** Custom skills preserved, evolution skills added

### Scenario 4: Heavy Cron Usage

**Current setup:** 10+ cron jobs for various tasks

**Migration:**
```bash
./scripts/migrate-from-hermes.sh

# Verify all jobs
hermes cron list
```

**Result:** All cron jobs preserved, evolution jobs added (optional)

---

## 🛠️ Troubleshooting

### Issue: Command not found after migration

**Solution:**
```bash
# Reinstall Hermes
cd ~/hermes-evolution
./setup-hermes.sh

# Or add to PATH manually
export PATH="~/hermes-evolution:$PATH"
```

### Issue: Profiles not showing

**Solution:**
```bash
# Check profiles directory
ls -la ~/.hermes/profiles/

# If empty, restore from backup
cp -r ~/.hermes.backup.*/profiles/* ~/.hermes/profiles/
```

### Issue: Cron jobs missing

**Solution:**
```bash
# Check cron database
hermes cron list

# If jobs are missing, re-add them manually
# (You should have a list from pre-migration verification)
```

### Issue: Custom skills not working

**Solution:**
```bash
# Verify skill files
ls -la ~/.hermes/skills/

# Check skill syntax
hermes skills check your-custom-skill

# Reinstall if needed
hermes skills install ~/.hermes/skills/your-custom-skill/
```

---

## 📝 What's Different After Migration?

### New Features Available

1. **Evolution Skills:**
   - `evolution/research` — Research capabilities
   - `evolution/issues` — GitHub integration
   - `evolution/analysis` — Prioritization
   - `evolution/implementation` — Self-implementation
   - `evolution/upstream-sync` — Upstream sync

2. **Evolution Cron Jobs (Optional):**
   - Daily research
   - Daily issue creation
   - Daily analysis
   - Daily implementation
   - Weekly upstream sync

3. **New Documentation:**
   - EVOLUTION_README.md
   - MIGRATION_GUIDE.md (this file)
   - CONTRIBUTING_EVOLUTION.md
   - SECURITY_EVOLUTION.md

### Same Functionality

Everything you had before still works:
- All your skills
- All your cron jobs
- All your profiles
- All your data

---

## 🎯 Next Steps

### 1. Explore Evolution Features

```bash
# Check evolution mode
python ~/hermes-evolution/evolution/detect_mode.py

# Read evolution docs
cat ~/hermes-evolution/EVOLUTION_README.md
```

### 2. Enable Evolution (Optional)

If you want to enable autonomous evolution:

```bash
# Add tokens
export GITHUB_TOKEN=*** Add evolution cron jobs
hermes cron create --name evolution-research --schedule "0 9 * * *" \
  --skills evolution/research
```

### 3. Continue Using Hermes

Everything works as before, plus evolution features are available:

```bash
# Use Hermes as you normally would
hermes "Help me write code"

# Or use evolution features
hermes --skill evolution/research
```

---

## 📞 Need Help?

If you encounter issues during migration:

1. **Check logs:** `~/.hermes/logs/`
2. **Verify backup:** Ensure `~/.hermes.backup.*` exists
3. **Run verification:** `./scripts/verify-migration.py`
4. **Create issue:** [GitHub Issues](https://github.com/Lexus2016/hermes-agent-evolution/issues)
5. **Rollback:** If needed, restore from backup

---

## ✅ Migration Checklist

Use this checklist to ensure successful migration:

### Before Migration
- [ ] Identified current installation location
- [ ] Listed all profiles
- [ ] Listed all custom skills
- [ ] Listed all cron jobs
- [ ] Created manual backup (optional)

### During Migration
- [ ] Cloned Hermes Evolution
- [ ] Ran migration script
- [ ] Script completed without errors
- [ ] Automatic backup created

### After Migration
- [ ] `hermes --help` works
- [ ] All profiles present
- [ ] Custom skills preserved
- [ ] Cron jobs preserved
- [ ] Can run queries
- [ ] Session history intact
- [ ] Memories preserved

### Evolution Setup (Optional)
- [ ] Read EVOLUTION_README.md
- [ ] Configured GITHUB_TOKEN (if using PUBLIC mode)
- [ ] Configured GITHUB_PRIVATE_TOKEN (if using PRIVATE mode)
- [ ] Added evolution cron jobs (if desired)

---

**Migration complete! Welcome to Hermes Evolution!** 🧬🚀

**Your data is safe, everything is preserved, and you now have evolution capabilities.**

---

**Next:** Explore [EVOLUTION_README.md](EVOLUTION_README.md) to learn about evolution features.
