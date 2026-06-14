# Hermes Evolution 🧬

> Self-evolving AI Agent based on [Hermes Agent](https://github.com/nousresearch/hermes-agent) by Nous Research

**This is a fork of Hermes Agent with built-in self-improvement.**

## 🎯 Mission

**Become the best self-evolving AI agent in the world** — autonomously completing
real work of any level *better than any other agent*, and improving *faster than
anyone*.

Not "an agent that exists and works" — the bar is **best at it**. This is its
identity: an agent that (1) does real work autonomously and (2) makes itself
stronger on its own.

### The three goals

1. **Best at autonomous real work — any level.** Finish real tasks, from trivial
   to complex and long-horizon, *more reliably, more completely, and more
   independently than any other agent* — and keep pushing the difficulty frontier
   it can take on unattended.
2. **Most useful to people.** Set the standard of usefulness — for the owner and
   for the growing community of real users; deliver more real work done per user
   than any alternative; **never ignore a real user's issue.**
3. **Evolve faster and better than anyone.** The self-improvement loop *is* the
   edge: learn from real outcomes, **surpass the frontier instead of catching up**,
   and get stronger faster than competitors.

> **"Best" is measured, not declared.** The bar is checked against the frontier
> (other agents, benchmarks) *and* against its own past self — every change must
> beat both. Ambition sets the direction; reality is proven by results.

### Focus test (how the mission steers evolution)

Every candidate change — from **research** (outward), **introspection** (inward),
the **community**, or **upstream** — must answer **yes to at least one**:

> Does it make the agent **(1)** better at autonomous real work of any level,
> **(2)** more useful to people, or **(3)** evolve faster/better? If none → reject.

How it pursues this: researches and *surpasses* other agents and papers, mines its
own real sessions for what blocks the user, triages every issue (its own and the
community's) against the mission, then implements and self-updates through gated
PRs.

## 🔄 How it works

```
┌─────────────────────────────────────────────────────────────┐
│                    HERMES EVOLUTION AGENT                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  PUBLIC Mode (all installations):                           │
│  ┌──────────────────┐         ┌──────────────────┐         │
│  │ DAILY RESEARCH   │────────▶│  ISSUE/PR CREATE │         │
│  │  (24h cron)      │         │  (read-only)     │         │
│  └──────────────────┘         └──────────────────┘         │
│           │                                                   │
│           ▼                                                   │
│    [Change proposals]                                        │
│           │                                                   │
│  PRIVATE Mode (owner only):                                  │
│           ▼                                                   │
│  ┌──────────────────┐         ┌──────────────────┐         │
│  │ ISSUE ANALYSIS   │────────▶│  IMPLEMENTATION  │         │
│  │  (24h cron)      │         │  (write + merge) │         │
│  └──────────────────┘         └──────────────────┘         │
│                                         │                    │
│                                         ▼                    │
│                                  ┌─────────────┐            │
│                                  │ SELF-UPDATE │            │
│                                  │ + RESTART   │            │
│                                  └─────────────┘            │
└─────────────────────────────────────────────────────────────┘
```

## 📅 Daily cycle

Schedules below mirror `cron/evolution/*.yaml` (the source of truth).

| Time | Stage | Mode |
|------|-------|------|
| 07:47 daily | Watchdog — deterministic pipeline health check (no LLM) | PUBLIC |
| 08:00 Mon/Wed/Fri | Sync with upstream Hermes Agent | PRIVATE |
| 09:00 daily | Research other agents and papers | PUBLIC |
| 12:00 daily | Create issues/PRs from proposals | PUBLIC |
| 20:00 daily | Introspection — self-observed weaknesses | PRIVATE |
| 21:00 daily | Analyze and prioritize issues | PRIVATE |
| 22:00 daily | Implement improvements (open PRs) | PRIVATE |
| 23:00 daily | Integration — merge green PRs + self-update | PRIVATE |

## 🆚 Differences from the original Hermes Agent

| Capability | Hermes Agent | Hermes Evolution |
|------------|--------------|------------------|
| Core agent capabilities | ✅ | ✅ |
| Skills & Tools | ✅ | ✅ |
| Cron Jobs | ✅ | ✅ |
| **Evolution skills** | ❌ | ✅ |
| **Automated research** | ❌ | ✅ |
| **Automated issue creation** | ❌ | ✅ |
| **Priority analysis** | ❌ | ✅ |
| **Self-update** | ❌ | ✅ |
| **Upstream sync** | ❌ | ✅ |

## 🚀 Installation

### 1. Clone

```bash
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution
```

### 2. Configure

```bash
# Detect the operating mode
python evolution/detect_mode.py

# PUBLIC mode (all users)
export GITHUB_TOKEN="your..."

# PRIVATE mode (repository owner)
export GITHUB_PRIVATE_TOKEN="your..."
```

### 3. Register the evolution cron jobs

The evolution stages ship as YAML under `cron/evolution/*.yaml`. Register them
into the native Hermes scheduler with the canonical registrar (it self-locates
the install's venv interpreter, so any python works):

```bash
python scripts/register_evolution_cron.py
```

Re-running it is idempotent: new stages are added and changed schedules/prompts
are reconciled in place. On a normal install this runs automatically as part of
`hermes update` (see `upgrade.sh`).

## 📚 Evolution Skills

### evolution/research
Researches other agents, papers, and trends to generate ideas.

### evolution/issues
Creates GitHub issues and PRs with proposals.

### evolution/analysis
Analyzes issues and prioritizes them for implementation (PRIVATE only).

### evolution/implementation
Implements selected changes and self-updates (PRIVATE only).

### evolution/upstream-sync
Synchronizes with the upstream Hermes Agent (PRIVATE only).

## 🔐 Operating modes

### PUBLIC Mode
- ✅ Research
- ✅ Issue/PR creation
- ❌ PR merge
- ❌ Code modification

### PRIVATE Mode
- ✅ Everything in PUBLIC mode
- ✅ PR merge
- ✅ Code modification
- ✅ Self-update

## 🔄 Upstream sync

Hermes Evolution regularly synchronizes with the original Hermes Agent:

1. Fetches upstream changes (analyzed at the merged-PR level, with the raw
   commit log as a fallback)
2. Evaluates each change
3. Determines integration priority
4. Opens proposals for conflicting changes
5. Integrates compatible changes — bounded per run, through a separate branch +
   PR + CI (never a wholesale direct merge)

The upstream release tag we have synced through is recorded in
`.evolution/upstream-sync-state.json` and mirrored into `__release_date__`
(`hermes_cli/__init__.py`) so our version corresponds to upstream's.

## 🛡️ Safe self-evolution gate

The agent writes code autonomously. Without a gate, broken or injected code
would reach `main` and auto-update would propagate it to every installation
within 24h. So merges are controlled by **infrastructure, not the LLM's own
judgement**:

1. **PR-only.** `evolution-implementation` only opens PRs (`gh pr create`) and
   never runs `git merge` / `git checkout main`. Direct merge is forbidden.
2. **CI gate.** Every PR into `main` runs `.github/workflows/tests.yml` and
   `lint.yml`. Red tests = merge blocked.
3. **Critical-path protection.** `.github/CODEOWNERS` requires owner review for
   PRs touching self-update, the scheduler, CI, or evolution skills.
4. **Auto-update pulls only CI-protected `main`** — the official `hermes update`
   (origin = our fork) updates onto code that has already passed the gate.

### Enable branch protection (REQUIRED)

Without branch protection, "PR-only" is just an instruction the LLM could
bypass. The repository owner enables enforcement:

```bash
gh api -X PUT repos/Lexus2016/hermes-agent-evolution/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["Tests"] },
  "enforce_admins": true,
  "required_pull_request_reviews": { "require_code_owner_reviews": true,
    "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

- `contexts: ["Tests"]` — the check name from `tests.yml` (`name: Tests`); add
  others (e.g. lint) by their actual names in Actions.
- `require_code_owner_reviews` + `count: 0` — ordinary PRs merge on green CI
  without review (autonomy), while PRs touching `CODEOWNERS` critical paths
  still require owner approval.
- For a full "human in the loop", set `required_approving_review_count: 1`.

> ⚠️ Without this step the gate is incomplete: the skill says "PR only", but
> nothing technically stops the agent from merging directly.

## 🤖 Bot account for the agent (for critical PRs)

Branch protection forbids a PR author from approving their own PR. If the agent
pushes under the owner's account, the owner cannot review the agent's PRs to
critical paths (`CODEOWNERS`) — they would hang forever. So the agent should act
under a SEPARATE bot account.

### Setup (once)

1. **Create a separate GitHub account** for the bot (e.g. `hermes-evo-bot`). A
   human does this — the agent does not create accounts.
2. **Add the bot as a collaborator** with write access (`Settings → Collaborators`).
3. **Create a fine-grained PAT** as the bot, scoped to ONLY this repo:
   - Repository access: only `hermes-agent-evolution`
   - Permissions: Contents (RW), Pull requests (RW), Issues (RW), Workflows (RW) — and nothing else.
4. **Configure the server to act as the bot** (token via env, not an argument):
   ```bash
   export GITHUB_EVOLUTION_TOKEN=<bot-pat>
   bash scripts/setup_evolution_bot.sh
   ```
   The script logs `gh` in as the bot, wires it as a git credential, and sets
   the git identity. The token is never printed.

### How it works from there

- The agent opens PRs as `hermes-evo-bot` → you (owner + code owner) review
  critical PRs and merge them; ordinary PRs merge on green CI without review.
- The bot token is scoped to one repo → even if the agent is compromised (via
  injection), an attacker cannot reach your other repositories.

> Store the bot PAT in a secrets vault / env with `chmod 600`, NOT in code or a
> git URL.

## 📖 Documentation

- [AGENTS.md](AGENTS.md) — Hermes Agent documentation (original)
- [SECURITY_EVOLUTION.md](SECURITY_EVOLUTION.md) — self-evolution threat model & gate
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute

## 🤝 Contributing

Contributions are welcome! Before opening a PR:

1. Check [CONTRIBUTING.md](CONTRIBUTING.md)
2. Run the tests: `python scripts/run_tests_parallel.py` (the canonical parallel
   runner; `pytest tests/` directly does not isolate cross-file state)
3. Update the documentation

## 📄 License

MIT (see `LICENSE`; built on [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)).

## 🙏 Acknowledgements

- [Nous Research](https://nousresearch.com/) — the original Hermes Agent
- All Hermes Agent contributors

---

**This is an experiment in self-improving AI systems.** ⚗️
