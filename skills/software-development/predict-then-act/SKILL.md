---
name: predict-then-act
description: "Use when an agent takes consequential actions in an unfamiliar or
  partially-known environment (trading, infra changes, game APIs, external
  systems): write a falsifiable prediction of the expected outcome BEFORE each
  action, have the harness grade it, and keep a one-page notes file that
  survives context compaction. Proven at 100% RHAE on ARC-AGI-3 (25/25 games,
  7,645 actions vs human median 17,135) by the arc-skill project
  (https://github.com/pbshgthm/arc-skill)."
version: 1.0.0
author: Hermes Agent (adapted from pbshgthm/arc-skill, MIT)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [agent-methodology, reliability, decision-making]
---

# Predict, Then Act

One rule: **an action must arrive with a written, falsifiable prediction of its
outcome.** The prediction is graded against reality after the action. This turns
every action into an experiment and every miss into dated evidence.

## Why it works

Across the ARC-AGI-3 campaign: single exploratory actions missed their
predictions **37.1%** of the time; actions inside a predicted plan missed only
**2.9%**. The gap is the value — you learn cheaply where beliefs are wrong,
then commit only through sequences whose every step is claimed in advance and
halts on the first surprise.

## The loop

1. **Look** — read current state (output, logs, balances, positions).
2. **Predict** — write what the action will change, concretely enough that the
   next observation can contradict it.
3. **Act** — execute with the prediction attached.
4. **Compare** — grade prediction vs outcome. A miss is the most valuable
   result: reality just corrected you for one action's cost.
5. **Note** — update the notes file before the next action.

## Claim vocabulary

Small grammar; every form must be checkable against one future observation:

| Form | Meaning | Example |
|---|---|---|
| `value PATH=X` | this field will hold this value | `balance USDT=1000` |
| `delta PATH +/-N` | this metric moves by this much | `pnl -50` |
| `exists NAME` / `vanish NAME` | object appears / disappears | `order 123 filled` |
| `state NAME=S` | system enters this state | `position=closed` |
| `noop` / `change` | nothing / something changes | sanity checks |

Free text is allowed but grades weaker than a specific claim.

## Batching proven mechanics

Once a mechanism is verified, batch steps with a claim on **every step**;
execution halts at the first miss so a wrong theory cannot burn the queue.
Never batch exploration.

## One-page notes (`NOTES.md`)

Context compaction will erase your reasoning. Keep ONE page per task/system:

```markdown
## Verified (cite event/log ids)
- fact — evidence
## Assumed / open questions
- guess — how to test it
## REFUTED
- dead belief — the evidence that killed it
## Plan
- next concrete steps
```

The page cannot grow forever, so observations must compress into rules.
Median length ~60 lines works.

## Tight rules, free thinking

- **The gate is hard.** No falsifiable claim → no action. Not a prompt
  suggestion; an enforced refusal.
- **The thinking is free.** Any representation, any tool, any escalation
  (rules → executable model → search). The skill never dictates HOW to model,
  only THAT claims are made and graded.

## Applying outside ARC (Hermes contexts)

- **Trading**: before placing an order, predict fill price/slippage band and
  resulting position state; grade against the exchange response.
- **Infra/deploys**: predict the exact health-check output and one metric that
  must not move; rollback if either misses.
- **Long migrations**: NOTES.md per migration; REFUTED section prevents
  re-testing dead hypotheses after compaction.
- **Subagents**: pass the current NOTES.md content, not conversation history.

Adapted from [pbshgthm/arc-skill](https://github.com/pbshgthm/arc-skill)
(MIT), which achieved 100.00 RHAE on ARC-AGI-3 with Claude Code + Opus 5,
verified by ARC scorecard `24ddb219`.
