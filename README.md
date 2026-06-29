# Hermes Evolution 🧬

> A self-improving version of **Hermes Agent** — it researches improvements,
> proposes them, and updates itself daily. You keep using Hermes as usual; it
> gets better on its own.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Based on Hermes Agent](https://img.shields.io/badge/based%20on-Hermes%20Agent-blue.svg)](https://github.com/nousresearch/hermes-agent)
**The self-improving AI agent built by [Nous Research](https://nousresearch.com).** It's the only agent with a built-in learning loop — it creates skills from experience, improves them during use, nudges itself to persist knowledge, searches its own past conversations, and builds a deepening model of who you are across sessions. Run it on a $5 VPS, a GPU cluster, or serverless infrastructure that costs nearly nothing when idle. It's not tied to your laptop — talk to it from Telegram while it works on a cloud VM.

Use any model you want — [Nous Portal](https://portal.nousresearch.com), OpenRouter, OpenAI, your own endpoint, and [many others](https://hermes-agent.nousresearch.com/docs/integrations/providers). Switch with `hermes model` — no code changes, no lock-in.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Six terminal backends — local, Docker, SSH, Singularity, Modal, and Daytona. Daytona and Modal offer serverless persistence — your agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions. Run it on a $5 VPS or a GPU cluster.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

---

## ⭐ Install or upgrade — one command

Whether you're **starting from scratch** or **already running Hermes**, paste
**one line** into your terminal. If Hermes isn't installed yet, it installs our
version fresh; if it is, it switches your existing Hermes onto Evolution
(your chats, memory, and settings are kept):

```bash
curl -fsSL https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/upgrade.sh | bash
```

That's it. The script does everything for you, safely:
- backs up your data (nothing is lost),
- switches your Hermes to this version,
- turns on the evolution features,
- sets up **daily auto-updates** so it keeps improving on its own.

You can re-run it any time — it won't break anything. Your existing chats,
memory, and settings stay exactly as they were.

> Don't want unattended daily updates? Add `--no-star` and/or `--no-auto-update`
> when you run it (from a clone): `bash upgrade.sh --no-auto-update`.

### Troubleshooting

#### Windows Defender or antivirus flags `uv.exe` as malware

If your antivirus (Bitdefender, Windows Defender, etc.) quarantines `uv.exe` from the Hermes `bin` folder (`%LOCALAPPDATA%\hermes\bin\uv.exe`), this is a **false positive**. The file is Astral's `uv` — the Rust Python package manager Hermes bundles to manage its Python environment. ML-based antivirus engines commonly flag unsigned Rust binaries that download and install packages.

**To verify your copy is authentic:**

```powershell
# Install GitHub CLI if needed
winget install --id GitHub.cli

# Login to GitHub
gh auth login

# Run verification
$uv = "$env:LOCALAPPDATA\hermes\bin\uv.exe"
$ver = (& $uv --version).Split(' ')[1]
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zip = "$env:TEMP\uv.zip"
Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip -UseBasicParsing
gh attestation verify $zip --repo astral-sh/uv
Expand-Archive $zip "$env:TEMP\uv_x" -Force
(Get-FileHash "$env:TEMP\uv_x\uv.exe").Hash -eq (Get-FileHash $uv).Hash
```

If attestation says "Verification succeeded" and the last line prints `True`, you're good.

**To whitelist Hermes:**
- **Windows Defender:** Run PowerShell as Admin → `Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\hermes\bin"`
- **Bitdefender:** Add an exception in the Bitdefender console (Protection > Antivirus > Settings > Manage Exceptions)
- Whitelist the **folder**, not the file hash — Hermes updates `uv` and the hash changes every version

For more context, see the upstream Astral reports: [astral-sh/uv#13553](https://github.com/astral-sh/uv/issues/13553), [astral-sh/uv#15011](https://github.com/astral-sh/uv/issues/15011), [astral-sh/uv#10079](https://github.com/astral-sh/uv/issues/10079).

---

## 🔑 Set up a GitHub token

The agent needs a GitHub token to work with GitHub. There are **two cases** —
pick the one that's you. (~2 minutes, no coding.)

### 👤 Regular user — let the agent open improvement *issues*

The agent opens issues on the **shared Hermes Evolution repo**. That repo isn't
yours, so a fine-grained token can't target it — use a **classic** token with
the `public_repo` scope:

1. Open **[github.com/settings/tokens/new](https://github.com/settings/tokens/new)**
   — this is *Personal access token (classic)*.
2. **Note:** `hermes-evolution`. **Expiration:** 90 days (or longer).
3. Tick **`public_repo`** (under **repo** → “Access public repositories”). That's
   the only box you need — it lets the agent open issues/PRs on public repos.
4. Click **Generate token** → **copy** it (starts with `ghp_…`; shown once).
5. Give it to your agent:
   ```bash
   echo 'GITHUB_TOKEN=PASTE_YOUR_TOKEN_HERE' >> ~/.hermes/.env
   ```

Done — your Hermes can now research and open improvement issues.

### 🛠️ Repo owner — full self-evolution (issues, PRs, commits, pushes, releases)

If you **own** `hermes-agent-evolution`, the agent can also implement changes,
open pull requests, push branches, and cut releases. You own the repo, so use a
**fine-grained** token scoped to it:

1. Open **[github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)**
   — this is the *Fine-grained token* page.
2. **Token name:** `hermes-evolution-owner`. **Expiration:** 90 days (or longer).
3. **Repository access:** *Only select repositories* → pick
   **`hermes-agent-evolution`** (you can select it because it's yours).
4. **Permissions → Repository permissions** — set to **Read and write**:
   - **Contents** — commits, pushes, tags **and releases**
   - **Issues**
   - **Pull requests**
5. Click **Generate token** → **copy** it (starts with `github_pat_…`; shown once).
6. Give it to your agent. The owner runs the **full** cycle, which uses **two
   roles**, so set **both** env vars (this is the part people miss):
   ```bash
   # PRIVATE role — analysis, implementation, push, PRs, releases (owner):
   echo 'GITHUB_PRIVATE_TOKEN=PASTE_OWNER_TOKEN_HERE' >> ~/.hermes/.env
   # PUBLIC role — research + opening issues reads GITHUB_TOKEN.
   # Simplest: reuse the SAME owner token here too.
   echo 'GITHUB_TOKEN=PASTE_OWNER_TOKEN_HERE' >> ~/.hermes/.env
   ```
   > **Why both?** The agent forces the right token per role: issues/research use
   > `GITHUB_TOKEN`, while analysis/implementation use `GITHUB_PRIVATE_TOKEN`. If
   > only one is set, half the cycle silently does nothing. Reusing the same owner
   > token in both is fine. For a clean *"proposals come from a separate account"*
   > split, put a **second** account's classic `public_repo` token in
   > `GITHUB_TOKEN` instead (see the *Regular user* section above) — but never swap
   > the two: `GITHUB_TOKEN` must be the proposer, `GITHUB_PRIVATE_TOKEN` the owner.

> Keep tokens private — treat them like passwords. Never share them or paste them
> into a chat. If one leaks, delete it on GitHub and create a new one.

---

## 🧬 What you get

- **Daily research** — scans other AI agents, papers, and trends for ideas.
- **Introspection** — reviews its own past sessions with you to find what
  blocked real tasks, and proposes fixes for *those* (not just shiny features).
- **Proposals** — opens GitHub issues with concrete improvement suggestions.
- **Self-update** — pulls the latest improvements automatically every day.
- **Stays current** — periodically brings in useful changes from the original
  Hermes Agent.

Everything you already love about Hermes Agent still works exactly the same.

---

## 🆕 Starting from scratch?

No need to install the original Hermes first — the one command above installs
**our fork directly** (you do NOT end up on the original and then migrate).
It pulls Hermes Evolution, sets it up, and turns on the evolution features in
one go.

Windows works natively too — a PowerShell installer (`scripts/install.ps1`) is
included; see **[AUTO_UPGRADE.md](AUTO_UPGRADE.md)**.

---

## 🛡️ Is it safe?

Yes. The agent can **propose** changes but cannot silently rewrite itself:
every change goes through a pull request with automated tests, and important
parts require your approval before they're merged. Updates are backed up and
roll back automatically if anything looks wrong.

Details: **[EVOLUTION_README.md](EVOLUTION_README.md)** ·
**[SECURITY_EVOLUTION.md](SECURITY_EVOLUTION.md)**

---

## 📖 Learn more

| Document | What's inside |
|----------|---------------|
| **[AUTO_UPGRADE.md](AUTO_UPGRADE.md)** | Install/upgrade in detail, Windows, manual steps |
| **[EVOLUTION_README.md](EVOLUTION_README.md)** | How evolution works, modes, the safety gate |
| **[SECURITY_EVOLUTION.md](SECURITY_EVOLUTION.md)** | Security policy |
| **[AGENTS.md](AGENTS.md)** | Original Hermes Agent documentation |

---

## 📄 License & credits

MIT License (see `LICENSE`). Built on **[Hermes Agent](https://github.com/nousresearch/hermes-agent)**
by [Nous Research](https://nousresearch.com/) — huge thanks to them and the
Hermes community.

- **Repository:** [github.com/Lexus2016/hermes-agent-evolution](https://github.com/Lexus2016/hermes-agent-evolution)
- **Upstream:** [github.com/nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)
