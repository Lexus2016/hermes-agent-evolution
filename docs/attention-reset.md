# Attention Reset Mechanism

## Overview

A lightweight, deterministic attention-reset ritual to prevent agents from fixating in long sessions. This mechanism breaks tunnel vision by forcing a deliberate pause and fresh perspective before continuing.

## Problem Statement

In long sessions, agents tend to lock onto their FIRST interpretation of a task and keep pushing the same approach even after it repeatedly fails — fixation / tunnel vision. Symptoms include:

- Re-running already-failed hypotheses
- Missing alternative angles
- Asserting "I already know the answer" without re-checking
- Burning cycles on wrong root-cause guesses

## Solution

A forced pause + small generative act + content-derived selection that breaks the inertia of the first framing:

1. Emit 10 fresh alphanumeric characters from output (no RNG tool, no reuse)
2. Calculate `position = (sum of all digit values in string) mod 10` (0-indexed)
3. Pick `string[position]`
4. Announce: `reset: <string> (digit-sum S, pos N): <char>`
5. Re-engage the task with fresh attention

## Why Content-Derived Index?

Random "pick any letter" empirically collapses to the same positional bias call after call. Deriving the index from the just-generated string forces an actual selection each time, not a pattern.

## When to Trigger

- **After >= 2 failed attempts** at the same problem
- **Before hard-to-reverse decisions** (schema/migration, merge, release, deploy)
- **When overconfident without verification** (agent "already knows" without checking)
- **When continuing prior reasoning** instead of looking fresh

## What It Is NOT

- Not a tie-breaker between options
- Not a post-hoc justification engine
- Not a scheduled no-op ritual

The value is the pause + small deliberate act, which interrupts the first interpretation.

## Usage

### Basic API

```python
from tools.attention_reset import AttentionReset, attention_reset_hook

# Initialize
reset = AttentionReset(failed_attempt_threshold=2, max_resets_per_task=5)

# Record failures
reset.record_failure("hypothesis_1_failed", hypothesis="Bug in parser")

# Check if should trigger
if reset.should_trigger_reset():
    result = reset.trigger_reset(seed_from_output="recent agent output")
    print(result.announcement)  # reset: abc123xyz (digit-sum 6, pos 6): x

# Record success (clears failure counter)
reset.record_success()
```

### Convenience Hook

```python
# Simple hook function
announcement = attention_reset_hook(
    failed_attempts=2,
    hypothesis="Bug is in parser",
    task_description="Debug test failure",
)

if announcement:
    print(f"Reset triggered: {announcement}")
```

### Before Irreversible Actions

```python
# Always reset before destructive operations
if reset.should_trigger_reset(before_irreversible=True):
    result = reset.trigger_reset()
    reset_prompt = reset.get_reset_prompt(result)
    # Show prompt to agent...
```

## Configuration

- `failed_attempt_threshold`: Trigger after N failed attempts (default: 2)
- `max_resets_per_task`: Maximum resets before abort recommendation (default: 5)

## Reset Prompt

When a reset is triggered, the agent receives:

```
reset: abc123xyz (digit-sum 6, pos 6): x

**You have just performed an attention reset.** 

Take a moment to breathe and step back from your previous line of reasoning.

Instructions:
1. Re-read the original task description from scratch
2. Consider alternative approaches you may have dismissed
3. Verify your previous assumptions against the actual problem
4. Look for evidence that contradicts your current hypothesis
5. If you still believe your approach is correct, explicitly state why
```

## Integration Example

```python
class AIAgent:
    def __init__(self):
        self.attention_reset = AttentionReset()
    
    def run_with_reset_protection(self, task):
        while True:
            hypothesis = self.generate_hypothesis()
            result = self.test_hypothesis(hypothesis)
            
            if result.success:
                self.attention_reset.record_success()
                return result
            
            self.attention_reset.record_failure(
                reason="hypothesis_failed",
                hypothesis=hypothesis,
                task_description=task,
            )
            
            if self.attention_reset.should_trigger_reset():
                reset_result = self.attention_reset.trigger_reset(
                    seed_from_output=self.recent_output
                )
                print(reset_result.announcement)
                # Agent receives reset prompt and reconsideres approach
            
            if self.attention_reset.should_abort_task():
                raise TaskAbortedError("Too many resets, aborting task")
```

## Value Proposition

- **Breaks fixation** — Agent reconsiders problem from multiple angles
- **Extremely cheap** — A few tokens, no external dependencies
- **Improves debugging** — Directly improves the evolution agent's own debugging quality
- **Prevents tunnel vision** — Forces fresh perspective on stuck problems

## Examples

### Example 1: Failed Debugging Attempts

```
Attempt 1: Check for syntax error → Failed
Attempt 2: Check for dependency issue → Failed
Trigger: Reset after 2 failures

Reset: a7b3c9d2e1 (digit-sum 22, pos 2): b
→ Agent re-reads the problem, notices actual error was in config file
```

### Example 2: Before Merge

```
Pre-merge check detected potential issue
Trigger: Reset before irreversible action

Reset: x4y2z8w1q (digit-sum 15, pos 5): q
→ Agent performs additional verification, catches critical bug
```

## Monitoring

```python
# Get current state
summary = reset.get_state_summary()
print(f"Failed attempts: {summary['failed_attempts']}")
print(f"Resets performed: {summary['reset_count']}")
print(f"Last trigger: {summary['last_trigger_reason']}")

# Check if task should be aborted
if reset.should_abort_task():
    logger.warning("Task abort recommended: too many resets")
```

## Best Practices

1. **Record failures explicitly** — Use descriptive reasons
2. **Seed from output** — Use `seed_from_output` for deterministic resets
3. **Set appropriate thresholds** — Adjust based on task complexity
4. **Use before irreversible actions** — Prevent destructive mistakes
5. **Monitor reset count** — Abort if exceeding max resets

## References

Inspired by Single Source of Truth (SSoT) attention management principles.
