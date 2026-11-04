# Hermes Evolution 🧬

**A Hermes Agent fork with scheduled research, improvement proposals and an owner-operated development pipeline.**

This repository adds evolution workflows to [Hermes Agent](https://github.com/nousresearch/hermes-agent)
by Nous Research. It is maintained separately in the Lexus2016 fork. Available
actions depend on configuration and GitHub permissions; installing it does not
guarantee that every workflow succeeds or that each update improves your workload.

[Install and upgrade](#-install-or-upgrade) · [Evolution workflow](EVOLUTION_README.md) · [Contribute](CONTRIBUTING_EVOLUTION.md) · [Report a bug](https://github.com/Lexus2016/hermes-agent-evolution/issues/new/choose)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Based on Hermes Agent](https://img.shields.io/badge/based%20on-Hermes%20Agent-blue.svg)](https://github.com/nousresearch/hermes-agent)
**Based on the agent built by [Nous Research](https://nousresearch.com).** Hermes provides
memory, skills, session search, a terminal interface and messaging integrations.
This fork adds the evolution workflow described below. Hosting requirements and
provider costs depend on the configuration you choose.

Use any model you want — [Nous Portal](https://portal.nousresearch.com), OpenRouter, OpenAI, your own endpoint, and [many others](https://hermes-agent.nousresearch.com/docs/integrations/providers). Switch with `hermes model` — no code changes, no lock-in.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Seven terminal backends — local, Docker, SSH, Singularity, Modal, Daytona, and Vercel Sandbox. Daytona and Modal offer serverless persistence — your agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions. Run it on a $5 VPS or a GPU cluster.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

---

## ⭐ Install or upgrade

Review [AUTO_UPGRADE.md](AUTO_UPGRADE.md) and [upgrade.sh](upgrade.sh) before running
the installer. It can install the fork or switch an existing Hermes checkout to it,
refresh evolution skills, register scheduled evolution jobs and restart a running
gateway after a code update. Those jobs may make model calls or GitHub changes
according to the credentials and mode you configure.

The default script also attempts to schedule daily code updates and star this
repository using available GitHub authentication. To skip those two optional
actions, use both flags:

```bash
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution
# Inspect upgrade.sh and the linked installation guide first.
bash upgrade.sh --no-auto-update --no-star
```

`--no-auto-update` skips creation of the separate daily updater; it does **not**
disable evolution jobs or remove an updater that was already installed. `--no-star`
skips the star attempt on this run. There is no read-only preview in this command.

Back up your data independently before upgrading. The script attempts a data
backup, but continues if it fails and retains only the three newest matching
backups. Review its output and verify the installation before relying on it.

### Windows & Termux

> **Windows:** Native Windows is supported via `scripts/install.ps1`; see **[AUTO_UPGRADE.md](AUTO_UPGRADE.md)**.
>
> **Android / Termux:** Use the [Termux guide](https://hermes-agent.nousresearch.com/docs/getting-started/termux).

### Troubleshooting

#### Running Hermes in Docker?

The container image is built **without** a git working tree (`.git` is excluded
from the build context), so there are no remotes to repoint from the inside —
the same reason `hermes update` refuses to run there. You have two options.

**A. Rebuild the image on the host** — durable, survives container recreate:

```bash
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution
docker build -t hermes-agent-evolution:latest .

# point your compose file / run command at that image, then:
docker compose up -d --force-recreate
```

**B. Upgrade the running container in place** — when you only have a shell
inside it (NAS or panel deployments, no host docker, no build daemon):

```bash
docker exec -u 0 -it <container> bash
curl -fsSL https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/upgrade.sh | bash -s -- --in-container
docker restart <container>   # or let the script bounce the s6 service
```

This works because the image installs Hermes as an **editable** checkout
(`uv pip install -e .`), so the virtualenv imports straight out of
`/opt/hermes` — replacing the source tree *is* the upgrade. The script turns
`/opt/hermes` into a real git checkout of this fork; everything the repo does
not track (`.venv/`, `node_modules/`, the `bin/hermes` shim,
`hermes_cli/web_dist/`) is left untouched.

Two caveats for option B:

- The change lives in the container's **writable layer**. `docker restart`
  keeps it; recreating the container from the image (`compose up
  --force-recreate`, an image pull) reverts to stock Hermes — which is also
  your rollback. Re-run the command to reapply.
- There is **no daily self-update** inside the container (the image ships no
  cron). Re-run the same command whenever you want the latest commit; after
  the first run it is a fast `git fetch`.

In both cases your config, chats and memory live on the `$HERMES_HOME` volume
(`/opt/data`) and are never touched.

#### Windows Defender or antivirus flags `uv.exe` as malware

Do not assume an alert is a false positive or disable protection for the Hermes
folder. Keep the file quarantined while you check its origin and the exact vendor
release. The package manager used by Hermes is [Astral uv](https://github.com/astral-sh/uv),
but a filename alone does not establish that a particular executable is authentic.

Use the official release's verification instructions and your antivirus vendor's
review process. If you report an installation problem, include the installer
version and detection name, without credentials or private files.

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

- **Scheduled research** — looks for improvement ideas; current schedules are
  documented in [EVOLUTION_README.md](EVOLUTION_README.md#-daily-cycle).
- **Introspection** — reviews its own past sessions with you to find what
  blocked real tasks, and proposes fixes for *those* (not just shiny features).
- **Proposals** — opens GitHub issues with concrete improvement suggestions.
- **Self-update** — pulls the latest improvements automatically every day.
- **Stays current** — periodically brings in useful changes from the original
  Hermes Agent.

- **300+ models & Tool Gateway** — all upstream models, web search, image gen, TTS, cloud browser.
- **Plus everything in Hermes Agent** — CLI, messaging gateway, TUI, Desktop app, skills, MCP, cron.

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

Evolution can run code, use paid models, publish GitHub changes and update the
installed application. The available actions depend on token permissions, mode,
scheduled jobs and repository controls. The workflow includes tests and gates,
but these are not a guarantee against failures, data loss or unsafe changes.

Review the active configuration and credentials, maintain independent backups,
and test updates before enabling unattended work on important data.

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

Start with the [PM developer workflow](website/docs/reference/package-management.md#developer-workflow)
for activation, daily use, dependency changes, and leaving the environment.
[Development Setup](CONTRIBUTING.md#development-setup) covers the separate test environment and verification commands.

---

## Community

- 💬 [Discord](https://discord.gg/NousResearch)
- 📚 [Skills Hub](https://agentskills.io)
- 🐛 [Issues for this fork](https://github.com/Lexus2016/hermes-agent-evolution/issues/new/choose)
- 🛠 [Contributing to this fork](CONTRIBUTING_EVOLUTION.md)
- 🔒 [Security scope and reporting](SECURITY.md)
- 🔌 [computer-use-linux](https://github.com/avifenesh/computer-use-linux) — Linux desktop-control MCP server for Hermes and other MCP hosts, with AT-SPI accessibility trees, Wayland/X11 input, screenshots, and compositor window targeting.
- 🔌 [HermesClaw](https://github.com/AaronWong1999/hermesclaw) — Community WeChat bridge: Run Hermes Agent and OpenClaw on the same WeChat account.

---

## License

MIT — see [LICENSE](LICENSE).

Based on Hermes Agent by [Nous Research](https://nousresearch.com).
Evolution additions are maintained in this fork.
