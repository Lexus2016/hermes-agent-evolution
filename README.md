# Hermes Evolution 🧬

> **Self-evolving AI Agent** — Research • Propose • Implement • Update

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Evolution](https://img.shields.io/badge/evolution-active-green.svg)](https://github.com/Lexus2016/hermes-agent-evolution)

---

## 🎯 What is Hermes Evolution?

**Hermes Evolution** is a self-improving AI agent based on [Hermes Agent](https://github.com/nousresearch/hermes-agent) by Nous Research, enhanced with autonomous evolution capabilities.

### Key Innovation: Collaborative Evolution

```
┌─────────────────────────────────────────────────────────────┐
│              COLLABORATIVE EVOLUTION ARCHITECTURE             │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  PUBLIC Mode — Everyone contributes:                         │
│  ┌──────────────────┐         ┌──────────────────┐         │
│  │ DAILY RESEARCH   │────────▶│  CREATE ISSUES   │         │
│  │  (24h cron)      │         │  (proposals)     │         │
│  └──────────────────┘         └──────────────────┘         │
│           │                                                   │
│           ▼                                                   │
│    [All installations propose improvements]                 │
│           │                                                   │
│  PRIVATE Mode — Only owner implements:                      │
│           ▼                                                   │
│  ┌──────────────────┐         ┌──────────────────┐         │
│  │ ANALYZE & PRIORITIZE│────────▶│  IMPLEMENT      │         │
│  │  (24h cron)      │         │  (auto-update)   │         │
│  └──────────────────┘         └──────────────────┘         │
│                                         │                    │
│                                         ▼                    │
│                                  ┌─────────────┐            │
│                                  │ SELF-UPDATE │            │
│                                  └─────────────┘            │
└─────────────────────────────────────────────────────────────┘
```

**How it works:**
1. All installations research and propose improvements
2. Only the owner's agent analyzes, implements, and self-updates
3. Regular sync with upstream Hermes Agent keeps features current

---

## ✨ Features

### 🧠 Base Capabilities (from Hermes Agent)
- Multi-tool AI agent with LLM integration
- Skills system for task specialization
- Cron jobs for automation
- Memory and context management
- Multi-provider support (OpenAI, Anthropic, etc.)

### 🧬 Evolution Capabilities (NEW)
- **Autonomous Research**: Scans other agents, papers, trends daily
- **Proposal Generation**: Creates GitHub issues with improvement ideas
- **Analysis & Prioritization**: Scores proposals by impact/effort
- **Implementation**: Automatically implements selected improvements
- **Self-Update**: Creates versions and updates itself
- **Upstream Sync**: Syncs with original Hermes Agent weekly

---

## 🚀 Quick Start

### Option 1: Fresh Installation

```bash
# Clone Hermes Evolution
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution

# Run setup
./setup-hermes.sh

# Configure evolution (see EVOLUTION_README.md)
export GITHUB_TOKEN=*** For PUBLIC mode (all users)
export GITHUB_PRIVATE_TOKEN=*** # Only for repository owner
```

### Option 2: Migrate from Hermes Agent

Already using Hermes Agent? **Migrate without data loss:**

```bash
# Backup your installation
cp -r ~/.hermes ~/.hermes.backup.$(date +%Y%m%d)

# Clone Hermes Evolution
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution

# Run migration script
./scripts/migrate-from-hermes.sh ~/.hermes.backup.*

# Continue using your data, skills, and configuration
```

**See [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) for detailed migration instructions.**

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| **EVOLUTION_README.md** | Evolution capabilities and architecture |
| **MIGRATION_GUIDE.md** | Migrate from Hermes Agent without data loss |
| **CONTRIBUTING_EVOLUTION.md** | Contribution guidelines |
| **CODE_OF_CONDUCT.md** | Community guidelines |
| **SECURITY_EVOLUTION.md** | Security policy and best practices |
| **AGENTS.md** | Original Hermes Agent documentation |

---

## 🔄 Evolution in Action

### Daily Evolution Cycle

| Time | Task | Mode | Description |
|------|------|-------|-------------|
| 08:00 (Sun) | Upstream Sync | PRIVATE | Sync with Hermes Agent |
| 09:00 | Research | PUBLIC | Scan agents & papers |
| 12:00 | Create Issues | PUBLIC | Generate proposals |
| 21:00 | Analysis | PRIVATE | Prioritize changes |
| 22:00 | Implementation | PRIVATE | Implement & update |

### Example Workflow

```bash
# 1. Research runs automatically (9 AM)
# Result: ~/.hermes/profiles/user1/evolution/research/2026-06-08.md

# 2. Issues created automatically (12 PM)
# Result: https://github.com/Lexus2016/hermes-agent-evolution/issues

# 3. Analysis runs automatically (9 PM, owner only)
# Result: ~/.hermes/profiles/user1/evolution/analysis/2026-06-08.json

# 4. Implementation runs automatically (10 PM, owner only)
# Result: New git tag, agent self-updates
```

---

## 🔐 Modes of Operation

### PUBLIC Mode (Default)
**For: All installations**

✅ **Can:**
- Research other agents and papers
- Create GitHub issues and PRs
- Use all Hermes Agent features

❌ **Cannot:**
- Modify code directly
- Merge pull requests
- Self-update

**Setup:**
```bash
export GITHUB_TOKEN="*** For PRIVATE Mode (Repository Owner Only)
**For: Lexus2016's installation only**

✅ **Everything in PUBLIC mode, plus:**
- Analyze and prioritize proposals
- Implement selected improvements
- Merge pull requests
- Create versions and self-update
- Sync with upstream Hermes Agent

**Setup:**
```bash
export GITHUB_PRIVATE_TOKEN="*** Evolution Skills

- **[evolution-research](skills/evolution/evolution-research/SKILL.md)** — Research agents & papers
- **[evolution-issues](skills/evolution/evolution-issues/SKILL.md)** — Create GitHub issues/PR
- **[evolution-analysis](skills/evolution/evolution-analysis/SKILL.md)** — Prioritize improvements
- **[evolution-implementation](skills/evolution/evolution-implementation/SKILL.md)** — Implement & update
- **[evolution-upstream-sync](skills/evolution/evolution-upstream-sync/SKILL.md)** — Sync with upstream

---

## 🔄 Updating Hermes Evolution

### Automatic Updates (PRIVATE mode only)

If you're the repository owner, Hermes Evolution updates itself automatically.

### Manual Updates (All users)

```bash
cd ~/hermes-agent-evolution  # or your installation path
git pull origin main
./setup-hermes.sh  # Re-run to ensure dependencies
```

### Update from Hermes Agent (Upstream)

If you were using original Hermes Agent:

```bash
./scripts/migrate-from-hermes.sh ~/.hermes.backup.*
```

See [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md).

---

## 🛠️ Installation

### Requirements

- **Python**: 3.11 or higher
- **OS**: macOS, Linux, or Windows (native PowerShell installer `scripts/install.ps1`, or WSL)
- **Git**: For cloning and updates
- **GitHub Account**: For tokens (optional for basic use)

### Step-by-Step

1. **Clone the repository:**
```bash
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution
```

2. **Run the setup script:**
```bash
./setup-hermes.sh
```

> **Windows (native, no WSL):** run the PowerShell installer instead:
> ```powershell
> powershell -ExecutionPolicy Bypass -File scripts/install.ps1
> ```

3. **Configure evolution tokens (optional):**
```bash
# For PUBLIC mode (research + proposals)
export GITHUB_TOKEN="*** For PRIVATE mode (implement + self-update, owner only)
export GITHUB_PRIVATE_TOKEN="*** the evolution cron jobs:**
```bash
# Register ALL evolution cron jobs from cron/evolution/*.yaml (idempotent):
~/hermes-agent-evolution/venv/bin/python \
  ~/hermes-agent-evolution/scripts/register_evolution_cron.py
```

See [EVOLUTION_README.md](EVOLUTION_README.md) for complete setup.

---

## 📖 Usage

### Basic Usage (Same as Hermes Agent)

```bash
# Start interactive agent
hermes

# Ask a question
hermes "What's the weather like?"

# Use a specific skill
hermes --skill github-pr-workflow
```

### Evolution Usage

```bash
# Run research manually
hermes --skill evolution-research

# Check evolution mode
python evolution/detect_mode.py

# View research reports
cat ~/.hermes/profiles/user1/evolution/research/*.md
```

---

## 🆚 Hermes Evolution vs Hermes Agent

| Feature | Hermes Agent | Hermes Evolution |
|---------|--------------|------------------|
| Base Agent Capabilities | ✅ | ✅ (inherited) |
| Skills System | ✅ | ✅ (inherited) |
| Cron Jobs | ✅ | ✅ (inherited) |
| **Autonomous Research** | ❌ | ✅ **NEW** |
| **Proposal Generation** | ❌ | ✅ **NEW** |
| **Analysis & Prioritization** | ❌ | ✅ **NEW** |
| **Self-Implementation** | ❌ | ✅ **NEW** |
| **Self-Update** | ❌ | ✅ **NEW** |
| **Upstream Sync** | ❌ | ✅ **NEW** |

---

## 🤝 Contributing

We welcome contributions! See [CONTRIBUTING_EVOLUTION.md](CONTRIBUTING_EVOLUTION.md).

Quick start:
1. Fork the repository
2. Create a branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Run tests: `pytest tests/`
5. Submit a pull request

---

## 🐛 Troubleshooting

### Migration Issues

If you encounter problems during migration:

```bash
# Check backup
ls -la ~/.hermes.backup.*

# Verify data integrity
python scripts/verify-migration.py ~/.hermes.backup.*

# Re-run migration if needed
./scripts/migrate-from-hermes.sh ~/.hermes.backup.* --force
```

### Evolution Not Working

```bash
# Check mode
python evolution/detect_mode.py

# Verify tokens
echo $GITHUB_TOKEN
echo $GITHUB_PRIVATE_TOKEN

# Check logs
tail -f ~/.hermes/profiles/user1/logs/evolution-*.log
```

### More Help

- **Documentation**: See [docs/](docs/)
- **Issues**: [Create an issue](https://github.com/Lexus2016/hermes-agent-evolution/issues)
- **Discussions**: [Join Discussions](https://github.com/Lexus2016/hermes-agent-evolution/discussions)

---

## 📄 License

Apache License 2.0 — Inherits from [Hermes Agent](https://github.com/nousresearch/hermes-agent) by Nous Research.

See [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

- **[Nous Research](https://nousresearch.com/)** — Original Hermes Agent
- **Hermes Agent Contributors** — Core agent functionality
- **Open Source Community** — Tools and libraries

---

## 📢 Status

🧬 **Evolution Status**: Active Development

✅ **Implemented**:
- Evolution skills (5 skills)
- Cron jobs (5 jobs)
- Mode detection
- Documentation

🚧 **In Progress**:
- Automated testing
- Enhanced upstream sync
- Web UI for monitoring

📋 **Planned**:
- Multi-agent collaboration
- Predictive evolution
- Enhanced rollback mechanism

---

**Ready to evolve?** [Get started now!](#-quick-start) 🚀

---

**Website**: [hermes-agent.nousresearch.com](https://hermes-agent.nousresearch.com)
**Repository**: [github.com/Lexus2016/hermes-agent-evolution](https://github.com/Lexus2016/hermes-agent-evolution)
**Upstream**: [github.com/nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)
