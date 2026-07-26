# MCP 2026-07-28 Stateless Migration — Stateful Surface Audit

**Status:** Slice A — read-only audit (zero behavioral change).
**Parent issue:** #1287 — [URGENT, 48h] Audit + patch MCP adapter for 2026-07-28 stateless spec.
**Child issue:** #1294 — this document.
**Audited file:** `tools/mcp_tool.py` (6056 lines) at `origin/main` HEAD `38c330b24`.
**Audited on:** 2026-07-26.

---

## 1. Background — what the 2026-07-28 spec changes

The MCP specification moving to GA on **2026-07-28** removes three classes of
implicit per-connection state that the current adapter assumes. Each removal is
tracked by a SEP (Spec Enhancement Proposal):

| SEP | Removes | What breaks |
|-----|---------|-------------|
| **SEP-2575** | The `initialize` / `notifications/initialized` handshake | Every `session.initialize()` call site |
| **SEP-2567** | The server-assigned `session-id` | All storage/reuse of `session_id` |
| (routing) | Long-lived `ClientSession` carrying affinity | `Mcp-Method` / `Mcp-Name` header routing, inline `_meta`, `MRTR` |

**Verified this session** (zero-reference confirmation): there are **no** existing
references in `tools/mcp_tool.py` to any of the new-stateless terms —
`stateless`, `Mcp-Method`, `Mcp-Name`, `MRTR`, or inline `_meta`. A `grep` for
that pattern returned `total_count: 0`. This means the entire stateless path is
**net-new adapter work** — there is no partial implementation to extend; Slice B
must author it from scratch.

The current adapter is built entirely on the **2025 stateful model**: one
`ClientSession` per server, `initialize` handshake performed, `InitializeResult`
cached for capability gating, and every request (`call_tool`, `list_tools`,
`read_resource`, `send_ping`) flows over that long-lived session. The sections
below enumerate where that assumption is load-bearing.

---

## 2. Stateful call sites — full enumeration

### 2.1 The `initialize` handshake (SEP-2575 removal target)

The `initialize` / `notifications/initialized` exchange is the handshake
SEP-2575 removes. It is invoked at **four** distinct connection sites, one per
transport family. Each site follows the same pattern:

```python
self.initialize_result = await asyncio.wait_for(
    session.initialize(), timeout=<connect_timeout>
)
```

| File:line | Transport | Context |
|-----------|-----------|---------|
| `tools/mcp_tool.py:2426-2428` | **stdio** | Inside `async with stdio_client(...)` → `async with ClientSession(...)` block. Connect-timeout bounded (`#59349` orphaned-task guard). |
| `tools/mcp_tool.py:2732-2734` | **SSE** (legacy) | Inside `async with sse_client(...)` → `ClientSession`. Same timeout bound. |
| `tools/mcp_tool.py:2789-2791` | **Streamable HTTP** (mcp ≥ 1.24.0) | Inside `streamable_http_client(url, http_client=...)`, which also yields a `_get_session_id` callable (§2.2). |
| `tools/mcp_tool.py:2821-2823` | **Streamable HTTP** (deprecated, mcp < 1.24.0) | Inside `streamablehttp_client(url, ...)` fallback path. Identical pattern. |

**Refactor approach for each site (SEP-2575):**

Under the new spec these four `session.initialize()` calls become no-ops at the
protocol level — the server advertises capabilities in its first response or via
a stateless endpoint. Two parts:

1. **Capability acquisition without a handshake.** Replace the blocking
   `initialize()` with a probe compatible with both pre-spec servers (still
   expecting the handshake) and post-spec servers (advertising inline). This is
   the **capability-detection shim** deferred to Slice B — it must detect which
   model the server speaks and route accordingly, else a hard cut breaks every
   2025-spec server on day one.
2. **Preserve the timeout bound.** Each site wraps `initialize()` in
   `asyncio.wait_for(..., timeout=connect_timeout)` to convert the
   orphaned-task hang (`#59349`) into a normal failure so the `finally` reaps
   the child. The shim must keep this guard — a silent server parks the
   coroutine forever regardless of handshake variant.

