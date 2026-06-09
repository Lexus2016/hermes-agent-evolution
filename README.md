# Hermes Evolution 🧬

> A self-improving version of **Hermes Agent** — it researches improvements,
> proposes them, and updates itself daily. You keep using Hermes as usual; it
> gets better on its own.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Based on Hermes Agent](https://img.shields.io/badge/based%20on-Hermes%20Agent-blue.svg)](https://github.com/nousresearch/hermes-agent)

---

## ⭐ Already running Hermes? Upgrade in one command

If you installed Hermes Agent and went through its setup wizard, switch to
Hermes Evolution by pasting **one line** into your terminal:

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

## 🔑 One thing to set up: a GitHub token

For the agent to research and open improvement **issues** on GitHub, it needs a
GitHub token. Here's how to get one (takes ~2 minutes, no coding):

1. Open **[github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)**
   (log in if asked). This is the **Fine-grained token** page.
2. **Token name:** type `hermes-evolution`.
3. **Expiration:** pick 90 days (or longer).
4. **Repository access:** choose **“Only select repositories”** → pick
   **`hermes-agent-evolution`**.
5. **Permissions → Repository permissions**, set these three to **Read and write**:
   - **Contents**
   - **Issues**
   - **Pull requests**
6. Click **Generate token** at the bottom, then **copy** the token
   (it starts with `github_pat_…`). Copy it now — GitHub shows it only once.
7. Give it to your agent — paste this one line (replace the placeholder):

   ```bash
   echo 'GITHUB_TOKEN=PASTE_YOUR_TOKEN_HERE' >> ~/.hermes/.env
   ```

Done. From now on your Hermes can research and open improvement issues on GitHub.

*That's all most people need. If you own the fork and want the agent to also
implement changes and update the project itself, that uses a separate owner
token — see [EVOLUTION_README.md](EVOLUTION_README.md).*

> Keep the token private — treat it like a password. Never share it or paste it
> into a chat. If it ever leaks, delete it on GitHub and make a new one.

---

## 🧬 What you get

- **Daily research** — scans other AI agents, papers, and trends for ideas.
- **Proposals** — opens GitHub issues with concrete improvement suggestions.
- **Self-update** — pulls the latest improvements automatically every day.
- **Stays current** — periodically brings in useful changes from the original
  Hermes Agent.

Everything you already love about Hermes Agent still works exactly the same.

---

## 🆕 Don't have Hermes yet?

Install the original Hermes Agent first, then run the one-command upgrade above:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
# then the upgrade line from the top of this README
```

Windows works natively too — see **[AUTO_UPGRADE.md](AUTO_UPGRADE.md)**.

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
