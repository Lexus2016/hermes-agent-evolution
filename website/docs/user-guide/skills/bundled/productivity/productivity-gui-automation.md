---
title: "Gui Automation — Safe, element-anchored cross-platform GUI automation"
sidebar_label: "Gui Automation"
description: "Safe, element-anchored cross-platform GUI automation"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Gui Automation

Safe, element-anchored cross-platform GUI automation.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/productivity/gui-automation` |
| Version | `1.0.0` |
| Author | Hermes Evolution |
| License | MIT |
| Platforms | linux, macos, windows |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# GUI Automation & On-Screen Element Understanding

This skill enables Hermes to interact with native and virtual desktop applications using structured on-screen element trees and safe execution primitives.

## Workflow

1. **Scan Screen & Detect Elements**:
   - Invoke `gui_elements()` to retrieve the list of accessible UI elements (buttons, inputs, text fields, tabs) with stable element IDs.
2. **Plan & Execute Targeted Actions**:
   - Use `gui_act(element_id=..., action="click" | "type" | "focus" | "scroll", text=...)` to interact directly with discrete element IDs rather than fragile raw pixel coordinates.
3. **Verify State**:
   - Call `gui_screenshot()` to capture the result of the interaction and visually verify completion.
