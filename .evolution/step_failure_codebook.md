# Step-Level Failure Codebook for Trace Introspection

> **Reference**: *Annotrace: A Workbench for Step-Level Annotation of Agent Traces* (arXiv:2610.23284).  
> **Target**: Trace introspection and evolution selection stages in `hermes-agent-evolution`.

---

## 1. Unit of Analysis

The atomic unit for trace annotation is the **Execution Step**. Every failure or stuck episode in an agent run is decomposed into step instances belonging to exactly one of three canonical step types:

1. `delegation`: Tasks dispatched to subagents or child execution processes.
2. `tool_call`: Invocations of environment tools (file operations, bash/terminal, code execution, search).
3. `gate_decision`: Evaluation verdicts by automated quality, evidence, safety, or admission gates.

---

## 2. Mutually Exclusive Failure Classes

For each step type, failure classes form a strictly partitioned set. An anomalous step is classified into exactly **one** primary failure class based on authoritative evidence and deterministic diagnostic priority.

### A. Step Type: `delegation`

| Failure Class | Definition | Authoritative Signals |
|---|---|---|
| `timeout` | Subagent execution exceeded maximum allocated duration | `"deadline exceeded"`, `"subagent timed out"` |
| `context_overflow` | Subagent prompt or dialogue tokens exceeded context capacity | `"context length exceeded"`, `"maximum tokens reached"` |
| `recursion_limit` | Nested delegation depth exceeded or cyclical delegation loop | `"maximum delegation depth exceeded"`, `"recursion cycle"` |
| `subagent_crash` | Abnormal termination, unhandled exception in subagent process | `"subagent crashed"`, non-zero exit code of worker |
| `contract_violation` | Result missing mandatory fields or schema validation failed | `"missing required field"`, schema validation failure |
| `unauthorized_delegation` | Delegation forbidden by current role or capabilities | `"unauthorized delegation"`, `"permission denied"` |
| `other_delegation_failure` | Unclassified delegation error | Catch-all for residual anomalies |

### B. Step Type: `tool_call`

| Failure Class | Definition | Authoritative Signals |
|---|---|---|
| `precondition_failed` | Inputs or environment state invalid prior to execution | File missing, argument errors, patch indentation/no-match |
| `permission_denied` | Execution blocked by safety guardrails or OS permissions | Write guards, sensitive path, safety gate, EACCES |
| `execution_error` | Command execution crashed or returned non-zero exit | Non-zero exit code without benign meaning, tracebacks |
| `timeout` | Tool process ran past execution deadline | `"timed out"`, ETIMEDOUT, deadline exceeded |
| `output_unparsable` | Result output could not be parsed into expected structure | JSONDecodeError, unparsable envelope |
| `quota_exceeded` | Tool execution halted due to rate limits or quota | HTTP 429, billing exhaustion, rate limits |
| `other_tool_failure` | Unclassified tool execution failure | Catch-all for residual anomalies |

### C. Step Type: `gate_decision`

| Failure Class | Definition | Authoritative Signals |
|---|---|---|
| `evidence_mismatch` | Claim rejected due to unsupported, fabricated, or mistyped evidence | Evidence-typed gate mismatch, missing citation |
| `safety_violation` | Gate flagged safety risk or prompt injection attempt | Safety policy violation, injection risk |
| `regression_detected` | Gate flagged test breakage or metric degradation | Test failure, regression gate reject |
| `policy_rejected` | Candidate violated repo invariants, admission rules, or budgets | Invariant breach, effort budget overflow |
| `holdout_failed` | Overfitting detected on holdout trace evaluation | Holdout generalization test failure |
| `other_gate_failure` | Unclassified gate failure | Catch-all for residual anomalies |

---

## 3. Model Pre-Labelling & Confidence Scoring

During trace introspection:
1. Candidate steps are pre-labelled against this codebook by a model or deterministic classifier.
2. Each label carries a normalized confidence score `confidence ∈ [0.0, 1.0]`.
3. Labels with `confidence < 0.80` are tagged with `escalated = true` and routed to human review.
4. High-confidence labels (`confidence >= 0.80`) automatically enter the cycle failure-class time series.

---

## 4. Cycle Comparability & Selection Suppression

Step-level failure classes are aggregated into canonical keys `<step_type>.<failure_class>`.
Because the vocabulary is fixed, failure frequencies can be reliably compared across cycles.
When a failure class is already filed as an improvement issue or active patch proposal, the selection stage suppresses it from re-filing, preventing cyclic backlog inflation.
