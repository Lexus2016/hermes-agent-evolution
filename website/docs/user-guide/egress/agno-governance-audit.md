# Agno AgentOS Governance Audit — Pattern Checklist for Hermes

> **Source:** Issue #1290 — design-pattern audit (read-only, no adoption proposal).
> **Reference architecture:** [Agno AgentOS](https://agno.com) — governance-first
> runtime shipping RBAC, per-request isolation, HITL approval flows, audit logs
> in-DB, session/trace monitoring, post-exec hooks, and 3-axis evaluations.
> **Scope:** this audit compiles the Agno governance checklist as a rubric, then
> gap-audits Hermes's current permission/approval layer against each item. It
> recommends targeted hardening tickets where coverage is `partial` or `missing`,
> and explicitly states whether Agno itself should be adopted or pattern-borrowed.

## Why now

Hermes is itself pursuing "governance as a first-class concept." The stateless
MCP work (#1287) aligns operationally with Agno's horizontal-scalability
premise — governance and statelessness should be designed together, so this
audit cross-references #1287 where the two interact.

---

## 1. The Agno Governance Checklist

Each item is a concrete capability Agno ships out-of-the-box, phrased as a
self-contained requirement Hermes can be measured against.

| # | Capability | One-line definition |
|---|------------|---------------------|
| 1 | **RBAC on tools/skills** | Role-based access control: which principal (user/service-account) may invoke which tool or skill is enforced at the tool-call boundary, not just at the UI layer. |
| 2 | **Per-user / per-request isolation** | Each request runs in an isolated context: a principal on request N cannot read another principal's in-flight state, secrets, or memory on the same worker. |
| 3 | **HITL approval flows** | Gated actions (destructive tools, prod deploys, money-moving calls) pause execution, surface a prompt to a human approver, and resume only on explicit allow. |
| 4 | **Audit trail persisted in-DB** | Every tool invocation, approval decision, and permission grant is written to a queryable database table — not just ephemeral log lines — so it survives log rotation and supports retroactive review. |
| 5 | **Session / trace monitoring** | Each agent run carries a trace id; spans for tool calls, sub-tasks, and model invocations are recorded and viewable for debugging and postmortem. |
| 6 | **Post-execution hooks** | After a tool returns (or after a turn completes), registered callbacks may inspect the result, redact secrets, emit metrics, or trigger side-effects. |
| 7 | **Multi-axis evaluation** | Agent outputs are scored along ≥3 axes — typically accuracy, reliability, and performance — on a recurring basis, not just at release time. |

---

## 2. Coverage Matrix — Hermes permission/approval layer today

Mapping each Agno item to the concrete Hermes code that implements (or fails to
implement) it. File:line references are from `main` at the time of writing.

| # | Capability | Coverage | Hermes implementation |
|---|------------|----------|------------------------|
| 1 | RBAC on tools/skills | **missing** | No central role/principal → tool authorization layer exists. Tools are gated by *capability flags* (e.g. `tools/computer_use/permissions.py:166 request_permissions_grant` for OS-level grants) and by *write approval* (`tools/write_approval.py:74 write_approval_enabled`), but neither encodes a role taxonomy. A principal is effectively "the operator" — there is no `roles: [reader, deployer, admin]` concept enforced at the tool-call boundary. |
| 2 | Per-request isolation | **partial** | `tools/approval.py:41-50` uses `contextvars.ContextVar` (`_approval_session_key`, `_approval_turn_id`, `_approval_tool_call_id`) to scope approval state per-session/per-turn — this is the right primitive. However, isolation is **approval-state only**: shared resources (filesystem, env vars, the in-process memory snapshot) are not per-request. A multi-tenant deployment (multiple principals on one worker) would leak across requests. |
| 3 | HITL approval flows | **have it** | `tools/approval.py` (dangerous-command detection + per-session state + smart auto-approve via auxiliary LLM) and `tools/write_approval.py:253 evaluate_gate` (inline memory-write staging, `GateDecision` with allow/blocked/stage outcomes) together implement a real pause-and-resume approval loop. `_fire_approval_hook` (`tools/approval.py:96`) emits `pre_approval_request` / `post_approval_response` lifecycle hooks. This is the strongest item in the matrix. |
| 4 | Audit trail in-DB | **partial** | Audit logging exists but is **per-subsystem, not unified**: `tools/skills_hub.py:635 append_audit_log` records skill install/block events; `hermes_cli/dashboard_auth/audit.py` (referenced at `hermes_cli/web_server.py:16554`) records dashboard auth events. There is no single `tool_invocations` or `approval_decisions` table that captures every gated action across the whole agent. Coverage depends on whether the subsystem opted in. |
| 5 | Session / trace monitoring | **have it** | `plugins/observability/langfuse/__init__.py:45 trace_id` + `create_trace_id` (`:606`) wire a real distributed trace through tool spans and sub-tasks. The observability contract (`docs/observability/README.md`) explicitly supports trace, metrics, audit, replay, and export. Strong, though it is a plugin (optional) rather than always-on. |
| 6 | Post-execution hooks | **partial** | The approval system fires `post_approval_response` (`tools/approval.py:104`), and the plugin manager exposes lifecycle hooks — but there is no general-purpose "after every tool call" callback registry where a redactor/metric-emitter can hang. Hooks exist for the approval lifecycle specifically, not for arbitrary tool results. |
| 7 | Multi-axis evaluation | **missing** | No recurring automated evaluation pipeline along accuracy/reliability/performance axes was found. Trajectory compression (`trajectory_compressor.py`) and telemetry (`hermes_telemetry.py`) record *what happened*, not *how good it was*. There is no eval harness that scores agent outputs on a schedule. |

---

## 3. Top gaps → concrete hardening tickets

The three highest-leverage gaps, each scoped as a follow-up enhancement issue.

### Gap A — Unified audit-trail table (covers Capability #4)
**Problem:** Audit logging is sprinkled across subsystems (`skills_hub`,
`dashboard_auth`), with no single queryable record of "every tool invocation +
every approval decision." A retroactive review ("who approved what, when?")
requires grepping logs across subsystems.
**Proposal:** Introduce a single `governance_events` table (or a thin
abstraction over the existing per-subsystem logs) written to from one
centralized call site in the tool dispatch path. Schema: `event_id, timestamp,
principal_id, session_id, turn_id, tool_name, decision, reason, trace_id`.
Backfill the existing `append_audit_log` / `audit_log` callers to route through it.
**Estimated effort:** medium — touches the tool dispatch hot path; needs a
storage backend decision (sqlite vs. the existing state.db).
**Cross-ref:** Provides the evidence trail Capabilities #3 and #1 would enforce
against.

### Gap B — Tool-call-level RBAC (covers Capability #1)
**Problem:** There is no role taxonomy. Any principal that can reach the agent
can invoke any enabled tool. The only granularity is the per-tool
`enabled`/`disabled` config, which is global, not per-principal.
**Proposal:** Add a `tool_permissions.yaml` mapping `role → allowed_tools`
wildcard list, checked at tool dispatch. Default role = current behavior (all
enabled tools) for backward compatibility. Roles attach to principals via the
existing session/principal identity already used by the approval contextvars.
**Estimated effort:** medium — schema + dispatch check + tests; deliberately
non-disruptive via the default-allow role.
**Cross-ref with #1287:** The stateless MCP model routes any request to any
server instance; RBAC must therefore travel **inline with the request** (the
`_meta` field the stateless spec introduces), not be assumed from a sticky
session. Design these together.

### Gap C — Recurring multi-axis evaluation harness (covers Capability #7)
**Problem:** No scheduled job scores agent outputs on accuracy / reliability /
performance. Regressions in answer quality are detected only by humans.
**Proposal:** A cron-driven eval harness that runs a fixed task suite through
the agent, scores each result on the three axes (rubric LLM judge for accuracy,
retry-rate for reliability, latency P95 for performance), and appends to a
trend log. Reuses the existing observability trace id to correlate.
**Estimated effort:** medium-high — task suite curation is the long pole; the
scoring plumbing is straightforward given the observability plugin already
exists.

---

## 4. Should Hermes adopt Agno, or pattern-borrow?

**Recommendation: pattern-borrow. Do not adopt Agno as a runtime dependency.**

Rationale:
1. **Hermes already implements the two highest-value items** (HITL approval,
   trace monitoring) in its own codebase, deeply integrated with its tool
   dispatch and observability plugin contract. Ripping those out to delegate to
   an external runtime would be a net loss of integration depth.
2. **Agno's value proposition is "governance out-of-the-box for greenfield
   agent apps."** Hermes is not greenfield — it has a working approval layer,
   a working trace plugin, and a working (if fragmented) audit story. The gap
   is *unification and RBAC*, not *existence*.
3. **The stateless-MCP direction (#1287) and Agno's horizontal-scalability
   premise converge** — but Hermes's path to that convergence is its own MCP
   adapter, not a runtime swap. Borrowing Agno's checklist as a rubric (this
   document) captures the design value without the adoption risk.
4. **Adoption risk is non-trivial:** Agno is a fast-moving externally-governed
   project; pinning a production agent's governance plane to it imports its
   release cadence and breaking-change risk into a security-critical layer.

The right move is to treat this audit as a **rubric Hermes measures itself
against each cycle**, filing the gap tickets above and tracking coverage as it
improves.

---

## 5. Cross-reference: governance × stateless MCP (#1287)

The stateless MCP spec (#1287, finalizes 2026-07-28) removes session pinning
and routes any request to any server instance via inline `_meta` headers. This
has two direct governance implications surfaced by this audit:

- **RBAC must travel inline (Gap B above).** A stateless server cannot look up
  "who is this principal?" from a sticky session — the role must be carried in
  the request. The `tool_permissions.yaml` design must therefore stamp the
  principal's role into the MCP `_meta` field, and the server-side check reads
  it from there. Design Gap B and the stateless adapter together, not
  separately.
- **Audit trail must be written at the gateway, not assumed at the server
  (Gap A).** A stateless MCP server may be ephemeral; the durable audit record
  must be written by the Hermes gateway before/after the tool call, not relied
  upon to live in the server's own store. This reinforces putting the
  `governance_events` write in the tool dispatch path, not in the MCP adapter.

---

*This document is a read-only design audit. It proposes no code changes itself;
the three gap tickets above are the actionable follow-ups.*
