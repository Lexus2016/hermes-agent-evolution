# Upstream sync v2026.7.7.2 — conflict resolution report

**Merge:** `git merge --no-ff v2026.7.7.2` into fork `main`
**Scale:** 712 upstream commits behind, 388 of our commits ahead, ~1310 files, 20 conflicts.
**Merge-base:** `7c1a029` (shared history restored by the earlier `git fetch --unshallow` — see #823).

Our evolution-specific files (telemetry, evolution skills/cron/scripts) are **additive and did NOT
conflict** — nothing of ours can be silently dropped from those. Verified: 0 conflict markers, 0
silently-dropped OUR symbols (SKILL Step-3 sweep), `compileall` clean, all key modules import.

---

## Resolved autonomously (authorship-first)

| File | Resolution |
|---|---|
| `agent/error_classifier.py` | **take upstream** — adds `ssl_cert_verification` error class (additive) |
| `cron/jobs.py` | **take upstream** — adds past one-shot-time validation (`ONESHOT_GRACE_SECONDS`) |
| `agent/auxiliary_client.py` | **keep BOTH** — our `aux_health_ping()` + upstream `_effective_aux_timeout()` (two additions collided) |
| `hermes_cli/main.py` | **keep BOTH** command names — our `corrections` (#569) + upstream `console` |
| `hermes_cli/models.py` | **take upstream** — NVIDIA NIM catalog (upstream-domain; same providers we use are unaffected) |
| `.github/workflows/ci.yml` | **merge** — our `cache/save@v6.1.0` pin (dependabot #761) + upstream's `hashFiles()` guard |
| `scripts/release.py` | **keep BOTH** `AUTHOR_MAP` blocks (attribution is additive) |
| `agent/chat_completion_helpers.py` | **take upstream** — Codex TTFB tuning (10k) + `"timestamp"` schema key (upstream-domain) |
| `tools/file_tools.py` | **merge** — keep both imports; description = upstream truncation/next_offset **+ our batch-read note (#784)** |
| `tools/terminal_tool.py` | **keep BOTH** — our `_increment_terminal_streak` + upstream's approved-run interrupt-clear (retry gating later switched to upstream parity — see pivot below) |
| `tools/web_tools.py` | **keep BOTH** — upstream's `_LEGACY_WEB_BACKENDS` registry refactor **+ our `_search_with_fallbacks`/`_search_backend_fallback_chain` (#574, still called by `plugins/web/ddgs/provider.py`)** — the Step-3 sweep caught a `--theirs` drop and it was restored |
| `tests/*` (error_classifier, web_providers, runtime_provider, mcp, cron) | tests follow their code; our test classes (`TestSearchFallbackChain`, `TestModelSuffixVariants`, …) preserved |

---

## Deep-core strategy — take upstream + re-apply our deltas (owner-decided pivot)

The first pass **deferred** four fragile-core files (kept OUR version). CI then went red: the deep core is
**entangled with the merged callers/tests** — merged `run_agent.py`/`model_tools.py` + upstream tests call
`hermes_cli.plugins.resolve_pre_tool_block` (an upstream-only symbol), and upstream `plugins.py` in turn
imports `tools.approval.request_tool_approval` (also upstream-only). Keeping OUR versions broke those
callers (29 of our own tests failed with `AttributeError`/`ImportError`). The owner chose to **take
upstream for the entangled files and re-apply our deltas**; the two genuinely-separable files stay deferred.

### Taken upstream, our deltas re-grafted
| File | Action | Our delta re-applied | Verified |
|---|---|---|---|
| `hermes_cli/plugins.py` | **take upstream** | none — our fork was simply behind (18 ins / 196 del vs upstream, no fork commits) | `resolve_pre_tool_block` present; run_agent blocked-tool tests green |
| `tools/approval.py` | **take upstream** | **#611 per-job cron approval override** ported into `_get_cron_approval_mode` (upstream already carried the rest of the `HERMES_CRON_SESSION` cron-approval gate) | approval / gateway / model_tools clusters: 705 pass |
| `agent/conversation_loop.py` | **take upstream** | (1) **loop-guard block** (95 lines — mono-tool spiral #432, unattended-cron hard-stop #624/#662, escalated interrupt) inserted after the budget check; (2) **`run_conversation`→`_run_conversation_impl` telemetry-span wrapper** (#167). Our #716/#510 rate-limit/fallback stopgaps are **subsumed** by upstream's far larger error-recovery (234 refs); our `test_rate_limit_fail_fast` guard passes against upstream. | compaction tests pass; 68 loop_guard tests pass; telemetry-span test passes |

### Still deferred (clean — 0 CI failures from them)
| File | Why still deferred |
|---|---|
| `cron/scheduler.py` | upstream `_teardown_cron_agent` / lifecycle rewrite; no merged caller depends on the new symbols, so keeping ours is self-consistent |
| `tools/delegate_tool.py` | upstream `_strip_model_hidden_task_fields` collides with our agent-team identity (#252); the only caller (`run_agent.py` delegate dispatch) was surgically reverted to our `tasks=function_args.get("tasks")` |

Their leaked upstream tests were reverted to ours and pass: `tests/cron/test_scheduler.py` (222), `tests/cron/test_run_one_job.py` (6), `tests/tools/test_delegate.py` (166). Upstream's new deferred-only test files stay removed until the code is applied: `tests/tools/test_scheduler_shutdown_guard.py`. **To finish:** `git checkout v2026.7.7.2 -- cron/scheduler.py tools/delegate_tool.py`, re-apply our evolution cron hooks / agent-team identity block, re-add the upstream tests, re-run.

### Other deltas from the pivot
- `agent/conversation_loop.py` `_billing_or_entitlement_message` — upstream's generic message dropped the fork's #288 "HTTP 402 (out of credit or quota)" naming. Re-grafted onto upstream's wording (both `test_288_billing_guidance` tests pass).
- `tools/terminal_tool.py` — **kept at the keep-BOTH committed resolution** (no further change). An attempt to switch the exception retry to upstream's unconditional retry was **reverted**: with `psutil` installed (matching CI) the classify-gated `test_retry_backoff_does_not_clear_genuine_interrupt` already passes, and unconditional retry wrongly retried interrupt/guard errors.
- `tests/hermes_cli/test_setup_blank_slate.py` + `test_prompt_size.py` — upstream tests assert a 6-tool Blank Slate; our fork's `repo_map` self-registers into the `file` toolset (toolsets.py:196), so the surface is 7. Tests updated to expect `repo_map` (fork-correct).

### ✅ Deep-core deltas RE-APPLIED (both previously-xfailed items now fixed)
The two remaining fork reliability deltas were fully re-applied; their `xfail` markers are removed and the tests pass on their own logic (locally the two `test_rate_limit_fail_fast` cases only fail on an env `state.db is locked` from the developer's live gateway — the fail-fast firing was proven via a `_persist_disabled=True` standalone repro: `failed=True, failure_reason=rate_limit, call_count=2`).
- **#704/#716 consecutive-429 fail-fast** — re-applied into upstream's restructured flow: the fail-fast BLOCK inserted after the Nous fall-through (before `is_payload_too_large`), **plus** a `_retry.consecutive_rate_limit_hits = 0` reset added at **all 10** recovery `continue` points (every `_retry.primary_recovery_attempted = False` site + the `recovered_with_pool` block). Resetting where the counter is already 0 is a no-op, so covering ALL recovery paths makes a missed-reset false-fire impossible. Verified: `test_two_consecutive_429s` fires (repro), `test_single_429_then_success` + `test_fresh_api_call_gets_a_fresh_counter` confirm no false-fire on recovery; 167-test failover/rate-limit regression sweep green.
- **terminal interrupt-note** — root cause found: an interrupted approved command (rc=130 WITH the `"[Command interrupted]"` marker) is handled by the fork's non-retryable `returncode != 0` return path (`terminal_tool.py` ~2822) which returned the BARE approval note, never reaching upstream's rc==0 enrichment at :2949. Fix: apply the same enrichment in the non-retryable path. Verified: note now reads "…approved by the user, then interrupted."

### Post-pivot silent-drop verification (SKILL Step-3, re-run after take-upstream)
Taking upstream `conversation_loop.py` risked silently dropping untested fork deltas, so the sweep was re-run against the pivot (not assumed):
- Top-level `def`/`class` diff (ours HEAD^1 vs upstream): the only fork-only symbol is `_run_conversation_impl` — **re-applied** (telemetry wrapper). Zero other top-level drops.
- Named fork reliability deltas audited individually and ALL re-applied: loop-guard #432/#662/#765 (68 loop_guard tests pass); telemetry #167; #288 billing; **#716** consecutive-429 fail-fast (see above). **#510** api_error→fallback provider-skip is **intact** — our HEAD^1 also passed `api_error` internally via `chat_completion_helpers.try_activate_fallback` (not at conversation_loop call-sites, which were bare in both trees), and `test_429_retry_after_cooldown` + `test_auth_provider_failover` (20) pass.
- Earlier "subsumed by upstream" claims for #716/#510 were treated as ASSUMPTIONS and verified against tests — #716 was falsified (so it was re-applied, not dropped), #510 confirmed intact.

### Local verification (pypy 7.3.20)
`compileall` clean; all key modules + `run_agent` import. Targeted green (clean, single-threaded):
`test_error_classifier` 202, `test_web_providers` 25, `test_jobs` 121, `test_auxiliary_client` 301,
`test_cron` 8, `test_run_one_job` 6, `test_plugins` 100, `test_scheduler` 222, `test_delegate` 166,
`test_approval` 296, `test_models` 84, `test_terminal_tool` 33.
<!-- no-tmp: ok — historical note about a macOS symlink, not a path we write -->
Env-only non-failures excluded from the gate: `test_file_tools` 2 cases assert `/tmp` but macOS resolves
the symlink to `/private/tmp` (pass on Linux CI); `tests/hermes_cli/*` + 2 `test_approval` cases raise
`ModuleNotFoundError: prompt_toolkit` (optional TUI dep absent in the local pypy env). Full suite runs on
the PR's CI.

> **Method note:** an early broad run showed 726 "failures" — that was an artifact of running two pytest
> jobs concurrently (shared sqlite → `_sqlite` locks, process-group collisions → `killpg` guard, tmp/port
> races). Re-run single-threaded, the same files were green (e.g. `test_plugins` 14-fail → 100-pass,
> `test_models` 20-fail → 84-pass). Only single-threaded numbers are trusted above.
>
> **Full-suite local hang (not a merge issue):** a clean single-threaded `pytest tests/agent tests/cron
> tests/tools` stalls partway through `tests/agent` (after `test_save_url_image.py`). Pinpointed: the two
> tests at that boundary (`test_unique_filenames_avoid_collision`, `tests/agent/test_secret_scope.py`)
> **pass in isolation** (exit 0), and all involved files (`test_save_url_image.py`, `test_secret_scope.py`,
> `agent/secret_scope.py`) are **byte-identical to HEAD** (untouched by the merge). So the stall is a
> cross-test resource/thread-leak artifact of the long local run (leaked QueueListener runtime thread +
> retry/timeout backoff from missing local deps), reproducible on pre-merge main too — **not a merge
> regression.** The authoritative full-suite gate is the PR's CI (real deps, isolation, per-test timeouts).
