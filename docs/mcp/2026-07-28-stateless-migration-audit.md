# MCP 2026-07-28 Stateless Migration — Audit (Slice A)

**Parent issue:** [#1287](https://github.com/Lexus2016/hermes-agent-evolution/issues/1287)
**This issue:** [#1293](https://github.com/Lexus2016/hermes-agent-evolution/issues/1293)
**Slice:** A (read-only audit, no code change)
**Scope:** `tools/mcp_tool.py` (6056 lines) — every stateful call site enumerated below.

## 1. Spec background

The MCP (Model Context Protocol) specification adds a **stateless transport
mode**, finalizing **2026-07-28** (currently RC; no RC SDK published yet). In
stateless mode a server holds **no per-client session**: each request must carry
its full routing context (`Mcp-Method`, `Mcp-Name` headers, inline `_meta`
routing) instead of relying on an `initialize` handshake + persistent
`Mcp-Session-Id`. This is required for horizontal scaling, serverless
deployments, and the future MRTR / Tasks / Apps primitives.

The current Hermes adapter (`tools/mcp_tool.py`) is **entirely stateful**: it
performs `ClientSession.initialize()` on every connect, pins the resulting
session to a long-lived `MCPServerTask`, and issues all subsequent RPCs against
that pinned session. A grep of the whole tree finds **zero** references to
`stateless`, `Mcp-Method`, `Mcp-Name`, MRTR, or inline `_meta` routing — so
stateless support is a **net-new adapter path**, not a flag flip.

**Why this audit is time-critical:** it must land *before* 2026-07-28 to
unblock Slices B and C. The deadline is for the *spec*; code can follow, but the
call-site map below is the prerequisite for any later shim.

## 2. Decomposition (from owner comment on #1287, 00:03Z)

| Slice | Scope | Effort | Status |
|-------|-------|--------|--------|
| **A** | This document (read-only audit) | 0.3 | **landed by this PR** |
| **B** | Capability-detection shim behind `HERMES_MCP_STATELESS=1` flag (default OFF) | >0.6 | future cycle |
| **C** | Flag flip + migration notes, once an RC SDK publishes and a reference impl validates the stateless model | — | post-RC-SDK |
| **D** | MRTR, server-rendered UIs, long-running Tasks, MCP Apps (each its own epic) | — | indefinite |

## 3. Coverage matrix — every stateful site in `tools/mcp_tool.py`

Line numbers re-verified by grep this cycle. Grouped by concern.

### 3.1 Session initialize handshake (4 transport variants)

| Line | Code (abbrev.) | Refactor needed | Slice |
|------|----------------|-----------------|-------|
| 2426 | `self.initialize_result = await asyncio.wait_for(session.initialize(), …)` (stdio) | Gate behind capability check; for stateless servers skip `initialize` and synthesize a minimal `InitializeResult` from a cached/probed capabilities doc. | B |
| 2732 | `… session.initialize() …` (SSE transport) | Same as 2426. | B |
| 2789 | `… session.initialize() …` (streamable_http, new API) | Same as 2426. | B |
| 2821 | `… session.initialize() …` (streamablehttp, deprecated API) | Same as 2426; consider dropping deprecated branch in Slice C. | B/C |

### 3.2 `initialize_result` capture & consumption

| Line | Code (abbrev.) | Refactor needed | Slice |
|------|----------------|-----------------|-------|
| 1856 | `self.initialize_result: Optional[Any] = None` (`__slots__` + `__init__`) | Retain field; for stateless path populate it from a probe/caps-doc instead of a real handshake. | B |
| 1882 | `init_result = self.initialize_result` (inside `_advertises_tools()` capability gate) | No change needed if 1856 is populated correctly — this read site is capability-source-agnostic. Verify. | B |
| 5052 | `init_result = getattr(server, "initialize_result", None)` (`_select_utility_schemas`) | Same as 1882 — agnostic to how the caps were obtained. Add a test that a synthesized caps object flows through. | B |

### 3.3 Pinned `ClientSession` lifecycle

| Line | Code (abbrev.) | Refactor needed | Slice |
|------|----------------|-----------------|-------|
| 1807 | `self.session: Optional[Any] = None` (server field) | For stateless mode, `self.session` may be a *factory* or short-lived per-call handle rather than a persistent object. Introduce `self._stateless` flag. | B |
| 2429, 2735, 2792, 2824 | `self.session = session` (post-handshake pin, 4 transports) | Guard each: if stateless, do not pin; build a per-call session or a header-injecting wrapper. | B |
| 1933, 2989, 3014, 3026, 3029, 3158, 3207 | `self.session = None` (teardown / recycle / reconnect) | No structural change, but reconnect-loop logic must short-circuit for stateless servers (no session to tear down). | B |

### 3.4 Stateful RPC call sites (all route through `self.session` / `server.session`)

| Line | RPC | Refactor needed | Slice |
|------|-----|-----------------|-------|
| 2053 | `self.session.list_tools` (`_paginate_full_list` discovery) | Route via stateless adapter that injects `Mcp-Method: tools/list` + `_meta` routing when flag is on. | B |
| 2136 | `self.session.list_tools()` (keepalive ping fallback) | Same; also note ping fallback may be a no-op for stateless servers. | B |
| 2116 | `self.session.send_ping()` (keepalive) | Stateless servers have no liveness session — skip ping, treat each call as the liveness probe. | B |
| 2867 | `self.session.list_tools` (post-reconnect discovery) | Same as 2053. | B |
| 4263 | `server.session.call_tool(tool_name, arguments=…)` | The primary hot path. Stateless adapter must inline args + routing headers; no server-side session affinity. | B |
| 4414 | `server.session.list_resources` | Same adapter routing as 2053. | B |
| 4479 | `server.session.read_resource(uri)` | Same. | B |
| 4541 | `server.session.list_prompts` | Same. | B |
| 4612 | `server.session.get_prompt(name, arguments=…)` | Same. | B |

### 3.5 Session-id-bearing transports (HTTP variants)

| Line | Code (abbrev.) | Refactor needed | Slice |
|------|----------------|-----------------|-------|
| 2785-2786 | `streamable_http_client(url, http_client=…) as (…, _get_session_id)` | Stateless mode must NOT send `Mcp-Session-Id`; the `_get_session_id` callback is unused on the stateless path. | B |
| 2817-2818 | `streamablehttp_client(url, …) as (…, _get_session_id)` (deprecated API) | Same; prefer dropping deprecated branch in Slice C. | B/C |

### 3.6 Net-new (zero current references — confirmed by tree grep)

| Concept | Current refs | Needed for | Slice |
|---------|--------------|------------|-------|
| `stateless` / `Mcp-Method` / `Mcp-Name` headers | 0 | Stateless transport routing | B |
| Inline `_meta` request routing | 0 | Per-call context in stateless mode | B |
| MRTR (Model-Render-Tool-Result) | 0 | Server-rendered UIs | D |
| Long-running Tasks | 0 | Async task primitives | D |
| MCP Apps | 0 | App-install surface | D |

## 4. Slice B — capability-detection strategy sketch

Goal: a behind-the-flag shim (default OFF) that does not disturb any current
server. Sketch only — implementation is Slice B.

1. **Flag:** `HERMES_MCP_STATELESS=1` (env) read once at server start; stored on
   `MCPServerTask` as `self._stateless: bool`.
2. **Capability detection:** when `_stateless`, skip `session.initialize()`;
   instead probe the server's `.well-known/mcp` capabilities doc (or fall back
   to a single `tools/list` probe) and synthesize an `InitializeResult`-shaped
   object so the existing capability gates at lines 1882 / 5052 keep working.
3. **Adapter layer:** introduce a thin `StatelessRpc` wrapper that, for each
   call site in §3.4, injects `Mcp-Method` + `Mcp-Name` headers and any inline
   `_meta` routing, then issues a fresh one-shot HTTP request (no pinned
   session). Call sites switch on `self._stateless` to pick wrapper vs. pinned
   session.
4. **Transports:** only the streamable_http path (lines 2784-2806) needs the
   stateless branch; stdio and SSE remain stateful-only (they are inherently
   connection-oriented).
5. **Tests:** mock stateless server asserting (a) no `initialize` sent, (b)
   `Mcp-Method`/`Mcp-Name` headers present on every call, (c) capability gating
   still filters resources/prompts correctly.

## 5. Slice C — flag flip & migration notes stub

- **Gate:** do NOT flip the default until (a) an RC SDK publishes and (b) a
  reference stateless server validates the synthesized-caps flow from Slice B.
- **Migration notes (to be filled in Slice C):** per-server opt-in via config
  (`stateless: true`), deprecation timeline for the deprecated `streamablehttp`
  branch (lines 2808-2834), and a runbook for servers that advertise stateless
  but still require a warm `initialize`.
- **Rollback:** the flag defaults OFF; flipping back is a one-line revert.

## 6. Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Slice B shim breaks a stateful server | Low | High | Flag defaults OFF; full stateful path untouched; CI runs the existing MCP suite unchanged. |
| Synthesized caps object shape drifts from SDK `InitializeResult` | Med | Med | Pin to a `typing.Protocol` capturing only `.capabilities.{tools,resources,prompts}`. |
| Spec finalizes 2026-07-28 with header/format changes vs. current RC | Med | Med | Slice B stays behind flag; Slice C waits for RC SDK. This audit is text-only and immune. |
| Deprecated `streamablehttp` branch (2816) diverges from new API | Low | Low | Slice C may delete it; flag both variants identically in Slice B. |
| **This PR (Slice A)** | — | — | **Zero code change; docs-only; no behavioral risk.** |

## 7. Cross-references

- Parent: #1287 (decomposed 2026-07-26 00:03Z into Slices A/B/C/D).
- This issue: #1293 (Slice A, owner-recommended, ≤200 lines).
- Related: the Agno governance audit (#1290, merged via PR #1292) flagged the
  same adapter as a permission-surface dependency.
- Implementation report measuring the 6056-line file and the 4 handshake sites:
  `/config/.hermes/evolution/implementation/2026-07-26.md`.

---

*Audit only. No code changed. Line numbers current as of `origin/main` at audit
time; re-grep before starting Slice B.*
