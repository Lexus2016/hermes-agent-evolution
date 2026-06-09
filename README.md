# Hermes Evolution 🧬

> A self-improving version of **Hermes Agent** — it researches improvements,
> proposes them, and updates itself daily. You keep using Hermes as usual; it
> gets better on its own.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Based on Hermes Agent](https://img.shields.io/badge/based%20on-Hermes%20Agent-blue.svg)](https://github.com/nousresearch/hermes-agent)

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

Apache License 2.0. Built on **[Hermes Agent](https://github.com/nousresearch/hermes-agent)**
by [Nous Research](https://nousresearch.com/) — huge thanks to them and the
Hermes community.

- **Repository:** [github.com/Lexus2016/hermes-agent-evolution](https://github.com/Lexus2016/hermes-agent-evolution)
- **Upstream:** [github.com/nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)
