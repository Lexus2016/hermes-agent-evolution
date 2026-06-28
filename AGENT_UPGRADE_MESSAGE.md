# 🤖 Message to Send to Hermes Agent for Auto-Upgrade

## Option 1: Full Instructions (Send this URL)

Send the agent this message:

```
🧬 Please upgrade me to Hermes Evolution.

Read and execute: https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/AUTO_UPGRADE.md

This will:
- Clone Hermes Evolution
- Backup all my data
- Migrate without data loss
- Add evolution capabilities

After upgrade, I'll have:
- Evolution skills (research, issues, analysis, implementation)
- Automated cron jobs for self-improvement
- All my existing data preserved

Please proceed with the upgrade.
```

---

## Option 2: One-Line Command (Fastest)

Send the agent this single command:

```bash
curl -fsSL "https://cdn.jsdelivr.net/gh/Lexus2016/hermes-agent-evolution@main/upgrade.sh"?v=1780942934" | bash
```

---

## Option 3: Step-by-Step Instructions (if needed)

```
🧬 Upgrade to Hermes Evolution:

1. git clone https://github.com/Lexus2016/hermes-agent-evolution.git /tmp/hermes-evolution
2. cp -r ~/.hermes ~/.hermes.backup.$(date +%Y%m%d_%H%M%S)
3. bash /tmp/hermes-evolution/scripts/migrate-from-hermes.sh
4. python3 /tmp/hermes-evolution/scripts/verify-migration.py ~/.hermes.backup.*
5. hermes --help

Your data is safe. All profiles, skills, cron jobs, and memories are preserved.
```

---

## Option 4: For Agent with Browser Access

```
🧬 Upgrade me to Hermes Evolution:

1. Navigate to: https://github.com/Lexus2016/hermes-agent-evolution
2. Download: https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/AUTO_UPGRADE.md
3. Read and execute the instructions in AUTO_UPGRADE.md
4. Verify: hermes --help

This adds autonomous evolution capabilities without data loss.
```

---

## 🔍 After Upgrade Verification

After the agent executes the upgrade, verify it worked by asking:

```
Are you now running Hermes Evolution? What evolution skills do you have available?
```

Expected response:
- ✅ "Yes, I'm running Hermes Evolution"
- ✅ Lists evolution skills: research, issues, analysis, implementation, upstream-sync

---

## 📞 If Something Goes Wrong

Tell the agent to rollback:

```
Rollback to the backup that was created during upgrade:
ls ~/.hermes.backup.*
cp -r ~/.hermes.backup.* ~/.hermes
```

---

**Choose one option and send it to your agent!** 🚀