### 2.2 Server-assigned `session-id` storage/reuse (SEP-2567 removal target)

SEP-2567 removes the server-assigned session identifier. Two call sites receive
a `_get_session_id` callable from the HTTP transport context manager but
currently do not store or reuse its return value:

| File:line | Transport | Code |
|-----------|-----------|------|
| `tools/mcp_tool.py:2785` | Streamable HTTP (≥ 1.24.0) | `async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream, _get_session_id,):` |
| `tools/mcp_tool.py:2817` | Streamable HTTP (deprecated) | `async with streamablehttp_client(url, **_http_kwargs) as (read_stream, write_stream, _get_session_id,):` |

**Refactor approach (SEP-2567):** today `_get_session_id` is unpacked but its
result is never captured into `MCPServerTask` state — the session is identified
only by the long-lived `ClientSession` object itself (§2.3). Under the
stateless model there is no server-assigned id to capture; the SDK-level session
pinning beneath `_get_session_id` disappears. Slice B should:

- Remove the `_get_session_id` unpack entirely (or assert it returns `None` for
  stateless servers).
- Confirm no downstream code reads a stored session id. **Verified this
  session:** a search for `session_id` / `_get_session_id` in `mcp_tool.py`
  returns only the two unpack lines above — there is no storage site, so the
  removal surface is exactly those two lines. This is the lowest-blast-radius
  of the three SEPs.

### 2.3 The long-lived `self.session` handle (routing SEP / affinity removal)

The deepest stateful assumption is that **one `ClientSession` object represents
one server for the life of the connection**. It is stored on
`MCPServerTask.session` and every request flows through it:

| File:line | Site | Purpose |
|-----------|------|---------|
| `tools/mcp_tool.py:1807` | `__init__` | `self.session: Optional[Any] = None` — declaration |
| `tools/mcp_tool.py:2429` | stdio connect | `self.session = session` — capture after `initialize()` |
| `tools/mcp_tool.py:2735` | SSE connect | `self.session = session` |
| `tools/mcp_tool.py:2792` | HTTP (≥1.24) connect | `self.session = session` |
| `tools/mcp_tool.py:2824` | HTTP (deprecated) connect | `self.session = session` |
| `tools/mcp_tool.py:2053` | discovery | `self.session.list_tools` — tool enumeration |
| `tools/mcp_tool.py:2116` | keepalive | `self.session.send_ping()` — liveness probe |
| `tools/mcp_tool.py:2136` | keepalive fallback | `self.session.list_tools()` — ping-unsupported fallback |
| `tools/mcp_tool.py:2867` | reconnect discovery | `self.session.list_tools` — re-enumerate after reconnect |
| `tools/mcp_tool.py:4263` | **tool dispatch** | `await server.session.call_tool(tool_name, arguments=args)` — the hot path |
| `tools/mcp_tool.py:4479` | **resource read** | `await server.session.read_resource(uri)` |
| `tools/mcp_tool.py:2213, 2854` | guards | `if self.session:` / `if self.session is None:` liveness checks |
| `tools/mcp_tool.py:1933, 2989, 3014, 3026, 3029, 3158, 3207` | teardown | `self.session = None` — seven null-out sites on disconnect/recycle/error |

**This is the largest blast radius of the migration.** Under the new
`Mcp-Method` / `Mcp-Name` header-routing model, a request is addressed by header
rather than by which `ClientSession` it is sent over; a stateless pool of
connections can serve any server. The refactor cannot simply delete
`self.session` — eleven distinct call sites dispatch through it. Slice B must
introduce an indirection layer (a `_dispatch(method, params)` method on
`MCPServerTask`) that today delegates to `self.session.<method>` but under the
new model routes via headers over a connection pool. The dispatch and
read-resource sites (`4263`, `4479`) are the hot path and must not regress.

### 2.4 Cached `initialize_result` capability gating (depends on SEP-2575)

