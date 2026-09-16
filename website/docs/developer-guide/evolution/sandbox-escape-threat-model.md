# Sandbox-Escape Threat Model

Companion to `tests/evolution/test_sandbox_escape_boundary.py` (issue #2641,
SandboxEscapeBench arXiv:2603.02277). Maps its escape layers (orchestration /
runtime / kernel) onto Hermes's tool-execution boundary; all refs verified.

## Threat model

- **Escape is cheap.** Tool results and web content are untrusted input: a
  compromised model, prompt-injected skill/webpage, or malicious subprocess
  output can drive tool calls. Treat them as already hostile.
- Every boundary below is **defense-in-depth, NOT an isolation boundary**: the
  terminal tool runs as the same OS user with shell access, so `cat`/`curl`
  can bypass any file or network guard.
- Redaction (`agent/redact.py::redact_sensitive_text`, force-mode at
  `tools/terminal_tool.py::_redact_terminal_error_text`) is the last line of
  defense for secrets leaving through logs/errors, not a control on the shell.
- **Fail closed**: when a guard cannot decide, it denies (`replace.require`,
  `UnscopedSecretError` below).

## Orchestration layer (agent-level tool policy)

Defended today:

- Write boundary — `agent/file_safety.py`: `build_write_denied_paths()` /
  `is_write_denied()` hard-deny `/etc/passwd`, `/etc/shadow`, `/etc/sudoers`,
  `~/.ssh` keys, credential stores under HERMES_HOME; `build_write_denied_prefixes()`
  covers `.ssh`/`.aws`/`.gnupg`/`.kube`, `/etc/systemd`; approval-gated paths
  (`build_write_approval_paths`) fail closed for non-interactive callers.
- Read block (defense-in-depth only) — `get_read_block_error()` blocks `auth.json`, `.env`, `mcp-tokens/`; docstring: NOT a security boundary.
- Secret scope — `agent/secret_scope.py::get_secret()`: under multiplexing the
  scope is allowlist + fail-closed; unscoped reads raise `UnscopedSecretError`
  instead of leaking another profile's env.
- Verification scope — `agent/verification_scope.py` least-agency checks wired
  via `file_safety.set_active_verification_scope()`.

Not defended: shell bypass of every file guard; `verification_scope.py`
documents that disabled safeguards leave open network egress. Coverage:
`test_filesystem_escape_is_write_denied`,
`test_secret_scope_never_exposes_foreign_secrets`.

## Runtime layer (process spawn & environment)

Defended today:

- Explicit encoding on the main spawn path — `tools/environments/local.py`,
  `LocalEnvironment._run_bash()` → `subprocess.Popen(..., text=True,
  encoding="utf-8", errors="replace")` (Windows-footgun rule); plus
  process-group teardown (`_kill_process`), safe-cwd recovery.
- Terminal error envelopes never return raw secrets —
  `tools/terminal_tool.py::_redact_terminal_error_text` →
  `agent/redact.py::redact_sensitive_text(force=True)`.

Not defended: spawned processes inherit the process environment; no
seccomp/landlock, no resource limits; background processes are
session-tracked, not contained. Coverage:
`test_process_spawn_passes_explicit_encoding`.

## Kernel layer (network egress & syscalls)

Defended today (remote sandboxes only):

- Egress firewall — `agent/proxy_sources/iron_proxy.py::build_proxy_config()`:
  default-deny allowlist of provider hosts (anything else is 403'd); SSRF
  `upstream_deny_cidrs` (loopback, RFC1918, `169.254.0.0/16` cloud
  metadata/IMDS); fail-closed credential swap (`replace.require: true`). See
  `docs/security/network-egress-isolation.md`.

Not defended: the `local` terminal backend has no egress control; the proxy is
opt-in and wired only for docker/modal/ssh backends; TLS-interception trust is
part of the boundary (compromised proxy CA ⇒ guarantee gone). Coverage:
`test_network_egress_default_deny`.

## Gap summary

| Layer | Hard boundary today | Primary gap |
|---|---|---|
| Orchestration | deny-lists + scopes, fail closed | shell bypass always available |
| Runtime | encoding + redaction only | no OS-level process isolation |
| Kernel | opt-in egress proxy (remote) | local backend egress unguarded |
