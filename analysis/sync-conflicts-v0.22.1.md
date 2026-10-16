# Sync conflict classification — `v0.22.1`

Total paths: **207**

## Verdicts
- upstream-wins: 0
- fork-keeps: 0
- manual: 207

## Per-bucket breakdown

| bucket | upstream-wins | fork-keeps | manual | total |
|---|---:|---:|---:|---:|
| .github | 0 | 0 | 35 | 35 |
| agent | 0 | 0 | 29 | 29 |
| cron | 0 | 0 | 4 | 4 |
| hermes_cli | 0 | 0 | 21 | 21 |
| tools | 0 | 0 | 34 | 34 |
| other | 0 | 0 | 84 | 84 |

## Per-path verdicts

| path | verdict | bucket |
|---|---|---|
| `.github/CODEOWNERS` | manual | .github |
| `.github/ISSUE_TEMPLATE/config.yml` | manual | .github |
| `.github/PULL_REQUEST_TEMPLATE.md` | manual | .github |
| `.github/workflows/ci-review-comment.yml` | manual | .github |
| `.github/workflows/ci.yaml` | manual | .github |
| `.github/workflows/contributor-check.yml` | manual | .github |
| `.github/workflows/deploy-site.yml` | manual | .github |
| `.github/workflows/docker-lint.yml` | manual | .github |
| `.github/workflows/docker.yml` | manual | .github |
| `.github/workflows/docs-site-checks.yml` | manual | .github |
| `.github/workflows/e2e-desktop-core.yml` | manual | .github |
| `.github/workflows/e2e-desktop.yml` | manual | .github |
| `.github/workflows/history-check.yml` | manual | .github |
| `.github/workflows/infographic-check.yml` | manual | .github |
| `.github/workflows/install-e2e-run.yml` | manual | .github |
| `.github/workflows/install-e2e.yml` | manual | .github |
| `.github/workflows/installer-tests.yml` | manual | .github |
| `.github/workflows/js-autofix.yml` | manual | .github |
| `.github/workflows/js-tests.yml` | manual | .github |
| `.github/workflows/lint.yml` | manual | .github |
| `.github/workflows/lockfile-diff.yml` | manual | .github |
| `.github/workflows/nix.yml` | manual | .github |
| `.github/workflows/osv-scanner.yml` | manual | .github |
| `.github/workflows/plugin-catalog-ci.yml` | manual | .github |
| `.github/workflows/profile-artifact-check.yml` | manual | .github |
| `.github/workflows/publish-e2e-evidence.yml` | manual | .github |
| `.github/workflows/review-labels.yml` | manual | .github |
| `.github/workflows/rust-tests.yml` | manual | .github |
| `.github/workflows/skills-index-freshness.yml` | manual | .github |
| `.github/workflows/skills-index.yml` | manual | .github |
| `.github/workflows/supply-chain-audit.yml` | manual | .github |
| `.github/workflows/tests-os.yml` | manual | .github |
| `.github/workflows/tests.yml` | manual | .github |
| `.github/workflows/uv-lockfile-check.yml` | manual | .github |
| `.github/workflows/windows-venv-e2e.yml` | manual | .github |
| `README.md` | manual | other |
| `agent/agent_init.py` | manual | agent |
| `agent/agent_runtime_helpers.py` | manual | agent |
| `agent/auxiliary_client.py` | manual | agent |
| `agent/background_review.py` | manual | agent |
| `agent/chat_completion_helpers.py` | manual | agent |
| `agent/context_compressor.py` | manual | agent |
| `agent/conversation_compression.py` | manual | agent |
| `agent/conversation_loop.py` | manual | agent |
| `agent/credential_pool.py` | manual | agent |
| `agent/display.py` | manual | agent |
| `agent/error_classifier.py` | manual | agent |
| `agent/file_safety.py` | manual | agent |
| `agent/inline_tool_executors.py` | manual | agent |
| `agent/insights.py` | manual | agent |
| `agent/memory_manager.py` | manual | agent |
| `agent/monitoring/events.py` | manual | agent |
| `agent/prompt_builder.py` | manual | agent |
| `agent/reasoning_timeouts.py` | manual | agent |
| `agent/retry_utils.py` | manual | agent |
| `agent/skill_commands.py` | manual | agent |
| `agent/subagent_lifecycle.py` | manual | agent |
| `agent/system_prompt.py` | manual | agent |
| `agent/tool_guardrails.py` | manual | agent |
| `agent/trajectory.py` | manual | agent |
| `agent/transports/chat_completions.py` | manual | agent |
| `agent/turn_context.py` | manual | agent |
| `agent/turn_facade.py` | manual | agent |
| `agent/turn_finalizer.py` | manual | agent |
| `agent/turn_retry_state.py` | manual | agent |
| `apps/desktop/src/app/contrib/hooks/use-session-tile-delegate.test.ts` | manual | other |
| `cli.py` | manual | other |
| `compat_manifest.json` | manual | other |
| `cron/jobs.py` | manual | cron |
| `cron/scheduler.py` | manual | cron |
| `cron/scheduler_provider.py` | manual | cron |
| `cron/scheduler_script.py` | manual | cron |
| `gateway/delivery.py` | manual | other |
| `gateway/display_config.py` | manual | other |
| `gateway/platforms/api_server.py` | manual | other |
| `gateway/platforms/api_server_openai_routes.py` | manual | other |
| `gateway/platforms/base.py` | manual | other |
| `gateway/run_turn.py` | manual | other |
| `gateway/session_transcript.py` | manual | other |
| `gateway/shutdown_flush.py` | manual | other |
| `gateway/stream_consumer_fallback.py` | manual | other |
| `hermes_cli/_subprocess_compat.py` | manual | hermes_cli |
| `hermes_cli/backup.py` | manual | hermes_cli |
| `hermes_cli/cli_chat_turn_mixin.py` | manual | hermes_cli |
| `hermes_cli/cli_commands_mixin.py` | manual | hermes_cli |
| `hermes_cli/config_defaults.py` | manual | hermes_cli |
| `hermes_cli/config_migrations.py` | manual | hermes_cli |
| `hermes_cli/cron.py` | manual | hermes_cli |
| `hermes_cli/dashboard_auth/middleware.py` | manual | hermes_cli |
| `hermes_cli/doctor_tools.py` | manual | hermes_cli |
| `hermes_cli/main.py` | manual | hermes_cli |
| `hermes_cli/mcp_catalog.py` | manual | hermes_cli |
| `hermes_cli/plugins.py` | manual | hermes_cli |
| `hermes_cli/profiles.py` | manual | hermes_cli |
| `hermes_cli/session_schema_history.py` | manual | hermes_cli |
| `hermes_cli/sessions_cmd.py` | manual | hermes_cli |
| `hermes_cli/skills_hub.py` | manual | hermes_cli |
| `hermes_cli/subcommands/sessions.py` | manual | hermes_cli |
| `hermes_cli/tools_config_post_setup.py` | manual | hermes_cli |
| `hermes_cli/update_cmd.py` | manual | hermes_cli |
| `hermes_cli/update_cmd_maint.py` | manual | hermes_cli |
| `hermes_cli/worktree_ops.py` | manual | hermes_cli |
| `hermes_state.py` | manual | other |
| `hermes_state_common.py` | manual | other |
| `hermes_state_messages.py` | manual | other |
| `hermes_state_portability.py` | manual | other |
| `model_tools.py` | manual | other |
| `nix/packages.nix` | manual | other |
| `plugin-catalog/custodian.yaml` | manual | other |
| `plugin-catalog/entropicmem.yaml` | manual | other |
| `plugins/memory/honcho/session.py` | manual | other |
| `plugins/memory/honcho/session_context.py` | manual | other |
| `plugins/memory/mem0/__init__.py` | manual | other |
| `plugins/memory/mem0/_backend.py` | manual | other |
| `plugins/platforms/homeassistant/adapter.py` | manual | other |
| `plugins/platforms/telegram/adapter.py` | manual | other |
| `pyproject.toml` | manual | other |
| `run_agent.py` | manual | other |
| `scripts/check_compat_pointers.py` | manual | other |
| `scripts/install.sh` | manual | other |
| `scripts/release.py` | manual | other |
| `scripts/run_tests.sh` | manual | other |
| `scripts/run_tests_parallel.py` | manual | other |
| `scripts/whatsapp-bridge/bridge.js` | manual | other |
| `setup-hermes.sh` | manual | other |
| `tests/agent/test_insights.py` | manual | other |
| `tests/agent/test_plugin_llm.py` | manual | other |
| `tests/agent/test_prompt_builder.py` | manual | other |
| `tests/agent/test_retry_utils.py` | manual | other |
| `tests/agent/test_tool_arg_coercion.py` | manual | other |
| `tests/agent/test_tool_guardrails.py` | manual | other |
| `tests/agent/test_verification_stop.py` | manual | other |
| `tests/conftest.py` | manual | other |
| `tests/cron/test_cron_provider_pin.py` | manual | other |
| `tests/cron/test_cron_script.py` | manual | other |
| `tests/e2e/core/cli_tmux/test_resize_scrollback.py` | manual | other |
| `tests/e2e/core/upgrade/_helpers.py` | manual | other |
| `tests/e2e/core/upgrade/test_upgrade_path.py` | manual | other |
| `tests/fixtures/resolution_allowlist.json` | manual | other |
| `tests/gateway/test_homeassistant.py` | manual | other |
| `tests/gateway/test_status.py` | manual | other |
| `tests/gateway/test_stream_consumer_continuation_cut.py` | manual | other |
| `tests/gateway/test_telegram_network.py` | manual | other |
| `tests/hermes_cli/test_computer_use_cli.py` | manual | other |
| `tests/hermes_cli/test_dashboard_param_clamps.py` | manual | other |
| `tests/hermes_cli/test_model_alias_credentials.py` | manual | other |
| `tests/hermes_cli/test_runtime_provider_resolution.py` | manual | other |
| `tests/hermes_cli/test_update_autostash.py` | manual | other |
| `tests/hermes_cli/test_worktree_selfheal.py` | manual | other |
| `tests/install/installer-script-e2e.sh` | manual | other |
| `tests/install/macos-desktop-e2e.sh` | manual | other |
| `tests/install/windows-e2e.ps1` | manual | other |
| `tests/plugins/memory/test_mem0_v3.py` | manual | other |
| `tests/scripts/install/test_install_sh_node_deps_failure.py` | manual | other |
| `tests/test_managed_runtime_resolution.py` | manual | other |
| `tests/tools/test_approval.py` | manual | other |
| `tests/tools/test_code_kernel.py` | manual | other |
| `tests/tools/test_delegate_toolset_scope.py` | manual | other |
| `tests/tools/test_memory_tool.py` | manual | other |
| `tests/tools/test_memory_tool_schema.py` | manual | other |
| `tests/tools/test_skills_hub.py` | manual | other |
| `tests/tools/test_subagent_steer.py` | manual | other |
| `tests/tools/test_terminal_tool.py` | manual | other |
| `tests/tools/test_toolsets.py` | manual | other |
| `tests/tools/test_transcription_tools.py` | manual | other |
| `tests/tools/test_voice_mode.py` | manual | other |
| `tests/tools/test_wake_word.py` | manual | other |
| `tests/tools/test_web_providers_ddgs.py` | manual | other |
| `tests/tui_gateway/test_tui_gateway_server.py` | manual | other |
| `tools/arg_coercion.py` | manual | tools |
| `tools/browser_tool.py` | manual | tools |
| `tools/checkpoint_manager.py` | manual | tools |
| `tools/code_execution_tool.py` | manual | tools |
| `tools/cronjob_tools.py` | manual | tools |
| `tools/delegate_tool.py` | manual | tools |
| `tools/environments/base.py` | manual | tools |
| `tools/environments/base_session_env.py` | manual | tools |
| `tools/file_operations.py` | manual | tools |
| `tools/file_operations_common.py` | manual | tools |
| `tools/file_tools.py` | manual | tools |
| `tools/fuzzy_match.py` | manual | tools |
| `tools/lazy_deps.py` | manual | tools |
| `tools/mcp_tool.py` | manual | tools |
| `tools/mcp_tool_config.py` | manual | tools |
| `tools/mcp_tool_handlers.py` | manual | tools |
| `tools/mcp_tool_registration.py` | manual | tools |
| `tools/mcp_tool_transport.py` | manual | tools |
| `tools/memory_tool.py` | manual | tools |
| `tools/memory_tool_store.py` | manual | tools |
| `tools/patch_parser.py` | manual | tools |
| `tools/skill_manager_guards.py` | manual | tools |
| `tools/skill_manager_tool.py` | manual | tools |
| `tools/skill_usage.py` | manual | tools |
| `tools/skills_hub.py` | manual | tools |
| `tools/skills_sync.py` | manual | tools |
| `tools/skills_tool.py` | manual | tools |
| `tools/subagent_worktree.py` | manual | tools |
| `tools/terminal_tool.py` | manual | tools |
| `tools/threat_patterns.py` | manual | tools |
| `tools/tool_search.py` | manual | tools |
| `tools/tool_search_validation.py` | manual | tools |
| `tools/transcription_tools.py` | manual | tools |
| `tools/web_tools.py` | manual | tools |
| `toolsets.py` | manual | other |
| `tui_gateway/methods_prompt.py` | manual | other |
| `tui_gateway/methods_session.py` | manual | other |
| `uv.lock` | manual | other |
