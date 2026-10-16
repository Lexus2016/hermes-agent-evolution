---
title: Public Subagent Lifecycle API
sidebar_label: Subagent lifecycle API
---

# Public Subagent Lifecycle API

Plugins can launch and supervise fresh Hermes child sessions without importing
`tools.delegate_tool`, gateway internals, TUI state, or `AIAgent` fields.
The service resolves its parent from the current agent turn, so it works in
CLI, gateway, non-interactive, and kanban-worker sessions. Launching outside an
active agent turn fails closed with `No active Hermes parent session`.
The facade also works when a plugin runs under `plugins.isolation: host`.

Requests accept `goal`, `context`, `role`, `model`, `provider`, `allowed_toolsets`,
`blocked_tools`, `working_directory`, `parent_session_id`, `correlation_id`,
`metadata`, and `timeout_seconds`, subject to the restrictions below.

```python
from agent.subagent_lifecycle import SubagentLaunchRequest

def launch_review(ctx):
    # Call from a plugin tool or hook while an agent turn is active.
    service = ctx.subagent_lifecycle
    handle = service.launch(SubagentLaunchRequest(
        goal="Review this change for regressions.",
        context="Only inspect the supplied repository.",
        role="leaf",
        correlation_id="review-42",
        allowed_toolsets=("file",),
    ))
    # Persist handle.to_dict() if desired.
    if service.wait(handle, timeout_seconds=2).timed_out:
        return handle.to_dict()
    return service.result(handle)
```

`SubagentHandle` is serializable and carries a versioned, opaque capability.
Requests and handles may also arrive as mappings, including plugin-host wire records.
Pass a handle back to `status`, `wait`, `cancel`, `steer`, `result`, or `reconnect`; malformed
or forged handles return `UNKNOWN`/`UNKNOWN_HANDLE` and cannot access a child.

`steer(handle, text)` queues text for the child's next iteration boundary and
returns `True` while the child accepts steering. It returns `False` for empty
text, unknown or forged handles, handles owned by another parent session, or a
child that has finished or stopped accepting steering.

The stable states are `PENDING`, `STARTING`, `RUNNING`, `SUCCEEDED`, `FAILED`,
`INTERRUPTED`, `CANCEL_REQUESTED`, `CANCELLED`, and `UNKNOWN`.

`cancel(handle, reason=...)` is cooperative: it asks the child agent to
interrupt at its next safe boundary and returns `CANCEL_REQUESTED`; it never
claims completion until `wait` or `result` observes a terminal state. Terminal
results are immutable, idempotent, bounded to 32k characters, omit transcripts
and hidden reasoning, and include a stable result hash.

This API is lifecycle-managed asynchronous execution. Child construction and
completion use the same host-owned path as `delegate_task`, including parent
tool-resolution restoration, memory notification, serialized `subagent_stop`
hooks, resource cleanup, and child-cost rollup. It does not change the
synchronous `delegate_task` tool, batch delegation, or its gateway/TUI display.
The initial implementation retains metadata and terminal results in-process for
one hour.
After a process restart, `reconnect` returns `RECONNECT_UNAVAILABLE` and never
starts a replacement child. Running Python threads also cannot survive process
exit; callers must treat those handles as interrupted by process exit.

Requests are fail-closed: goal/context/metadata sizes are capped, unknown or
parent-broadening toolsets are rejected (plugin-registered toolsets count as
known), and per-tool blocks, working-directory overrides, and per-launch
timeouts are explicitly rejected until Hermes can
support them without weakening isolation. Use `allowed_toolsets` to narrow a
child; Hermes's existing unsafe-tool block remains enforced.

## Routing

Children follow the `delegation` config (`provider`, `model`, `base_url`,
`api_key`, `api_mode`, `request_overrides`) exactly as `delegate_task` children do.
The request's `model` overrides the configured model. Its optional `provider`
names a configured provider for this launch, with precedence request >
delegation config > parent; the selected provider supplies its own credential
bundle. An unresolvable provider raises `SubagentLifecycleError` before launch.
Core does not expose `provider` to the model: the plugin chooses it, for example
from operator config.

## Delegation escalation prototype

Every delegated run records a content-free contact trajectory under the active
profile's `evolution/trajectories/`: run start, each committed tool result (including
blocked results), and an outcome report. Decisions include the state, result class,
SILENT/CONSULT values, consultation price, and suppression reason. The report is
also returned in the child's `escalation` field and aggregated by `evolution_funnel.py`.
Goals, raw tool results, and advice are not stored in these records.

By default the controller only observes and makes no advisor requests. Configure
`delegation.escalation` in `config.yaml` to opt in:

```yaml
delegation:
  escalation:
    enabled: true
    consultation_price: 0.5
    budget_mode: count_only
    max_consultations: 1
auxiliary:
  delegation_advisor:
    provider: your-configured-provider
    model: your-configured-advisor-model
```

This prototype enforces a request-count budget (maximum 10 consultations per run),
512 output tokens per request and a 30-second request timeout. It does not enforce
a dollar ceiling. Configure the advisor through the existing auxiliary routing
system. No new provider credentials or privileges are created. The consultation
price is a dimensionless reward penalty, not USD. Missing or invalid budget mode
suppresses consultation; increasing the price suppresses marginal consultations.
Safety/approval denials and interrupted runs cannot request consultation. Advisor
text is explicitly untrusted, non-authorizing tool-result context, incorporated
before persistence while preserving structured JSON result envelopes; it does
not authorize actions or modify the system prompt.

The shipped values are an inspectable heuristic in `tools/delegation_escalation.py`.
The controller selects CONSULT only when `Q(CONSULT) - price > Q(SILENT)`.
Per-profile learned values load at run start from
`evolution/delegation-action-values.json`. Invalid tables fall back to the defaults.

Offline reports and first-visit Monte Carlo updates require no model calls:

```sh
python scripts/delegation_escalation_report.py ~/.hermes/evolution/trajectories
python scripts/delegation_escalation_report.py ~/.hermes/evolution/trajectories \
  --verified-outcomes reviewed-outcomes.json --price 0.5 \
  --output ~/.hermes/evolution/delegation-action-values.json
```

The reviewed outcome file maps trajectory session IDs to
`{"verified": true, "reward": 1.0}` (finite rewards between zero and one).
Only independently reviewed outcomes belong here. Runtime completion is not a
training label. Rebuilding the table is idempotent; each state/action contributes
once per trajectory. Values include penalties for subsequent consultations;
the current consultation price is subtracted once at selection. Keep the training
price and runtime price aligned when using a learned table. Unseen state/actions
retain their default values. Use separate, held-out trajectories for evaluation;
never update a policy with an evaluation run's future outcome before replaying it.

Reports distinguish unknown monetary cost from zero. A successful consulted run
is not evidence of a recovered failure: recovered failures versus always-proceed
remain null until a separately controlled paired evaluation exists. Tests use
synthetic fixtures to check wiring and accounting, and make no quality claim.

For a separately controlled always-proceed comparison, reviewed outcomes may also
include `paired_baseline_verified: true`, a `baseline_evidence` reference and binary
`baseline_reward`. Supplying `--verified-outcomes` without `--output` produces a
report using only those reviewed pairs, including paired coverage and recovered
failures. Unpaired runs do not become counterfactual evidence. The live funnel
remains explicitly unmeasured until such evidence is supplied to the offline report.
Advisor routes must be explicit (no auto/MoA routing) and their transports must
support disabling retries per request; unsupported transports fail closed. Capacity
errors do not invoke the auxiliary fallback ladder or the main model endpoint.
