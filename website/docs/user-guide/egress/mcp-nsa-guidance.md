# MCP Security Policy — NSA Guidance Alignment (#3040)

This document is the **reviewable in-repo policy** for consuming third-party MCP
servers. It adopts the three systemic risk areas named in the NSA Artificial
Intelligence Security Center's first formal US-government MCP guidance (CSI
U/OO/6030316-26, May 2026) as the governing principles for every MCP server
Hermes connects to, and maps each to the enforcement mechanism already in the
codebase plus the one this document introduces (`sensitive` trust tier).

## Principles

### 1. Per-call re-authorization, not login-time trust

A connected MCP server is **not** trusted by default. Every call to a server
that is not `trust: full` is authorized **at call time**, not at connect time.

- **Enforcement:** `tools/mcp_tool.py::_trust_gate_check` runs before ANY
  transport work (including the lazy first-use spawn) for servers configured
  `trust: untrusted` or `trust: sensitive`. A denied call never touches the
  server. The approval routes to whichever surface owns the session (CLI, TUI,
  Telegram, Slack, …) via `tools.approval.request_elicitation_consent`.
- **Tiers (config `trust:` key on the server entry):**
  - `full` (default, backward compatible) — gate off; operator has explicitly
    vetted the server.
  - `untrusted` — every **write-capable** tool (no `readOnlyHint=true`
    annotation) is approved per call; read-only tools pass.
  - `sensitive` (added by this policy) — **every** tool call, read or write,
    is approved per call, because the server handles sensitive/regulated data
    and must be isolated accordingly.
  - Any unrecognized value normalizes to `untrusted` (fail closed).

### 2. Trust-level isolation: public vs sensitive data

Tools handling public data and tools handling sensitive/regulated data are
**separated** at the trust boundary, so a compromise of a public-facing server
cannot silently read from a sensitive one.

- **Enforcement:** the `sensitive` tier above is the mechanism — mark a
  sensitive-data server `trust: sensitive` and every one of its tools is
  gated per call. Public-data servers can stay at `full` or `untrusted`
  without widening access to the sensitive tier.

### 3. Unverified task propagation

Tasks passed between MCP servers/components, and stages passed between
delegated subagents, carry **origin, scope, and intent**. The agent must not
blindly forward a task from an untrusted source to a privileged target.

- **Enforcement:** task-extension responses from MCP servers
  (`tools/mcp_tool.py` MCP Tasks Extension handling) are only driven to
  completion when the originating tool call itself passed the trust gate; a
  `sensitive` server's spawned tasks therefore also inherit per-call
  authorization. Delegation guidance (see `docs/evolution/`) instructs
  subagent teams to treat forwarded tasks as requiring the same
  origin/scope/intent scrutiny as direct calls.

## Configuration reference

```yaml
mcp_servers:
  public-readonly:
    command: npx
    args: ["-y", "public-server"]
    # default: full  -> no gate (vetted public data)
  home-files:
    command: python
    args: ["mcp.py"]
    trust: untrusted        # write-capable tools gated per call
  finance-records:
    command: python
    args: ["finance-mcp.py"]
    trust: sensitive        # ALL tools gated per call (isolation)
```

## Review cadence

This policy is reviewable in-repo. It lives with the security audits under
`docs/security/` and is expected to be re-read (and updated) whenever a new
third-party MCP server is onboarded at scale, per the NSA guidance's emphasis
on re-evaluating implicit trust relationships.

## References

- NSA press release on MCP security guidance (CSI U/OO/6030316-26, May 2026).
- Reed Smith analysis of the NSA MCP guidance.
- Issue #3040.
