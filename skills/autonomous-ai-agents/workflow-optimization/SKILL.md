---
name: workflow-optimization
description: "Hybrid workflow optimization recipe: select > generate > edit for delegation and skill routing decisions."
version: 1.0.0
author: Hermes Evolution
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [workflow, delegation, routing, subagents, skills, orchestration]
    related_skills: [hermes-agent]
---

# Workflow Optimization (select > generate > edit)

Before spawning a subagent, choosing a skill, or changing an in-flight plan,
apply this cost-ordered hierarchy. It matches the cheapest form of workflow
plasticity to the minimum need, so cheap reuse is the default and expensive
mutation is the exception.

The cost ordering is **select < generate < edit**:

1. **SELECT (cheapest)** — reuse an existing static asset: a matching skill, an
   installed plugin, or a proven prior delegation configuration. Selecting from
   existing structure costs nothing to design.
2. **GENERATE (medium)** — create a new structure (a new subagent configuration,
   a new tool composition, a new skill) only when no existing asset covers the
   need.
3. **EDIT (most expensive)** — mutate the plan or graph mid-execution. Reserve
   this strictly for genuine uncertainty or a runtime anomaly, never for
   routine course changes.

## When to Use

- Any `delegate_task` decision: choosing whether to spawn a subagent and how to
  configure it.
- Any skill-routing decision: picking which skill (if any) to load for a task.
- Any point where the plan is about to change mid-execution.

## Decision Procedure

Run this checklist top to bottom; stop at the first tier that produces a
confident match.

### Tier 1 — SELECT first (always try this before anything else)

- Does an existing skill cover the task? Load it instead of generating a new
  plan or a new subagent. Prefer `skill_view` over reasoning from scratch.
- Does an existing plugin or tool already do the job? Extend, don't duplicate.
- Is there a proven prior delegation configuration (a task-type + role + context
  that succeeded before)? Reuse it verbatim rather than re-designing the
  subagent from scratch.
- Only advance to Tier 2 when no existing asset matches with confidence.

### Tier 2 — GENERATE only when selection fails

- Spawn a new subagent (or author a new tool composition) only after Tier 1 came
  up empty or the existing asset demonstrably failed the task.
- Prefer node-level tuning first: refine the prompt, tighten the tool
  description, or adjust parameters of an existing structure before inventing a
  new one. Graph-level change (new skills, new tool compositions, new routing)
  is the last move, not the first.
- When you do generate, record what you built so it becomes a Tier-1 asset next
  time.

### Tier 3 — EDIT only under genuine uncertainty

- Change an in-flight plan only when you actually face an anomaly: a wrong
  path, a missing prerequisite that just surfaced, or a confidence collapse.
- Routine course corrections are not a reason to edit the plan — a stable plan
  that is merely slow or imperfect should be left to run, not re-planned.
- If you must edit, make the smallest change that resolves the uncertainty; do
  not rewrite the whole plan.

## Pitfalls

- **Generating before selecting** is the common failure: a new subagent or a new
  skill is authored for a task an existing asset already covers. Check Tier 1
  explicitly and say what you found before spawning.
- **Graph search for parametric problems.** When failures are parametric (a weak
  prompt, a bad threshold), tuning the node beats restructuring the graph.
  Reserve structural change for structural failures (missing verifier, wrong
  path).
- **Editing for routine reasons.** Re-planning a still-valid plan burns context
  and, in a cached conversation, can be far more expensive than it looks.
- **Re-implementing instead of extending.** If several tasks want the same new
  capability, build one shared asset, not N near-duplicate subagent configs.

## Verification

- The delegation/skill choice can be traced to a specific Tier-1 asset, or to an
  explicit "no asset matched" finding that justified Tier 2.
- No new subagent or skill was created while a usable existing asset sat unused.
- Any mid-execution plan change names the concrete uncertainty that forced it.
