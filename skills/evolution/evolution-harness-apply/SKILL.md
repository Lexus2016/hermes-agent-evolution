---
name: evolution-harness-apply
description: Manually trigger the gated apply-path for harness code-diff proposals (Harness-R1 Slice C).
version: 1.0.0
author: Hermes Evolution
license: MIT
platforms: [linux, macos, windows]
category: evolution
mode: PUBLIC
metadata:
  hermes:
    tags: [evolution, harness, apply, gate]
---

# Evolution Harness Apply Skill

**Operating mode:** PUBLIC (all installations)

Manual trigger for `scripts/evolution_harness_apply.py` (#2615): gated code-diff apply-path, human/agent invoked only, never a silent loop.
`--apply` writes the validated surface ONLY when the sandboxed regression gate is green. Exit codes: 0/1/2/3 = validated/rejected/invalid/refused.