The adapter caches `InitializeResult` and reuses it to gate which request
families are safe to call, rather than probing blindly:

| File:line | Site | Purpose |
|-----------|------|---------|
| `tools/mcp_tool.py:1856` | `__init__` | `self.initialize_result: Optional[Any] = None` — declaration |
| `tools/mcp_tool.py:1882-1886` | `_advertises_tools()` | reads `.capabilities.tools` to decide whether `tools/list` is safe (avoids `-32601` killing discovery on prompt/resource-only servers) |
| `tools/mcp_tool.py:5052-5054` | `_select_utility_schemas()` | reads `.capabilities.resources` / `.capabilities.prompts` to filter `list_resources` / `list_prompts` utility schemas |
| `tools/mcp_tool.py:5081` | legacy fallback | comment: "initialize_result wasn't captured. Preserves the old behavior" — the fail-open path when no capability info exists |

**Refactor approach:** `initialize_result` is the cached output of the handshake
that SEP-2575 removes. Under the new model capabilities arrive inline on
responses or via a stateless endpoint, so there is no single `InitializeResult`
to cache. Slice B must replace this cached field with a capability-resolution
function that behaves as today for 2025-spec servers (handshake → cache → gate),
resolves on first use and caches per server-name (not per connection) for
2026-spec servers, and preserves the **fail-open contract** at line 5081 (gate
to legacy always-call when capability info is absent). The `_advertises_tools()`
docstring (1868-1886) makes this contract explicit and it must survive.

---

## 3. Per-SEP refactor summary

| SEP | Call sites affected | Net-new code? | Risk | Slice |
|-----|---------------------|---------------|------|-------|
| **SEP-2575** (handshake removal) | 4 `session.initialize()` sites (§2.1) + 4 `initialize_result` cache sites (§2.4) | Yes — capability-detection shim | Medium — breaks all 2025-spec servers if cut hard | **B** |
| **SEP-2567** (session-id removal) | 2 `_get_session_id` unpacks (§2.2), 0 storage sites | No — removal only | **Low** — no stored id exists; delete the unpacks | **B** |
| Routing (`Mcp-Method`/`Mcp-Name`, `_meta`, MRTR) | 11 `self.session.*` dispatch sites (§2.3) | Yes — `_dispatch()` indirection + header routing | **High** — hot path (`call_tool` line 4263) | **B/C** |

---

## 4. What does NOT change (confirmed stateless-safe)

- **`_ping_unsupported` latch** (line 1801; logic 1857-1862): per-connection
  keepalive-fallback boolean; resets on each fresh transport, tracks an SDK
  quirk not a server-assigned id — stateless-compatible as-is.
- **Sampling / elicitation callbacks** (lines 1821-1822, 2348, 1454, 1664):
  client→server callbacks registered per-session via `sampling_kwargs`; they
  move with the session object and depend on no server-assigned id.
- **Keepalive / reconnect logic** (lines 2116, 2136, 2208, 2800): driven by the
  `_wait_for_lifecycle_event()` loop and local breaker state, not by session-id.

---

## 5. Acceptance criteria for this slice (all met)

- [x] Audit document exists at `docs/mcp/2026-07-28-stateless-migration-audit.md`
- [x] Every `initialize`/`initialized` call site listed with file:line (§2.1 — 4 sites)
- [x] Every session-id storage/reuse site listed with file:line (§2.2 — 2 unpacks, 0 storage)
- [x] Each site annotated with the specific SEP it maps to and the refactor approach (§3)
- [x] No behavioral change to any code (read-only slice — this commit adds one markdown file)

---

## 6. What is explicitly NOT in this slice

- Any code patch (Slice B+ — blocked until the spec finalizes 2026-07-28 and RC
  SDKs publish, so the capability-detection shim can be validated against a real
  post-spec server).
- The capability-detection shim itself (Slice B).
- Migration docs for skill/plugin authors (Slice C).

The audit is produced ahead of spec GA so Slice B can begin implementation the
moment an RC SDK is available, with every call site already mapped.
