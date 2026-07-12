---
name: a2a
description: |
  Agent2Agent (A2A) interoperability for Hermes. Slice 1 exposes Hermes'
  existing tools and skills as an A2A Agent Card served at
  /.well-known/agent.json, so other A2A-speaking agents can discover what
  this Hermes instance can do. Discovery only -- no A2A JSON-RPC server yet.
version: 0.1.0
metadata:
  hermes:
    tags: [a2a, interoperability, agent-card, discovery]
    category: interoperability
    related_skills: [autonomous-ai-agents]
---

# A2A Agent Card (discovery)

Hermes advertises itself to the [Agent2Agent (A2A)](https://google.github.io/A2A/)
ecosystem with an **Agent Card**: a small JSON document that describes the
agent's identity, endpoint, auth methods, protocol capabilities, and the list
of things it can do (its "skills").

This slice (issue #879, child of #748) implements **discovery only**:

- The Agent Card data model lives in `hermes_cli/a2a.py` (`AgentCard`,
  `AgentSkill`, `build_agent_card`).
- The dashboard web server (`hermes_cli/web_server.py`) serves the card at
  **`GET /.well-known/agent.json`**.
- Each existing Hermes tool and each `SKILL.md` skill is mapped to one entry
  in the card's `skills[]` array. **No new core tools are registered** -- the
  card is a read-only view over capabilities Hermes already has.

## Configuration

All behaviour is driven by [`config.yaml`](config.yaml) in this directory,
which overlays the defaults baked into `hermes_cli/a2a.py`. Highlights:

- `enabled: false` makes the endpoint return `404` (Hermes stops advertising).
- `name` / `description` / `provider` set the card's identity.
- `url` is the A2A service endpoint; a relative value (`/a2a`) is resolved
  against the incoming request host so no hostname is hardcoded.
- `authentication.schemes` lists the advertised auth methods.
- `expose.tools` / `expose.skills` / `expose.max_skills` control which
  capabilities are surfaced and the cap on the total.

## Scope note

The card is a **public** discovery document by design (A2A clients hold no
dashboard session). It contains only tool/skill names and one-line
descriptions -- never secrets or config values. The A2A JSON-RPC server that
actually fulfils tasks (`url`), plus streaming and push notifications, are
later slices (#880 client, #881 server).
