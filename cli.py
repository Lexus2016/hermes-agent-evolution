#!/usr/bin/env python3
"""Hermes Agent CLI — interactive terminal interface (``python cli.py --help`` for usage)."""

# Must be the very first import (UTF-8 stdio on Windows). Missing only mid-``hermes update``.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass

import logging
import os
import functools
import shutil  # noqa: F401 — tests patch shutil/time through the cli facade
import sys
import re
import atexit
import errno
import time  # noqa: F401 — see shutil
from collections import deque
from dataclasses import dataclass
from contextlib import contextmanager, suppress
from pathlib import Path
from datetime import datetime  # noqa: F401 — siblings import it lazily through cli
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

os.environ["HERMES_QUIET"] = "1"  # suppress our modules' startup chatter


from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from hermes_cli.cli_commands_mixin import CLICommandsMixin
from hermes_cli.cli_billing_mixin import CLIBillingMixin
from hermes_cli.cli_loops_mixin import CLILoopsMixin
from hermes_cli.cli_info_mixin import CLIInfoMixin
from hermes_cli.cli_terminal_mixin import CLITerminalMixin
from hermes_cli.cli_modal_mixin import CLIModalMixin
from hermes_cli.cli_stream_mixin import CLIStreamMixin
from hermes_cli.cli_session_mixin import CLISessionMixin
from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
from hermes_cli.cli_voice_mixin import CLIVoiceMixin
from hermes_cli.cli_status_bar_mixin import CLIStatusBarMixin
from hermes_cli.cli_tui_mixin import CLITuiMixin
from hermes_cli.cli_process_notifications import CLIProcessNotificationsMixin
from hermes_cli.cli_init_mixin import CLIInitMixin
from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
# Extracted clusters (mechanical split, #116911); re-exported here so `cli.<name>` stays the seam.
from hermes_cli.cli_shutdown import (  # noqa: F401,E402
    _CLEANUP_STEPS,
    _arm_exit_watchdog,
    _emit_interrupted_session_end,
    _exit_watchdog_timeout,
    _finalize_single_query,
    _float_env,
    _flush_logging_and_stdio,
    _flush_one_shot_session_store,
    _interrupt_async_delegations,
    _invoke_interrupted_session_end,
    _notify_session_finalize,
    _notify_single_query_session_finalize,
    _oneshot_agent_and_session,
    _should_emit_cleanup_session_finalize,
    _shutdown_agent_memory_provider,
    _shutdown_cached_aux_clients,
    _shutdown_mcp_servers,
    _stop_cli_wake_word,
    _sync_process_session_id,
    _wait_for_oneshot_background_completions,
)
from hermes_cli.cli_auto_maintenance import (  # noqa: F401,E402
    _run_checkpoint_auto_maintenance,
    _run_state_db_auto_maintenance,
)
from rich.console import Console
from hermes_cli.fallback_config import get_fallback_chain
from hermes_state_ids import new_session_id
from utils import base_url_host_matches, base_url_hostname, is_truthy_value
from hermes_cli.cli_render import (  # noqa: F401,E402
    ChatConsole,
    _ACCENT,
    _ACCENT_ANSI_DEFAULT,
    _BOLD,
    _DA1_REPLY_RE,
    _DIM,
    _FALSE_RE,
    _LIGHT_DEFAULT_TERM_PROGRAMS,
    _LIGHT_MODE_REMAP,
    _LIGHT_MODE_REMAP_UPPER,
    _REASONING_TAGS,
    _RST,
    _STREAM_PAD,
    _STREAM_PARTIAL_PREVIEW_LEN,
    _SkinAwareAnsi,
    _TOOL_CALL_TAGS,
    _TRUE_RE,
    _WINDOWS_PATH_WITH_DOT_SEGMENT_RE,
    _accent_hex,
    _add_suspect_rows,
    _append_blank_panel_line,
    _append_panel_line,
    _assistant_content_as_text,
    _assistant_copy_text,
    _b,
    _build_compact_banner,
    _clear_output_history,
    _cli_visible_print,
    _coerce_output_history_limit,
    _cprint,
    _d,
    _detect_light_mode_uncached,
    _heal_cooked_mode_drift,
    _hex_to_ansi,
    _install_skin_light_mode_hook,
    _line_rows,
    _luminance_from_hex,
    _maybe_remap_for_light_mode,
    _output_history_lines,
    _output_history_recording,
    _output_history_rows,
    _output_tail_fitting,
    _painted_columns,
    _PaintedLine,
    _panel_box_width,
    _post_stream_transform_output,
    _prepend_note_to_message,
    _preserve_windows_dot_segments_for_markdown,
    _pt_app_is_running,
    _pt_print_ansi,
    _query_osc11_background,
    _record_output_history,
    _record_output_history_entry,
    _release_paints,
    _render_final_assistant_content,
    _rich_text_from_ansi,
    _set_chrome_floor,
    _strip_markdown_syntax,
    _strip_reasoning_tags,
    _terminal_columns,
    _terminal_reflows,
    _terminal_width_for_streaming,
    _tty_wrap,
    _wrap_panel_text,
    _wrap_panel_text_keep_ws,
)
from hermes_cli.cli_config_load import (  # noqa: F401,E402
    _AUXILIARY_TASK_ENV,
    _CWD_PLACEHOLDERS,
    _TERMINAL_ENV_MAPPINGS,
    _cli_config_defaults,
    _init_logging_and_display_from_config,
    _load_prefill_messages,
    _merge_file_config,
    _mirror_config_to_env,
    _parse_reasoning_config,
    _parse_service_tier_config,
    _resolve_prefill_messages_file,
    load_cli_config,
)
from hermes_cli.cli_terminal_input import (  # noqa: F401,E402
    _BACKSLASH_LINE_CONTINUATION_RE,
    _DSR_CPR_ESC_RE,
    _DSR_CPR_VISIBLE_RE,
    _EXTENDED_ENTER_KEYS_SEQ,
    _IMAGE_EXTENSIONS,
    _KITTY_KEYBOARD_PUSH_SEQ,
    _MODIFY_OTHER_KEYS_SEQ,
    _SGR_MOUSE_BARE_RE,
    _SGR_MOUSE_ESC_RE,
    _SGR_MOUSE_VISIBLE_RE,
    _TERMINAL_INPUT_MODE_RESET_SEQ,
    _apply_backslash_line_continuation,
    _apply_bracketed_paste_timeout_patch,
    _bind_prompt_submit_keys,
    _build_cpr_disabled_output,
    _cli_multiline_shortcuts_enabled,
    _collect_query_images,
    _detect_file_drop,
    _disable_prompt_toolkit_cpr_warning,
    _enable_extended_enter_keys,
    _estimate_tui_input_height,
    _file_drop_result,
    _format_image_attachment_badges,
    _hermes_call_output_screen_diff,
    _is_backslash_line_continuation,
    _is_ghostty_terminal,
    _preserve_ctrl_enter_newline,
    _resolve_attachment_path,
    _select_classic_cli_pt_output,
    _should_auto_attach_clipboard_image_on_paste,
    _split_path_input,
    _status_bar_visible_from_display_config,
    _strip_leaked_terminal_responses_with_meta,
    _terminal_may_leak_cpr,
    _terminal_supports_extended_enter_keys,
    _termux_example_image_path,
)
from hermes_cli.cli_single_query import (  # noqa: F401,E402
    _TERMINAL_PROVIDER_REASONS,
    _TRANSIENT_PROVIDER_REASONS,
    _collect_kanban_task_images,
    _configure_quiet_agent,
    _install_single_query_signal_handlers,
    _int_or,
    _interrupt_agent_for_signal,
    _route_single_query_images,
    _run_kanban_goal_loop_chat,
    _run_kanban_goal_loop_q,
    _run_quiet_single_query,
    _run_single_query_mode,
    _single_query_exit_code,
    _sync_cli_session_id_from_agent,
)

from prompt_toolkit.patch_stdout import patch_stdout
try:
    from prompt_toolkit.enums import EditingMode
except ImportError:  # partial prompt_toolkit stubs in tests
    EditingMode = None
from prompt_toolkit import print_formatted_text as _pt_print
from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
try:
    from prompt_toolkit.cursor_shapes import CursorShape
    _STEADY_CURSOR = CursorShape.BLOCK
except (ImportError, AttributeError):
    _STEADY_CURSOR = None

try:
    from hermes_cli import pt_input_extras as _pt_extras

    _pt_extras.install_shift_enter_alias()
    _pt_extras.install_ctrl_enter_alias()
    _pt_extras.install_cmd_backspace_alias()
    _pt_extras.install_modify_other_keys_aliases()
    _pt_extras.install_keypress_data_normalization()
    _pt_extras.install_ignored_terminal_sequences()
    del _pt_extras
except Exception:
    pass
import threading
import queue


def _lazy_shim(module: str, name: str, alias: str | None = None):
    """Import ``module.name`` on first call; keeps heavy imports off startup while ``cli.<name>`` stays patchable."""
    import importlib

    def shim(*args, **kwargs):
        return getattr(importlib.import_module(module), name)(*args, **kwargs)

    shim.__name__ = shim.__qualname__ = alias or name
    return shim


def format_duration_compact(*args, **kwargs):
    seconds = float(args[0] if args else kwargs.get("seconds", 0.0))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        remaining_min = int(minutes % 60)
        return f"{int(hours)}h {remaining_min}m" if remaining_min else f"{int(hours)}h"
    days = hours / 24
    return f"{days:.1f}d"


# model id -> shortest configured alias (process-lifetime cache; config is read once).
_REVERSE_ALIAS_CACHE: dict[str, str] | None = None


def _reverse_alias_for_display(model_name: str) -> str:
    """Shortest alias for ``model_name`` from ``model_aliases:`` or ``model.aliases:``, else ``model_name``."""
    global _REVERSE_ALIAS_CACHE
    if not model_name:
        return model_name
    if _REVERSE_ALIAS_CACHE is None:
        rmap: dict[str, str] = {}

        def _put(m: str, alias: str) -> None:
            if m and (m not in rmap or len(alias) < len(rmap[m])):
                rmap[m] = alias

        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            ma = cfg.get("model_aliases")
            if isinstance(ma, dict):
                for alias, entry in ma.items():
                    if isinstance(entry, dict):
                        _put(str(entry.get("model", "") or "").strip(), alias)
            mdl = cfg.get("model", {}) or {}
            if isinstance(mdl, dict):
                simple = mdl.get("aliases")
                if isinstance(simple, dict):
                    for alias, val in simple.items():
                        if isinstance(val, str) and val.strip():
                            v = val.strip()
                            _put(v.split("/", 1)[1] if "/" in v else v, alias)
        except Exception:
            pass
        _REVERSE_ALIAS_CACHE = rmap
    return _REVERSE_ALIAS_CACHE.get(model_name, model_name)


def format_token_count_compact(*args, **kwargs):
    value = int(args[0] if args else kwargs.get("value", 0))
    abs_value = abs(value)
    if abs_value < 1_000:
        return str(value)

    sign = "-" if value < 0 else ""
    units = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for threshold, suffix in units:
        if abs_value >= threshold:
            scaled = abs_value / threshold
            text = f"{scaled:.{2 if scaled < 10 else 1 if scaled < 100 else 0}f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"

    return f"{value:,}"


realign_markdown_tables = _lazy_shim("agent.markdown_tables", "realign_markdown_tables")

_COMMAND_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


# ~/.hermes/.env first, project .env as dev fallback; user env files override stale shell exports.
from hermes_constants import get_hermes_home
from hermes_cli.env_loader import load_hermes_dotenv

_hermes_home = get_hermes_home()
_project_env = Path(__file__).parent / '.env'
load_hermes_dotenv(hermes_home=_hermes_home, project_env=_project_env)


CLI_CONFIG = load_cli_config()


_init_logging_and_display_from_config()

# Neuter AsyncHttpxClientWrapper.__del__ before any AsyncOpenAI client exists: it
# schedules aclose() on the running loop (prompt_toolkit's, during idle), closing
# transports bound to dead worker loops ("Event loop is closed" / "Press ENTER to
# continue..."). A meta_path finder patches ``openai._base_client`` at first import —
# eager import costs ~166ms/30MB cold, and the patch is guaranteed to land before
# instantiation. See ``agent.auxiliary_client.neuter_async_httpx_del``.
try:
    import sys as _httpx_neuter_sys
    import importlib.util as _httpx_neuter_imp_util

    class _AsyncHttpxDelNeuter:
        """Patch ``AsyncHttpxClientWrapper.__del__`` to a no-op when ``openai._base_client`` loads."""

        _armed = True

        def find_spec(self, fullname, path=None, target=None):
            if not self._armed or fullname != "openai._base_client":
                return None
            # Disarm before delegating so the recursive find_spec doesn't loop through us.
            self._armed = False
            try:
                _httpx_neuter_sys.meta_path.remove(self)
            except ValueError:
                pass
            spec = _httpx_neuter_imp_util.find_spec(fullname)
            if spec is None or spec.loader is None:
                return None
            _orig_exec = spec.loader.exec_module

            def _patched_exec(module):
                _orig_exec(module)
                try:
                    cls = getattr(module, "AsyncHttpxClientWrapper", None)
                    if cls is not None:
                        cls.__del__ = lambda self: None  # type: ignore[assignment]
                except Exception:
                    pass

            spec.loader.exec_module = _patched_exec  # type: ignore[method-assign]
            return spec

    _httpx_neuter_sys.meta_path.insert(0, _AsyncHttpxDelNeuter())
except Exception:
    pass


# Agent/tool systems load lazily: bare startup only needs the prompt.
def get_tool_definitions(*args, **kwargs):
    from hermes_cli.mcp_startup import wait_for_mcp_discovery
    from model_tools import get_tool_definitions as _get_tool_definitions

    wait_for_mcp_discovery()
    return _get_tool_definitions(*args, **kwargs)


validate_toolset = _lazy_shim("toolsets", "validate_toolset")


_cleanup_all_terminals = _lazy_shim("tools.terminal_tool", "cleanup_all_environments", "_cleanup_all_terminals")
set_sudo_password_callback = _lazy_shim("tools.terminal_tool", "set_sudo_password_callback")
set_approval_callback = _lazy_shim("tools.terminal_tool", "set_approval_callback")
set_secret_capture_callback = _lazy_shim("tools.skills_tool", "set_secret_capture_callback")
_cleanup_all_browsers = _lazy_shim("tools.browser_tool_lifecycle", "_emergency_cleanup_all_sessions", "_cleanup_all_browsers")

_cleanup_done = False  # _run_cleanup runs exactly once
_cleanup_in_progress = False
_cli_wake_owner = None
# One-shot finalization runs before process cleanup (plugins see the boundary while the
# agent is attached); atexit cleanup must not finalize those sessions again.
_single_query_finalize_attempted_session_ids: set[str | None] = set()
# /handoff sessions belong to the gateway: finalizing them here would stamp end_reason on
# a row the gateway just reopened, making the handoff leg vanish from history.
# Session IDs that were handed off to the gateway via /handoff. The CLI process exits after a successful
# handoff, but the gateway now owns the session lifecycle — _run_cleanup must NOT call finalize_session on
# these, because doing so sets end_reason on a row the gateway just reopened and is actively writing to
# (#88234). The race made the handoff leg vanish from session history and broke session_search recall for
# the handed-off session.
_handed_off_session_ids: set[str | None] = set()
_active_agent_ref = None  # active AIAgent, for memory-provider shutdown at exit
_deferred_agent_startup_done = False
# Set once the TUI app starts (focus reporting + mouse tracking on); gates the on-exit
# terminal reset so non-TUI one-shot runs never emit codes for modes they never enabled.
_tui_input_modes_active = False


# Set True once the TUI's prompt_toolkit app starts (which enables focus reporting + mouse tracking). Gates
# the on-exit terminal reset so non-TUI one-shot CLI runs — which also register _run_cleanup via atexit —
# don't emit escape codes for modes they never enabled (#36823).
def _mark_tui_input_modes_active() -> None:
    """Record that the TUI app started, so _run_cleanup resets input modes."""
    global _tui_input_modes_active
    _tui_input_modes_active = True


def _prepare_deferred_agent_startup() -> None:
    """Run Termux-deferred agent discovery before the first real agent turn."""
    global _deferred_agent_startup_done
    if _deferred_agent_startup_done:
        return
    if os.environ.get("HERMES_DEFER_AGENT_STARTUP") != "1":
        return
    _deferred_agent_startup_done = True
    _accept_hooks = os.environ.get("HERMES_ACCEPT_HOOKS", "").lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception:
        logger.warning("plugin discovery failed at deferred CLI startup", exc_info=True)
    try:
        from hermes_cli.mcp_startup import start_background_mcp_discovery

        start_background_mcp_discovery(logger=logger, thread_name="termux-cli-mcp-discovery")
    except Exception:
        logger.debug("MCP tool discovery failed at deferred CLI startup", exc_info=True)
    try:
        from agent.shell_hooks import register_from_config
        from agent.outbound_webhooks import register_from_config as register_outbound_webhooks
        from hermes_cli.config import load_config

        _hooks_cfg = load_config()
        register_from_config(_hooks_cfg, accept_hooks=_accept_hooks)
        register_outbound_webhooks(_hooks_cfg)
    except Exception:
        logger.debug("shell-hook registration failed at deferred CLI startup", exc_info=True)


_signal_watchdog_armed = False


def _arm_exit_watchdog_on_shutdown_signal() -> None:
    """Arm the exit backstop the moment a termination signal arrives (idempotent; never raises).

    The graceful unwind has wedge points BEFORE ``_run_cleanup`` arms its own watchdog
    (main thread in a syscall, prompt_toolkit teardown never returning). Leash is 2x
    the cleanup timeout so a progressing cleanup is never cut short. Never arm at
    startup: the timer exits unconditionally.

    SIGTERM/SIGHUP establish unambiguous shutdown intent, but the graceful path from signal →
    ``agent.interrupt()`` → ``app.exit()`` / ``KeyboardInterrupt`` → ``finally`` → ``_run_cleanup`` has
    several wedge points BEFORE ``_run_cleanup`` arms the normal watchdog: a main thread parked in a syscall
    that never observes the unwind, a prompt_toolkit teardown that never returns, or an agent worker
    blocking the ``finally``. When that happens the process has NO backstop and a "dead" CLI lingers
    (observed: ``hermes --tui`` alive ~47 min at 4% CPU after terminal close — the #65998 class).
    """
    global _signal_watchdog_armed
    if _signal_watchdog_armed:
        return
    _signal_watchdog_armed = True
    base = _exit_watchdog_timeout()
    if base <= 0:
        return  # explicitly disabled
    with suppress(Exception):  # never let the backstop break signal handling
        _arm_exit_watchdog(timeout_s=base * 2, from_signal=True)


def _run_cleanup(*, notify_session_finalize: bool = True):
    """Run resource cleanup exactly once."""
    global _cleanup_done, _cleanup_in_progress
    if _cleanup_done:
        return
    _cleanup_done = True
    _cleanup_in_progress = True

    try:
        _arm_exit_watchdog()
        # Reset terminal input modes FIRST: teardown below can take seconds and a later
        # step raising must not skip the reset. No-op unless the TUI ran.
        # See #36823.
        _reset_terminal_input_modes_on_exit()

        for step, swallow in _CLEANUP_STEPS:
            with suppress(swallow):
                globals()[step]()
        if notify_session_finalize:
            cleanup_session_id = _active_agent_ref.session_id if _active_agent_ref else None
            if _should_emit_cleanup_session_finalize(cleanup_session_id):
                _notify_session_finalize(session_id=cleanup_session_id, platform="cli", reason="shutdown")
        try:
            _shutdown_agent_memory_provider(_active_agent_ref)
        except Exception as e:
            logger.warning("CLI cleanup memory shutdown failed: %s", e, exc_info=True)
    finally:
        _cleanup_in_progress = False


def _reset_terminal_input_modes_on_exit() -> None:
    """Disable focus reporting + mouse tracking on TUI exit (best-effort).

    Ctrl+C / SIGTERM / crashes bypass prompt_toolkit's unwind, leaving focus events and
    mouse reports as visible text in the next shell. Writes to stdout when it is the
    terminal, else /dev/tty (the TUI may have run with stdout redirected).

    Called from ``_run_cleanup`` (atexit-registered + invoked on the normal / EOF / interrupt exit paths)
    this covers normal quit, Ctrl+C and SIGTERM/SIGHUP. ``kill -9`` is uncatchable, and the kanban worker's
    ``os._exit(0)`` path bypasses ``atexit``; neither runs this — but both are non-TTY / non-TUI, so there
    is nothing to reset there. See #36823.
    """
    global _tui_input_modes_active
    if not _tui_input_modes_active:
        return
    # Clear first so a re-armed _run_cleanup doesn't re-emit.
    _tui_input_modes_active = False
    try:
        stream = sys.stdout
        if stream is not None and stream.isatty():
            stream.write(_TERMINAL_INPUT_MODE_RESET_SEQ)
            stream.flush()
            return
    except Exception:
        pass
    with suppress(Exception), open("/dev/tty", "w", encoding="ascii") as tty:
        tty.write(_TERMINAL_INPUT_MODE_RESET_SEQ)
        tty.flush()


from hermes_cli.worktree_ops import (
    _git_quiet,
    _git_repo_root,
    _maintain_pack_health,
    _prune_stale_worktrees,
    _repo_is_shallow,
    _setup_worktree,
    _worktree_has_unpushed_commits,
    _worktree_is_dirty,
    release_lsp_clients,
)

# ============================================================================= Git Worktree Isolation
# (#652) =============================================================================
_active_worktree: Optional[Dict[str, str]] = None


def _cleanup_worktree(info: Dict[str, str] = None) -> None:
    """Remove a worktree and its branch on exit; kept only when it has unpushed commits."""
    global _active_worktree
    info = info or _active_worktree
    if not info:
        return

    wt_path, branch, repo_root = info["path"], info["branch"], info["repo_root"]
    if not Path(wt_path).exists():
        return

    if _worktree_has_unpushed_commits(wt_path, timeout=10):
        if _repo_is_shallow(repo_root):
            # Shallow boundary makes the unpushed verdict unreliable; the startup pruner reaps later.
            _cprint(f"\n\033[33m⚠ Shallow clone — cannot verify push state, keeping: {wt_path}\033[0m")
            print("  The next `hermes -w` session deepens the clone and prunes merged worktrees automatically.")
        else:
            _cprint(f"\n\033[33m⚠ Worktree has unpushed commits, keeping: {wt_path}\033[0m")
            print(f"  To clean up manually: git worktree remove --force {wt_path}")
        _active_worktree = None
        return

    # Release the tree's language servers while the path still exists, then unlock so `remove`
    # isn't blocked by the lock placed at creation. Fail-soft.
    release_lsp_clients(wt_path)
    _git_quiet(["worktree", "unlock", wt_path], repo_root, log="git worktree unlock failed (non-fatal)")
    _git_quiet(["worktree", "remove", wt_path, "--force"], repo_root, timeout=15, log="Failed to remove worktree")
    _git_quiet(["branch", "-D", branch], repo_root, log=f"Failed to delete branch {branch}")

    _active_worktree = None
    _cprint(f"\033[32m✓ Worktree cleaned up: {wt_path}\033[0m")


def _run_state_db_auto_maintenance(session_db) -> None:
    """One-time repairs + auto-archive/prune/vacuum per the ``sessions:`` config. Never raises."""
    if session_db is None:
        return
    try:
        from hermes_cli.config import load_config as _load_full_config
        from hermes_constants import get_hermes_home as _get_hermes_home  # lazy: tests patch it
        _hermes_home_maint = _get_hermes_home()

        # One-time repairs, each latched in state_meta once it has run.
        for meta_key, repair, done_msg, skip_msg in (
            (
                "ghost_session_prune_v1",
                lambda: session_db.prune_empty_ghost_sessions(sessions_dir=_hermes_home_maint / "sessions"),
                "Pruned %d empty TUI ghost sessions", "Ghost session prune skipped: %s",
            ),
            (
                "orphaned_compression_finalize_v1",
                session_db.finalize_orphaned_compression_sessions,
                "Finalized %d orphaned compression sessions", "Orphan compression finalize skipped: %s",
            ),
        ):
            try:
                if not session_db.get_meta(meta_key):
                    count = repair()
                    session_db.set_meta(meta_key, "1")
                    if count:
                        logger.info(done_msg, count)
            except Exception as _exc:
                logger.debug(skip_msg, _exc)

        cfg = (_load_full_config().get("sessions") or {})

        # Auto-archive is independent of auto_prune: run it before prune's early return.
        if cfg.get("auto_archive", False):
            session_db.maybe_auto_archive(
                idle_days=float(cfg.get("auto_archive_days", 3)),
                min_interval_hours=int(cfg.get("min_interval_hours", 24)),
            )

        if not cfg.get("auto_prune", False):
            return
        session_db.maybe_auto_prune_and_vacuum(
            retention_days=int(cfg.get("retention_days", 90)),
            min_interval_hours=int(cfg.get("min_interval_hours", 24)),
            min_vacuum_interval_days=int(cfg.get("min_vacuum_interval_days", 30)),
            vacuum=bool(cfg.get("vacuum_after_prune", True)),
            sessions_dir=_hermes_home_maint / "sessions",
            db_size_vacuum_threshold=int(
                cfg.get("db_size_vacuum_threshold", 768 * 1024 * 1024)
            ),
        )
    except Exception as exc:
        logger.debug("state.db auto-maintenance skipped: %s", exc)


def _run_checkpoint_auto_maintenance() -> None:
    """Checkpoint store retention on a daemon thread: its ``git gc`` can block for tens of seconds
    on a large store, which used to stall the prompt once a day. ``auto_prune_from_config`` owns the
    config gate and the 24h marker and never raises."""
    from tools.checkpoint_manager import auto_prune_from_config
    threading.Thread(target=auto_prune_from_config, name="checkpoint-auto-prune", daemon=True).start()


_ACCENT_ANSI_DEFAULT = "\033[1;38;2;255;215;0m"  # #FFD700 bold fallback
_BOLD = "\033[1m"
_RST = "\033[0m"
_STREAM_PAD = ""  # no indent: leading whitespace pollutes copy/paste
_STREAM_PARTIAL_PREVIEW_LEN = 60  # tail of an unfinished line mirrored into the spinner


def _hex_to_ansi(hex_color: str, *, bold: bool = False) -> str:
    """Convert '#RRGGBB' to a true-color ANSI escape, remapping dark-tuned colors in light mode."""
    hex_color = _maybe_remap_for_light_mode(hex_color)
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return f"\033[{'1;' if bold else ''}38;2;{r};{g};{b}m"
    except (ValueError, IndexError):
        return _ACCENT_ANSI_DEFAULT if bold else "\033[38;2;184;134;11m"


# Light/dark terminal detection (mirrors ui-tui/src/theme.ts detectLightMode()). Priority:
# HERMES_LIGHT/HERMES_TUI_LIGHT env, HERMES_TUI_THEME, HERMES_TUI_BACKGROUND, COLORFGBG
# (bg slot 7/15 = light), OSC 11 query, default dark. Cached so the terminal is queried once.
_LIGHT_MODE_CACHE: bool | None = None


def _detect_light_mode() -> bool:
    global _LIGHT_MODE_CACHE
    if _LIGHT_MODE_CACHE is not None:
        return _LIGHT_MODE_CACHE
    try:
        result = _detect_light_mode_uncached()
    except Exception:
        result = False
    _LIGHT_MODE_CACHE = result
    return result


_install_skin_light_mode_hook()


# Prime the light-mode cache when interactive so OSC 11 happens before prompt_toolkit owns the tty.
with suppress(Exception):
    if sys.stdin.isatty() and sys.stdout.isatty():
        _detect_light_mode()


_OUTPUT_HISTORY_ENABLED = True
_OUTPUT_HISTORY_REPLAYING = False
_OUTPUT_HISTORY_SUPPRESSED = False
_OUTPUT_HISTORY_MAX_LINES = 200
_OUTPUT_HISTORY = deque(maxlen=_OUTPUT_HISTORY_MAX_LINES)


def _configure_output_history(enabled: bool, max_lines=200) -> None:
    """Configure recent CLI output replayed after terminal redraws."""
    global _OUTPUT_HISTORY_ENABLED, _OUTPUT_HISTORY_MAX_LINES, _OUTPUT_HISTORY
    _OUTPUT_HISTORY_ENABLED = bool(enabled)
    _OUTPUT_HISTORY_MAX_LINES = _coerce_output_history_limit(max_lines)
    _OUTPUT_HISTORY = deque(maxlen=_OUTPUT_HISTORY_MAX_LINES)


@contextmanager
def _suspend_output_history():
    global _OUTPUT_HISTORY_SUPPRESSED
    old_value = _OUTPUT_HISTORY_SUPPRESSED
    _OUTPUT_HISTORY_SUPPRESSED = True
    try:
        yield
    finally:
        _OUTPUT_HISTORY_SUPPRESSED = old_value


def _replay_output_history(fit=None, output=None) -> None:
    """Repaint recent output above the prompt after a full screen clear.

    ``fit=(rows, columns, painted, top)`` replays only the newest lines whose wrapped height
    fits ``rows`` (see ``_output_tail_fitting``) — the older ones are still in scrollback
    (#95375) — from screen row ``top`` when known (``_set_chrome_floor``). ``output``: paint
    now, straight to this prompt_toolkit output, where the caller just erased the viewport and
    reset the renderer — ``run_in_terminal`` would first erase below the top row, which
    scroll-on-clear terminals (tmux) take as a clear and copy the blank screen into scrollback.
    """
    global _OUTPUT_HISTORY_REPLAYING
    if not _OUTPUT_HISTORY_ENABLED or not _OUTPUT_HISTORY:
        return
    _OUTPUT_HISTORY_REPLAYING = True
    try:
        rendered_lines = _output_history_lines()
        top = None
        if fit is not None:
            rows, columns, painted, top = fit
            rendered_lines = _output_tail_fitting(rendered_lines, rows, columns, painted)
        if rendered_lines:
            # One payload: per-line pt prints each force a sync redraw (a waterfall of old output).
            if output is None:
                _pt_print(_PT_ANSI("\n".join(rendered_lines)))
            else:
                from prompt_toolkit.renderer import print_formatted_text as _paint_formatted_text
                from prompt_toolkit.styles import Style
                _paint_formatted_text(output, _PT_ANSI("\n".join(rendered_lines) + "\n"), Style([]))
                size = output.get_size()
                if top is not None:  # the chrome's top is now this many rows down
                    top += sum(_line_rows(line, columns) for line in rendered_lines)
                    _set_chrome_floor(max(0, size.rows - top))
                    if size.columns != columns:
                        _add_suspect_rows(top + 1 - size.rows)
            width = _painted_columns() if fit is None else columns
            for line in rendered_lines:  # repainted: they wrap at today's width from now on
                if isinstance(line, _PaintedLine):
                    line.width = width
    except Exception:
        pass
    finally:
        _OUTPUT_HISTORY_REPLAYING = False


def _pt_print_ansi(text: str) -> None:
    """``_pt_print(ANSI(text))``, falling back to ``print`` when stdout is not a real console."""
    try:
        _pt_print(_PT_ANSI(text))
    except Exception:
        # NoConsoleScreenBufferError (Windows) / OSError when stdout is e.g. a worker log file.
        with suppress(Exception):
            print(text)


def _cprint(text: str):
    """Print ANSI text through prompt_toolkit's renderer (patch_stdout swallows raw ANSI).

    From a background thread while an Application runs, a direct print races the input
    redraw and gets buried, so those go through ``run_in_terminal`` via ``call_soon_threadsafe``.
    """
    _record_output_history(text)

    try:
        from prompt_toolkit.application import get_app_or_none, run_in_terminal
    except Exception:
        _pt_print(_PT_ANSI(text))
        return

    try:
        app = get_app_or_none()
    except Exception:
        app = None

    if app is None or not getattr(app, "_is_running", False):
        _pt_print_ansi(text)
        return

    import asyncio as _asyncio

    try:
        loop = app.loop  # type: ignore[attr-defined]
    except Exception:
        loop = None
    try:
        # get_running_loop(): get_event_loop() warns from threads with no current loop.
        # Use get_running_loop() instead of get_event_loop() to avoid the DeprecationWarning /
        # RuntimeWarning emitted by Python 3.10+ when get_event_loop() is called from a thread that has no
        # current event loop set (e.g. the process_loop background thread). Fixes #19285.
        current_loop = _asyncio.get_running_loop()
    except Exception:
        current_loop = None
    if loop is None or (current_loop is loop and loop.is_running()):
        _pt_print(_PT_ANSI(text))
        return

    def _schedule():
        # run_in_terminal() returns an awaitable (pt >= 3.0) that must be scheduled or the
        # output is dropped, or None (mocks / older pt) when it already ran synchronously.
        # Never fall back to a bare print on error: the sync path already printed.
        with suppress(Exception):
            import inspect as _inspect
            coro = run_in_terminal(lambda: _pt_print(_PT_ANSI(text)))
            if coro is not None and (_inspect.isawaitable(coro) or _inspect.iscoroutine(coro)):
                _asyncio.ensure_future(coro)

    try:
        loop.call_soon_threadsafe(_schedule)
    except Exception:
        _pt_print_ansi(text)


def _prepend_note_to_message(message, note: str):
    """Prepend a one-shot note to a user message (str, or content-part list when an image is attached).

    For lists the note is folded into the first text part or inserted as a leading one.
    Unknown shapes are returned unchanged.
    """
    note = str(note or "").strip()
    if not note:
        return message
    if isinstance(message, str):
        return f"{note}\n\n{message}" if message else note
    if isinstance(message, list):
        parts = list(message)
        for i, part in enumerate(parts):
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                parts[i] = {**part, "text": f"{note}\n\n{text}" if text else note}
                return parts
        return [{"type": "text", "text": note}, *parts]
    return message


def _get_cron_failure_digest_for_user() -> Optional[str]:
    """Return a formatted digest of recent unacked cron failures, if enabled.

    Returns ``None`` otherwise.  The underlying implementation updates ack
    timestamps only when it actually emits a digest, so calling this on every
    user turn is safe and will not repeat the same failure.
    """
    try:
        from cron.scheduler import build_cron_failure_digest

        return build_cron_failure_digest()
    except Exception:
        return None



def _pt_app_is_running() -> bool:
    """Whether a prompt_toolkit Application currently owns the live terminal."""
    try:
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
    except Exception:
        return False
    return app is not None and bool(getattr(app, "_is_running", False))


def _cli_visible_print(text: str = "") -> None:
    """``print`` unless a prompt_toolkit Application owns the terminal (patch_stdout swallows bare prints)."""
    if _pt_app_is_running():
        _cprint(text)
    else:
        print(text)


_IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.bmp', '.tiff', '.tif', '.svg', '.ico',
})


def _termux_example_image_path(filename: str = "cat.png") -> str:
    """Return a realistic example media path for the current Termux setup."""
    candidates = [
        os.path.expanduser("~/storage/shared"),
        "/sdcard",
        "/storage/emulated/0",
        "/storage/self/primary",
    ]
    # Literal "/" so the Android hint is right even on Windows.
    for root in candidates:
        if os.path.isdir(root):
            return f"{root}/Pictures/{filename}"
    return f"~/storage/shared/Pictures/{filename}"


def _split_path_input(raw: str) -> tuple[str, str]:
    r"""Split a leading path token (quoted or with ``\ `` escapes) from trailing free-form text."""
    raw = str(raw or "").strip()
    if not raw:
        return "", ""

    if raw[0] in {'"', "'"}:
        quote = raw[0]
        pos = 1
        while pos < len(raw):
            ch = raw[pos]
            if ch == '\\' and pos + 1 < len(raw):
                pos += 2
                continue
            if ch == quote:
                return raw[1:pos], raw[pos + 1 :].strip()
            pos += 1
        return raw[1:], ""

    pos = 0
    while pos < len(raw):
        ch = raw[pos]
        if ch == '\\' and pos + 1 < len(raw) and raw[pos + 1] == ' ':
            pos += 2
        elif ch == ' ':
            break
        else:
            pos += 1

    return raw[:pos].replace('\\ ', ' '), raw[pos:].strip()


def _resolve_attachment_path(raw_path: str) -> Path | None:
    """Resolve a user-supplied attachment path (quotes, ``~``, env vars, ``file://``; relative to TERMINAL_CWD).

    Returns ``None`` unless it resolves to an existing file.
    """
    token = str(raw_path or "").strip()
    if not token:
        return None

    if token[0] == token[-1] and token[0] in {'"', "'"}:
        token = token[1:-1].strip()
    token = token.replace('\\ ', ' ')
    if not token:
        return None

    expanded = token
    if token.startswith("file://"):
        try:
            parsed = urlparse(token)
            if parsed.scheme == "file":
                expanded = unquote(parsed.path or "")
                if parsed.netloc and os.name == "nt":
                    expanded = f"//{parsed.netloc}{expanded}"
                elif os.name == "nt" and len(expanded) >= 3 and expanded[0] == "/" and expanded[1].isalpha() and expanded[2] == ":":
                    # file:///C:/... parses to path "/C:/..." — drop the leading slash
                    # so it resolves as a drive-letter path.
                    expanded = expanded[1:]
        except Exception:
            expanded = token
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    if os.name != "nt":
        normalized = expanded.replace("\\", "/")
        if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "/" and normalized[0].isalpha():
            expanded = f"/mnt/{normalized[0].lower()}/{normalized[3:]}"
    path = Path(expanded)
    if not path.is_absolute():
        base_dir = Path(os.getenv("TERMINAL_CWD", os.getcwd()))
        path = base_dir / path

    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    # ENAMETOOLONG for a pasted `/goal <long prose>` that passed the `/` prefilter
    # would otherwise reach process_loop and silently lose the input.
    try:
        if not resolved.exists() or not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


def _file_drop_result(path: Path, remainder: str) -> dict:
    return {"path": path, "is_image": path.suffix.lower() in _IMAGE_EXTENSIONS, "remainder": remainder}


def _detect_file_drop(user_input: str) -> "dict | None":
    """Detect a dragged/pasted file path at the start of *user_input* -> ``{path, is_image, remainder}`` or None."""
    if not isinstance(user_input, str):
        return None

    stripped = user_input.strip()
    if not stripped:
        return None

    # Optionally quoted; then /, ~, ./, ../, a Windows drive prefix, or (unquoted) file://.
    quoted = stripped[:1] in {"'", '"'}
    unquoted = stripped[1:] if quoted else stripped
    starts_like_path = (
        unquoted.startswith(("/", "~", "./", "../"))
        or (not quoted and unquoted.startswith("file://"))
        or (len(unquoted) >= 3 and unquoted[1] == ":" and unquoted[2] in {"\\", "/"} and unquoted[0].isalpha())
    )
    if not starts_like_path:
        return None

    direct_path = _resolve_attachment_path(stripped)
    if direct_path is not None:
        return _file_drop_result(direct_path, "")

    first_token, remainder = _split_path_input(stripped)
    drop_path = _resolve_attachment_path(first_token)
    if drop_path is None and " " in stripped and not quoted:
        for pos in reversed([idx for idx, ch in enumerate(stripped) if ch == " "]):
            drop_path = _resolve_attachment_path(stripped[:pos].rstrip())
            if drop_path is not None:
                remainder = stripped[pos + 1 :].strip()
                break
    if drop_path is None:
        return None
    return _file_drop_result(drop_path, remainder)


def _format_image_attachment_badges(attached_images: list[Path], image_counter: int, width: int | None = None) -> str:
    """Attached-image badge row: compact summary on narrow terminals, per-image badges otherwise."""
    if not attached_images:
        return ""

    width = width or shutil.get_terminal_size((80, 24)).columns

    def _trunc(name: str, limit: int) -> str:
        return name if len(name) <= limit else name[: max(1, limit - 3)] + "..."

    if width < 52:
        if len(attached_images) == 1:
            return f"[📎 {_trunc(attached_images[0].name, 20)}]"
        return f"[📎 {len(attached_images)} images attached]"

    if width < 80:
        if len(attached_images) == 1:
            return f"[📎 {_trunc(attached_images[0].name, 32)}]"
        return f"[📎 {_trunc(attached_images[0].name, 20)}] [+{len(attached_images) - 1}]"

    base = image_counter - len(attached_images) + 1
    return " ".join(f"[📎 Image #{base + i}]" for i in range(len(attached_images)))


def _should_auto_attach_clipboard_image_on_paste(pasted_text: str) -> bool:
    """Auto-attach clipboard images only for image-only paste gestures."""
    return not pasted_text.strip()


_strip_leaked_bracketed_paste_wrappers = _lazy_shim(
    "hermes_cli.input_sanitize", "strip_leaked_bracketed_paste_wrappers", "_strip_leaked_bracketed_paste_wrappers"
)


# OSC sequences (e.g. OSC-8 links): pt's ANSI parser strips the ESC but leaks the payload as text.
_OSC_ESCAPE_RE = re.compile(r"\x1b\][\s\S]*?(?:\x07|\x1b\\)")


def _looks_like_slash_command(text: str) -> bool:
    """``/help`` yes, ``/Users/x/file.md`` no: a command's first word has no further ``/``."""
    if not text or not text.startswith("/"):
        return False
    return "/" not in text.split()[0][1:]


_skill_commands = None
_skill_bundles = None


def _slash_args(cmd: str) -> str:
    """Text after the slash-command word, stripped ("" when absent)."""
    parts = cmd.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _ensure_skill_commands() -> dict:
    global _skill_commands
    if _skill_commands is None:
        from agent.skill_commands import scan_skill_commands

        _skill_commands = scan_skill_commands()
    return _skill_commands


def get_skill_commands() -> dict:
    return _ensure_skill_commands()


build_skill_invocation_message = _lazy_shim("agent.skill_commands", "build_skill_invocation_message")
build_preloaded_skills_prompt = _lazy_shim("agent.skill_commands", "build_preloaded_skills_prompt")


def get_skill_bundles() -> dict:
    global _skill_bundles
    if _skill_bundles is None:
        from agent.skill_bundles import get_skill_bundles as _impl

        _skill_bundles = _impl()
    return _skill_bundles


build_bundle_invocation_message = _lazy_shim("agent.skill_bundles", "build_bundle_invocation_message")


def _get_plugin_cmd_handler_names() -> set:
    """Return plugin command names (without slash prefix) for dispatch matching."""
    try:
        from hermes_cli.plugins import get_plugin_commands
        return set(get_plugin_commands().keys())
    except Exception:
        return set()


def _parse_skills_argument(skills: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize a CLI skills flag into a deduplicated list of skill identifiers."""
    if not skills:
        return []
    raw_values = [str(item) for item in skills if item is not None] if isinstance(skills, (list, tuple)) else [str(skills)]
    parts = (p.strip() for raw in raw_values for p in raw.split(","))
    return list(dict.fromkeys(p for p in parts if p))


def save_config_value(key_path: str, value: any) -> bool:
    """Persist dot-separated ``key_path`` = value into HERMES_HOME/config.yaml; True on success.

    Never the repo's cli-config.yaml: no config reader loads it, so the value would vanish.
    """
    config_path = get_hermes_home() / 'config.yaml'

    try:
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(config_path.parent)
        from utils import atomic_roundtrip_yaml_update
        atomic_roundtrip_yaml_update(config_path, key_path, value)
        try:  # owner-only: config files contain API keys
            os.chmod(config_path, 0o600)
        except (OSError, NotImplementedError):
            pass
        return True
    except Exception as e:
        logger.error("Failed to save config: %s", e)
        return False


def _normalize_moa_model(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``moa:<preset>`` -> ``("moa", preset)`` (same routing as ``/moa``); anything else -> ``(None, model)``.

    Returns ``("moa", "<preset>")`` when *model* selects the MoA virtual provider, otherwise ``(None,
    model)`` unchanged. This gives non-interactive ``hermes chat -Q -m moa:<preset>`` the same routing the
    interactive ``/moa`` command and the model picker already use: ``resolve_runtime_provider`` handles
    ``requested_provider == "moa"`` and ``agent_init`` builds the MoAClient off ``provider == "moa"``.
    Without this the raw ``moa:<preset>`` string is sent to the real provider and rejected with a 401/400
    "model not supported" (#56828).
    """
    if isinstance(model, str) and model.strip().lower().startswith("moa:"):
        preset = model.strip().split(":", 1)[1].strip()
        if preset:
            return "moa", preset
    return None, model

_split_model_config_default = _lazy_shim("hermes_cli.config", "split_model_config_default", "_split_model_config_default")


class _VoiceInputMessage:
    """Sentinel for voice-transcribed input so the concise voice prefix never applies to typed text.

    Distinguishes STT output from manually typed text while voice mode is active, so the
    concise-voice-response prefix is applied only to messages that actually came from the microphone
    (#65827).
    """

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text

    def __str__(self) -> str:
        return self.text


class _SeededQueryMessage:
    """Sentinel for a ``-q`` prompt seeded into an interactive session; treated LITERALLY (no slash/!/file-drop)."""

    __slots__ = ("text", "images")

    def __init__(self, text: str, images=None):
        self.text = text or ""
        self.images = list(images or [])

    def __str__(self) -> str:
        return self.text


def _should_seed_interactive(query, image, quiet: bool, oneshot: bool) -> bool:
    """``-q`` seeds an interactive session only on a real TTY without ``--oneshot``/``-Q`` (automation answers and exits)."""
    if not (query or image) or oneshot or quiet:
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


@dataclass
class _ChatTurn:
    """Per-turn state shared by the ``chat()`` phases and the agent worker thread.

    ``result`` is written by the worker and read after the join; ``tts_normal_exit`` is
    set only when the TTS worker drained on its own so the last sentence is never cut.
    """

    result: Optional[dict] = None
    mute_notification_reply: bool = False
    use_streaming_tts: bool = False
    box_opened: bool = False
    thinking_started: bool = False
    text_queue: Optional[queue.Queue] = None
    tts_thread: Optional[threading.Thread] = None
    stream_callback: Optional[Any] = None
    stop_event: Optional[threading.Event] = None
    tts_normal_exit: bool = False
    voice_prefix: str = ""
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin


_PASTE_REF_RE = re.compile(r'\[Pasted text #\d+: \d+ lines \u2192 (.+?)\]')


class HermesCLI(CLIInitMixin, CLITuiRuntimeMixin, CLIProcessNotificationsMixin, CLIAgentSetupMixin, CLICommandsMixin, CLIBillingMixin, CLITuiMixin, CLIStatusBarMixin, CLIVoiceMixin, CLIModelSwitchMixin, CLISessionMixin, CLIStreamMixin, CLIModalMixin, CLITerminalMixin, CLIInfoMixin, CLILoopsMixin, CLIChatTurnMixin):
    """Interactive REPL for the Hermes Agent."""

    # Seeded -q first message (see _should_seed_interactive); run() re-creates
    # _pending_input, so it is enqueued only after the fresh queue exists.
    _seeded_first_message: Optional["_SeededQueryMessage"] = None
    # Inspection surfaces (banner, /tools, status line) read this on partially built instances too.
    disabled_toolsets: Optional[List[str]] = None

    def __init__(
        self,
        model: str = None,
        toolsets: List[str] = None,
        provider: str = None,
        reasoning: str = None,
        api_key: str = None,
        base_url: str = None,
        max_turns: int = None,
        run_budget: float = None,
        verbose: Optional[bool] = None,
        compact: bool = False,
        resume: str = None,
        checkpoints: bool = False,
        pass_session_id: bool = False,
        ignore_rules: bool = False,
    ):
        """CLI args win over config; ``reasoning`` is per-run only; ``resume`` restores history from SQLite."""
        self._init_display_options(verbose, compact)
        self._init_model_routing(model, toolsets, provider, reasoning, api_key, base_url, max_turns, run_budget,
                                 checkpoints, pass_session_id, ignore_rules)
        self._init_runtime_state(resume)

    def _init_display_options(self, verbose, compact):
        """Display-related config: compact/tool-progress/focus view, bells, streaming, previews, stream buffers."""
        self.console = Console()
        self.config = CLI_CONFIG
        display = CLI_CONFIG["display"]
        self.compact = compact if compact is not None else display.get("compact", False)
        # tool_progress: "off" | "new" | "all" | "verbose"; YAML 1.1 parses bare `off` as False.
        _raw_tp = display.get("tool_progress", "all")
        self.tool_progress_mode = "off" if _raw_tp is False else str(_raw_tp)
        # focus_view (/focus) is display-only: snaps tool_progress to "off" (stashing the
        # pre-focus mode for /focus off); never changes what is sent to the model.
        self._focus_view_enabled = bool(display.get("focus_view", False))
        self._focus_saved_tool_progress = self._focus_last_counted_tool = None
        self._focus_hidden_lines = 0
        if self._focus_view_enabled:
            from hermes_cli.focus_view import FOCUS_TOOL_PROGRESS_MODE, normalize_tool_progress_mode

            self._focus_saved_tool_progress = normalize_tool_progress_mode(self.tool_progress_mode)
            self.tool_progress_mode = FOCUS_TOOL_PROGRESS_MODE
        self.resume_display = display.get("resume_display", "full")  # "full" | "minimal"
        self.bell_on_complete = display.get("bell_on_complete", False)
        self.bell_on_prompt = display.get("bell_on_prompt", False)  # bell when a blocking modal opens
        self.show_reasoning = display.get("show_reasoning", True)
        self.reasoning_full = display.get("reasoning_full", False)
        _configure_output_history(
            enabled=display.get("persistent_output", True),
            max_lines=display.get("persistent_output_max_lines", 200),
        )
        # busy_input_mode: "interrupt" (redirect the run) | "queue" (next turn) | "steer" (inject mid-run).
        _bim = str(display.get("busy_input_mode", "interrupt")).strip().lower()
        self.busy_input_mode = _bim if _bim in ("queue", "steer") else "interrupt"

        # verbose ONLY controls global DEBUG logging; tool_progress="verbose" is independent
        # (coupling them spewed every module's DEBUG logs to the console).
        self.verbose = bool(verbose) if verbose is not None else False

        self.streaming_enabled = display.get("streaming", False)
        self.show_timestamps = display.get("timestamps", False)
        self.timestamp_format = display.get("timestamp_format", "%H:%M")
        _frm = str(display.get("final_response_markdown", "strip")).strip().lower()
        self.final_response_markdown = _frm if _frm in {"render", "strip", "raw"} else "strip"

        self._inline_diffs_enabled = display.get("inline_diffs", True)

        # Per-turn accounting: CLI-only chrome riding the tool-progress feed.
        self._turn_summary_enabled = bool(display.get("turn_summary", True))
        self._spinner_token_flow_enabled = bool(display.get("spinner_token_flow", True))
        self._turn_summary_collector = None
        self._turn_summary_start = 0.0
        self._turn_token_baseline = 0
        self._interactive_turn = False  # only run()-loop turns; keeps the summary line off -Q

        _ump = display.get("user_message_preview", {})
        _ump = _ump if isinstance(_ump, dict) else {}
        self.user_message_preview_first_lines = max(1, _int_or(_ump.get("first_lines", 2), 2))
        self.user_message_preview_last_lines = max(0, _int_or(_ump.get("last_lines", 2), 2))

        # Streaming display state
        self._stream_buf = ""  # partial line buffer
        self._reasoning_preview_buf = ""  # coalesces tiny reasoning chunks
        self._stream_started = self._stream_box_opened = self._stream_box_live = False
        self._held_status_lines: list[str] = []  # agent status lines parked while a box streams
        # Possible markdown-table lines held until the block ends for wcwidth-aware re-padding.
        self._stream_table_buf: list[str] = []
        self._in_stream_table = False
        self._pending_edit_snapshots = {}
        self._last_input_mode_recovery = self._last_termios_drift_check = None  # None = never; monotonic epoch is arbitrary
        self._input_mode_recovery_notice_shown = self._termios_drift_notice_shown = False

    def _init_model_routing(self, model, toolsets, provider, reasoning, api_key, base_url, max_turns, run_budget, checkpoints, pass_session_id, ignore_rules):
        """Resolve model/provider/base_url, turn limits, toolsets, checkpoints, prompt/personality, reasoning + routing config."""
        self._init_model_and_provider(model, provider, api_key, base_url)
        self._init_turn_limits(max_turns, run_budget)
        self._init_toolsets(toolsets)
        self._init_checkpoints_and_rules(checkpoints, pass_session_id, ignore_rules)
        self._init_prompt_and_reasoning(reasoning)

    def _init_model_and_provider(self, model, provider, api_key, base_url):
        """Priority: CLI args > env vars > config file."""
        # LLM_MODEL/OPENAI_MODEL env vars are deliberately NOT checked (multi-agent setups
        # would stomp each other through the environment).
        _model_config = CLI_CONFIG["model"]
        # A dict-valued default carries its own provider, which must feed requested_provider
        # instead of being replaced by the merged model.provider (typically "auto").
        _config_model, _nested_provider = _split_model_config_default(
            _model_config.get("default") or _model_config.get("model") or ""
        )
        # resume must not clobber an explicit -m with the session's stored model.
        self._explicit_model_override = bool(model)
        self.model = model or _config_model or ""
        _cfg_provider = _model_config.get("provider") or os.getenv("HERMES_INFERENCE_PROVIDER")
        _startup_provider_override = _startup_base_url_override = _startup_api_key_override = ""
        if self.model:
            from hermes_cli.model_switch import resolve_startup_model_route

            _startup_route = resolve_startup_model_route(
                self.model,
                explicit_provider=provider or "",
                current_provider=(provider or _nested_provider or _cfg_provider or ""),
                user_providers=CLI_CONFIG.get("providers"),
                custom_providers=CLI_CONFIG.get("custom_providers"),
            )
            if _startup_route is not None:
                self.model = _startup_route.model
                _startup_provider_override = _startup_route.provider
                _startup_base_url_override = _startup_route.base_url
                _startup_api_key_override = _startup_route.api_key
        # ``moa:<preset>`` selects the MoA virtual provider before provider resolution so the
        # real provider never sees the unknown model; the prefix wins over --provider.
        # A ``moa:<preset>`` model string selects the MoA virtual provider in one shot (parity with
        # interactive ``/moa`` and the model picker). See #56828.
        _moa_provider_override, self.model = _normalize_moa_model(self.model)

        if self.model == "":  # auto-detect from a local server
            _base_url = _model_config.get("base_url") or ""
            if base_url_hostname(_base_url) in ("localhost", "127.0.0.1"):
                from hermes_cli.runtime_provider import _auto_detect_local_model
                self.model = _auto_detect_local_model(_base_url) or self.model
        # Provider normalisation may silently override the default but must warn for an
        # explicit choice (a config model equal to the global fallback is NOT explicit).
        self._model_is_default = not model and not _config_model

        # --api-key wins; otherwise a URL-bearing startup alias carries its own credential.
        # See #28660.
        self._explicit_api_key = api_key or _startup_api_key_override or None
        # A startup alias endpoint is not a --base-url flag. Leaving this None lets
        # runtime resolution replace it with model.base_url (the openrouter default).
        self._explicit_base_url = base_url or _startup_base_url_override or None

        # Resolved lazily at use-time via _ensure_runtime_credentials().
        self.requested_provider = (
            _moa_provider_override or provider or _startup_provider_override or _nested_provider
            or _cfg_provider or "auto"
        )
        # `--provider <custom>` without `-m` uses that entry's default_model, else the global
        # default goes to the custom endpoint and the compressor gets the wrong context length.
        # Explicit `-m` still wins. See #86978.
        if not model and provider:
            try:
                from hermes_cli.runtime_provider import _get_named_custom_provider

                _named_custom = _get_named_custom_provider(provider)
            except Exception as exc:
                logger.warning(
                    "Could not resolve --provider %s default model; keeping global model.default (%s)",
                    provider, exc,
                )
                _named_custom = None
            _provider_default = str((_named_custom or {}).get("model") or "").strip()
            if _provider_default:
                self.model = _provider_default
                self._model_is_default = False
        self._provider_source: Optional[str] = None
        self.provider = self.requested_provider
        self.api_mode = "chat_completions"
        self.acp_command: Optional[str] = None
        self.acp_args: list[str] = []
        self.base_url = (
            base_url or _startup_base_url_override or _model_config.get("base_url", "")
            or os.getenv("OPENROUTER_BASE_URL", "")
        ) or None
        # Key matches the resolved base_url; re-resolved by _ensure_runtime_credentials().
        _keys = ("OPENROUTER_API_KEY", "OPENAI_API_KEY")
        if not (self.base_url and base_url_host_matches(self.base_url, "openrouter.ai")):
            _keys = _keys[::-1]
        self.api_key = api_key or os.getenv(_keys[0]) or os.getenv(_keys[1])

    def _init_turn_limits(self, max_turns, run_budget):
        """max_turns: CLI arg > config > env var > default; run budget: CLI flag > config."""
        # resolve_turn_limit() accepts "none"/"unlimited" (-> sys.maxsize) alongside ints.
        # KEEP the root-level CLI_CONFIG["max_turns"] fallback: it is never migrated on disk
        # and other config paths may bypass the load-time fold.
        from hermes_cli.config import resolve_turn_limit as _resolve_turn_limit
        self.max_turns = _resolve_turn_limit(next(
            (v for v in (max_turns, CLI_CONFIG["agent"].get("max_turns"), CLI_CONFIG.get("max_turns")) if v is not None),
            os.getenv("HERMES_MAX_ITERATIONS"),
        ))
        self.run_budget_seconds = run_budget if run_budget is not None else CLI_CONFIG["agent"].get("run_budget_seconds")

    def _init_toolsets(self, toolsets):
        self.enabled_toolsets = toolsets
        from agent.skill_utils import parse_config_string_list

        self.disabled_toolsets = parse_config_string_list(CLI_CONFIG["agent"].get("disabled_toolsets"))

        if toolsets and "all" not in toolsets and "*" not in toolsets:
            # Plugin toolsets ("jev" etc.) land in the registry only when discovery runs; without
            # this they fail validate_toolset() and raise a false "Unknown toolsets" warning.
            try:
                from hermes_cli.plugins import discover_plugins

                discover_plugins()
            except Exception:
                logger.warning("plugin discovery failed during toolset validation", exc_info=True)
            # MCP server names only resolve after discover_mcp_tools runs; skip them here.
            mcp_names = set((CLI_CONFIG.get("mcp_servers") or {}).keys())
            # Plugin toolsets register on a background thread that may not have landed.
            # Names it already declared are not typos (#71650).
            try:
                from hermes_cli.plugins import get_plugin_toolset_keys_nowait
                plugin_ts_names = get_plugin_toolset_keys_nowait()
            except Exception:
                plugin_ts_names = set()
            invalid = [t for t in toolsets
                       if not validate_toolset(t) and t not in mcp_names and t not in plugin_ts_names]
            if invalid:
                self._console_print(f"[bold red]Warning: Unknown toolsets: {', '.join(invalid)}[/]")

    def _init_checkpoints_and_rules(self, checkpoints, pass_session_id, ignore_rules):
        cp_cfg = CLI_CONFIG.get("checkpoints", {})
        if isinstance(cp_cfg, bool):
            cp_cfg = {"enabled": cp_cfg}
        self.checkpoints_enabled = checkpoints or cp_cfg.get("enabled", False)
        self.checkpoint_max_snapshots = cp_cfg.get("max_snapshots", 20)
        self.checkpoint_max_total_size_mb = cp_cfg.get("max_total_size_mb", 500)
        self.checkpoint_max_file_size_mb = cp_cfg.get("max_file_size_mb", 10)
        self.pass_session_id = pass_session_id
        # --ignore-rules: AIAgent skips context files (AGENTS.md/SOUL.md/...) and memory.
        self.ignore_rules = ignore_rules or is_truthy_value(os.environ.get("HERMES_IGNORE_RULES"))

    def _init_prompt_and_reasoning(self, reasoning):
        """Ephemeral system prompt/prefill, reasoning + service tier, OpenRouter routing knobs, fallback chain."""
        # Env var wins, then hermes_cli.personality (single owner of overlay resolution).
        from hermes_cli.personality import available_personalities, resolve_ephemeral_system_prompt

        self.system_prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "") or resolve_ephemeral_system_prompt(CLI_CONFIG)
        self.personalities = available_personalities(CLI_CONFIG)

        self.prefill_messages = _load_prefill_messages(_resolve_prefill_messages_file(CLI_CONFIG))

        # Per-model override > global reasoning_effort.
        # Reasoning config (OpenRouter reasoning effort level) Per-model override > global reasoning_effort
        # — resolved through the shared chokepoint in hermes_constants (Closes #21256).
        from hermes_constants import resolve_reasoning_config
        self.reasoning_config = resolve_reasoning_config(CLI_CONFIG, self.model)
        self._explicit_reasoning_config = None
        # --reasoning wins for this run only (never persisted); unparseable -> warn and ignore.
        # Recorded so an auth-fallback model swap does not re-resolve over the flag.
        if reasoning is not None and str(reasoning).strip():
            _cli_reasoning = _parse_reasoning_config(reasoning)
            if _cli_reasoning is None:
                logger.warning("Unknown --reasoning '%s', keeping the configured level", reasoning)
            else:
                self.reasoning_config = _cli_reasoning
                self._explicit_reasoning_config = _cli_reasoning
        self.service_tier = _parse_service_tier_config(CLI_CONFIG["agent"].get("service_tier", ""))

        pr = CLI_CONFIG.get("provider_routing", {}) or {}
        self._provider_sort = pr.get("sort")
        self._providers_only = pr.get("only")
        self._providers_ignore = pr.get("ignore")
        self._providers_order = pr.get("order")
        self._provider_require_params = pr.get("require_parameters", False)
        self._provider_data_collection = pr.get("data_collection")

        # OpenRouter Pareto Code router coding-score floor; out-of-range = unset.
        _raw_score = (CLI_CONFIG.get("openrouter", {}) or {}).get("min_coding_score")
        self._openrouter_min_coding_score: Optional[float] = None
        if _raw_score not in {None, ""}:
            try:
                _f = float(_raw_score)
                if 0.0 <= _f <= 1.0:
                    self._openrouter_min_coding_score = _f
            except (TypeError, ValueError):
                pass

        self._fallback_model = get_fallback_chain(CLI_CONFIG)

    def _init_runtime_state(self, resume):
        """Session store + all per-run mutable state (queues, overlays, pet/voice/status-bar fields)."""
        # A signature change across turns (/model, credential rotation) rebuilds the agent.
        self._active_agent_route_signature = None
        self.agent: Optional[Any] = None  # initialized on first use
        self._tool_callbacks_installed = self._tirith_security_checked = False
        self._app = None  # prompt_toolkit Application (set in run())

        self.conversation_history: List[Dict[str, Any]] = []
        self.session_start = datetime.now()
        # Per-prompt elapsed timer shown in the status bar.
        self._prompt_start_time: Optional[float] = None
        self._prompt_duration: float = 0.0
        self._last_turn_finished_at: Optional[float] = None
        self._init_session_store()
        self._pending_title: Optional[str] = None
        self._resumed = bool(resume)
        self.session_id = resume or new_session_id(self.session_start)
        getattr(self, "_write_terminal_breadcrumb", lambda: None)()

        self._history_file = _hermes_home / ".hermes_history"
        self._last_invalidate: float | None = None  # throttles UI repaints (None = never; monotonic epoch is arbitrary)
        self._init_ui_state()

    def _init_session_store(self):
        """Open the session store early (so /title works before the first message) + opportunistic maintenance."""
        self._session_db = None
        self._session_db_unavailable = False
        try:
            # Registry handle, not a bare SessionDB(): goals/loops/heartbeat acquire the same
            # path a moment later from the REPL thread, and a second writer repeats the full
            # open (the /proc-wide deleted-WAL scan, ~4k readlinks) while the render thread
            # holds the GIL — that repeat was the post-banner freeze before the first prompt.
            from hermes_state_registry import acquire
            self._session_db = acquire()
        except Exception as e:
            # Without a store the transcript is NOT persisted while the chat looks healthy,
            # so surface it prominently rather than only logging.
            # #41386: a failed session store means the transcript is NOT persisted to state.db — the live
            # chat looks healthy but resume later shows a truncated/empty session. A buried log line is not
            # enough; surface it prominently so the user knows persistence is off for this run and can fix
            # the store before relying on resume.
            self._session_db_unavailable = True
            logger.warning("Failed to initialize SessionDB — session will NOT be indexed for search: %s", e)
            from hermes_state_user_copy import describe_storage_failure, storage_failure_details
            failure = describe_storage_failure(e)
            try:
                Console(stderr=True).print(
                    "[bold yellow]⚠ Session store unavailable[/bold yellow] — "
                    "this conversation will [bold]NOT be saved[/bold] and cannot be resumed later. "
                    "Searching past sessions is also disabled.\n"
                    f"  Reason: {failure.gloss}.\n"
                    f"  {failure.action}\n"
                    f"  [dim]Details: {storage_failure_details(e)}[/dim]"
                )
            except Exception:
                print(
                    "WARNING: Session store unavailable — this conversation will NOT be "
                    f"saved and cannot be resumed later. Reason: {failure.gloss}. {failure.action}"
                )
        _run_state_db_auto_maintenance(self._session_db)
        _run_checkpoint_auto_maintenance()

    def _init_ui_state(self):
        """Per-run mutable UI state; must exist before any chat() call since -q never goes through run()."""
        self._pending_input = queue.Queue()
        self._interrupt_queue = queue.Queue()
        self._agent_running = self._should_exit = False
        self._last_turn_interrupted = False  # /goal never auto-queues on a Ctrl+C'd turn
        self._terminal_io_broken = False  # stdout EIO: freeze UI paints instead of spinning
        self._delete_session_on_exit = False  # /exit --delete
        # /update: relaunch() runs from run() after prompt_toolkit restored terminal modes.
        # /exit --delete: when True, the current session's SQLite history and on-disk transcripts are
        # deleted during shutdown. Set by process_command() when the user runs /exit --delete or /quit
        # --delete. Ported from google-gemini/gemini-cli#19332.
        self._pending_relaunch: list[str] | None = None
        self._last_ctrl_c_time = 0
        # Blocking-prompt overlays (clarify / sudo / approval / slash-confirm / model picker).
        self._clarify_state = self._clarify_multi_base = None
        self._clarify_freetext = False
        self._clarify_prefill = ""
        self._sudo_state = self._modal_input_snapshot = self._approval_state = None
        self._slash_confirm_state = self._model_picker_state = None
        self._clarify_deadline = self._sudo_deadline = self._approval_deadline = self._slash_confirm_deadline = 0
        self._approval_lock = threading.Lock()
        try:  # composer placeholder chosen once so it stays stable on screen
            from hermes_cli.tips import get_random_composer_placeholder
            self._composer_placeholder = get_random_composer_placeholder()
        except Exception:
            self._composer_placeholder = ""
        self._command_palette_state = self._secret_state = None
        self._pending_resume_sessions = None  # armed by a bare `/resume`; the next bare number selects
        self._pending_agent_seed = None  # one-shot seed from a slash handler
        self._secret_deadline = 0
        self._tool_start_time: float = 0.0
        self._pending_tool_info: dict = {}  # function_name -> [(preview, args)] for stacked scrollback
        self._spinner_text = self._command_status = ""
        self._last_scrollback_tool: str = ""  # "new" mode dedup
        self._command_running = self._command_blocks_input = False
        # Petdex mascot (display.pet): kitty placeholders on kitty/Ghostty, half-blocks elsewhere.
        self._pet_renderer = self._pet_anim_thread = None
        self._pet_slug = self._pet_kitty_pending = ""
        self._pet_enabled = self._pet_anim_running = False
        self._pet_cols: int = 18
        self._pet_scale: float = 0.7
        self._pet_frames_cache: dict = {}
        self._pet_kitty_cache: dict = {}
        self._pet_kitty_image_id = self._pet_frame_idx = 0
        self._pet_lock = threading.Lock()
        self._pet_cfg_checked = self._pet_event_until = 0.0
        self._pet_event: str = ""
        self._pet_reasoning = self._pet_turn_error = False
        self._attached_images: list[Path] = []
        self._image_counter = 0
        # Ctrl+S prompt stash; in-memory only because drafts routinely contain secrets.
        from hermes_cli.prompt_stash import PromptStash as _PromptStash
        self._prompt_stash = _PromptStash()
        self.preloaded_skills: list[str] = []
        self._startup_skills_line_shown = False
        # skills.auto_load rendered in the preload thread; None until joined. Handed to every
        # agent this CLI builds so the prompt bytes never depend on when the agent was created.
        self._auto_load_skills_result: Optional[tuple] = None
        # Background --skills preload, joined by finalize_preloaded_skills before any agent is built.
        self._preload_skills_thread: Optional[threading.Thread] = None
        self._preload_skills_result: Optional[tuple] = None
        self._preload_skills_error: Optional[BaseException] = None
        self._preload_skills_requested: list = []
        self._preload_skills_finalized = False
        self._active_session_lease = None

        # Voice mode state (also reinitialized inside run() for interactive TUI).
        self._voice_lock = threading.Lock()
        self._voice_mode = self._voice_tts = self._voice_recording = False
        self._voice_processing = self._voice_continuous = False
        self._voice_recorder = self._voice_tts_stop = None
        self._voice_tts_done = threading.Event()
        self._voice_tts_done.set()
        self._voice_barge_capture = threading.Event()  # barge monitor is capturing the interruption
        self._voice_last_tts_text = ""  # echo guard
        self._voice_barge_phase = None  # "generation" | "playback"

        self._status_bar_visible = _status_bar_visible_from_display_config(CLI_CONFIG.get("display"))
        self._battery_visible = bool(CLI_CONFIG["display"].get("battery", False))
        # Vi/vim editing mode for the input composer (display.vim_mode, config-only).
        # Off by default: prompt_toolkit's standard emacs bindings.
        self._vim_mode = bool(CLI_CONFIG["display"].get("vim_mode", False))
        # Hide rules + status bar until the next input after a resize, so SIGWINCH cannot
        # stamp a fresh status bar over one the terminal just reflowed into scrollback.
        self._status_bar_suppressed_after_resize = self._resize_recovery_pending = False
        self._resize_recovery_lock = threading.Lock()
        self._resize_recovery_timer = self._status_bar_unsuppress_timer = None  # latter: debounced un-suppress
        self._last_resize_width = None  # width change (reflow, needs viewport clear) vs rows-only

        self._background_tasks: Dict[str, threading.Thread] = {}
        self._background_task_counter = 0

        # Cache-hit baseline, reset on model switch / compression so the bar shows the current regime.
        self._cache_hit_baseline_prompt = self._cache_hit_baseline_read = self._cache_hit_baseline_compressions = 0
        self._cache_hit_baseline_model: Optional[str] = None

    def _claim_active_session(self, surface: str = "cli", *, stderr: bool = False) -> bool:
        """Claim a global active-session slot for this CLI process."""
        if self._active_session_lease is not None:
            return True
        try:
            from hermes_cli.active_sessions import format_refusal_stderr, try_acquire_active_session

            lease, message = try_acquire_active_session(
                session_id=self.session_id,
                surface=surface,
                config=self.config,
                # Writer identity: a re-claim by this process replaces its own entry.
                # See #94595.
                metadata={"live_session_id": str(self.session_id)},
            )
        except Exception as exc:
            logger.warning("Failed to claim active session slot: %s", exc)
            return True
        if message:
            print(format_refusal_stderr(message), file=sys.stderr) if stderr else self._console_print(f"[bold red]{message}[/]")
            return False
        self._active_session_lease = lease
        with suppress(Exception):
            atexit.register(self._release_active_session)
        return True

    def _release_active_session(self) -> None:
        lease = getattr(self, "_active_session_lease", None)
        if lease is None:
            return
        try:
            lease.release()
        except Exception:
            logger.debug("Failed to release active session slot", exc_info=True)
        finally:
            self._active_session_lease = None

    _PET_FRAME_INTERVAL = 0.16
    _PET_CFG_INTERVAL = 2.5

    def _install_tool_callbacks(self) -> None:
        """Install tool callbacks that need the live prompt UI."""
        if self._tool_callbacks_installed:
            return
        set_sudo_password_callback(self._sudo_password_callback)
        set_approval_callback(self._approval_callback)
        set_secret_capture_callback(self._secret_capture_callback)
        from agent.vault_backends.unlock import set_code_prompt_callback, set_save_login_prompt_callback, set_unlock_prompt_callback
        set_unlock_prompt_callback(self._vault_unlock_callback)
        set_save_login_prompt_callback(self._vault_save_login_callback)
        set_code_prompt_callback(self._vault_code_callback)
        self._tool_callbacks_installed = True

    def _ensure_tirith_security(self) -> None:
        """Check tirith availability once before tools can run terminal commands."""
        if self._tirith_security_checked:
            return
        self._tirith_security_checked = True
        try:
            from tools.tirith_security import ensure_installed, is_platform_supported

            if (
                ensure_installed(log_failures=False) is None and is_platform_supported()
                and (self.config.get("security", {}) or {}).get("tirith_enabled", True)
            ):
                _cprint(
                    f"  {_DIM}⚠ tirith security scanner enabled but not available "
                    f"— command scanning will use pattern matching only{_RST}"
                )
        except Exception:
            pass

    def _show_security_advisories(self):
        """Startup banner for unacked security advisories, on stderr (piped stdout stays clean); 24h rate-limited."""
        try:
            from hermes_cli.security_advisories import detect_compromised, startup_banner

            banner = startup_banner(detect_compromised())
            if banner:
                print(banner, file=sys.stderr, flush=True)
        except Exception:
            pass  # never block startup

    def _show_browser_backend_notice(self):
        """Once-per-24h hint when the default Browser Use backend silently fell back to built-in tools."""
        try:
            from tools.browser_use_cli import default_downgrade_notice

            notice = default_downgrade_notice()
            if notice:
                from gateway.warning_notifications import render_notification
                render_notification(lambda: self._console_print(f"[yellow]⚠ {notice}[/yellow]"), platform="cli")
        except Exception:
            logger.debug("browser backend notice failed", exc_info=True)

    def finalize_preloaded_skills(self) -> None:
        """Join the background --skills preload and fold it into the prompt (idempotent).

        Raises ``ValueError`` only when EVERY requested skill was unknown.
        """
        if getattr(self, "_preload_skills_finalized", False):
            return
        thread = getattr(self, "_preload_skills_thread", None)
        if thread is None:
            self._preload_skills_finalized = True
            return
        thread.join(timeout=120)
        self._preload_skills_finalized = True
        err = getattr(self, "_preload_skills_error", None)
        if err is not None:
            raise err
        auto_result = getattr(self, "_auto_load_skills_result", None)
        if auto_result and auto_result[2]:
            logger.warning("skills.auto_load: skill(s) not found or disabled, skipped: %s", ", ".join(auto_result[2]))
        # auto_load names first, then explicit -s names that were not already pinned.
        self.preloaded_skills = list(auto_result[1]) if auto_result else []
        result = getattr(self, "_preload_skills_result", None)
        if not result:
            return
        skills_prompt, loaded_skills, missing_skills = result
        if missing_skills:
            missing_display = ", ".join(missing_skills)
            # A typo'd name must not crash a kanban worker; only a fully-missing set fails loudly.
            if loaded_skills:
                logger.warning(
                    "Unknown skill(s) requested, skipping: %s. "
                    "Continuing with: %s. "
                    "List available skills with `hermes skills list`.",
                    missing_display,
                    ", ".join(loaded_skills),
                )
            else:
                raise ValueError(f"Unknown skill(s): {missing_display}")
        if skills_prompt:
            self.system_prompt = "\n\n".join(p for p in (self.system_prompt, skills_prompt) if p).strip()
        self.preloaded_skills += [name for name in loaded_skills if name not in self.preloaded_skills]

    def _show_tool_availability_warnings(self):
        """Warn about toolsets switched off at startup (missing API keys, unusable terminal backend)."""
        try:
            # Runs on a daemon thread on the snapshot fast path: keep the imports to modules the
            # registry walk already loaded plus the pure notices module (a heavy import here races
            # importlib's module locks against the main thread).
            from model_tools import check_tool_availability
            from hermes_cli.tool_availability_notices import (
                current_terminal_backend, filter_to_enabled_toolsets, tool_availability_warning_lines,
            )
            from tools.terminal_tool import terminal_backend_unavailable_reason
            from toolsets import resolve_toolset

            _, unavailable = check_tool_availability()
            # Only toolsets this CLI session actually has. The selection is usually a composite bundle
            # (``hermes-cli``), so expand it to tool names before matching — a raw name comparison
            # matched nothing on a default install and silently dropped the terminal notice.
            unavailable = filter_to_enabled_toolsets(unavailable, self.enabled_toolsets or [], resolve_toolset)
            lines = tool_availability_warning_lines(
                unavailable, terminal_reason=terminal_backend_unavailable_reason(),
                terminal_backend=current_terminal_backend())
            if lines:
                self._console_print()
                for line in lines:
                    self._console_print(line)
        except Exception:
            pass

    def show_config(self):
        """Display current configuration with kawaii ASCII art."""
        terminal_env = os.getenv("TERMINAL_ENV", "local")
        terminal_cwd = os.getenv("TERMINAL_CWD", os.getcwd())
        terminal_timeout = os.getenv("TERMINAL_TIMEOUT", "60")

        config_path = _hermes_home / 'config.yaml'
        if not config_path.exists():
            config_path = Path(__file__).parent / 'cli-config.yaml'
        config_status = "(loaded)" if config_path.exists() else "(not found)"

        # ``api_key`` may be a callable (Entra ID bearer provider): never invoke it. Prefer the
        # LIVE agent's key: the constructor seeds self.api_key from env before provider
        # resolution, so on non-OpenAI providers it can be another vendor's key.
        from agent.azure_identity_adapter import is_token_provider

        display_key = self.api_key
        if self.agent is not None and getattr(self.agent, "api_key", None):
            display_key = self.agent.api_key
        if is_token_provider(display_key):
            api_key_display = "Microsoft Entra ID"
        elif isinstance(display_key, str) and len(display_key) > 12:
            api_key_display = f"{display_key[:8]}...{display_key[-4:]}"
        else:
            api_key_display = "Not set!"

        title = "(^_^) Configuration"
        width = 50
        pad = width - len(title)
        ssh_target = (
            f"{os.getenv('TERMINAL_SSH_USER', 'not set')}@{os.getenv('TERMINAL_SSH_HOST', 'not set')}"
            f":{os.getenv('TERMINAL_SSH_PORT', '22')}"
        ) if terminal_env == "ssh" else None
        sections = (
            ("Model", (("Model:    ", self.model), ("Base URL: ", self.base_url), ("API Key:  ", api_key_display))),
            ("Terminal", (
                ("Environment: ", terminal_env),
                *((("SSH Target:  ", ssh_target),) if ssh_target else ()),
                ("Working Dir: ", terminal_cwd),
                ("Timeout:     ", f"{terminal_timeout}s"),
            )),
            ("Agent", (
                ("Max Turns: ", self.max_turns),
                ("Toolsets:  ", ", ".join(self.enabled_toolsets) if self.enabled_toolsets else "all"),
                ("Verbose:   ", self.verbose),
            )),
            ("Session", (
                ("Started:    ", self.session_start.strftime("%Y-%m-%d %H:%M:%S")),
                ("Config File:", f"{config_path} {config_status}"),
            )),
        )
        print()
        print("+" + "-" * width + "+")
        print("|" + " " * (pad // 2) + title + " " * (pad - pad // 2) + "|")
        print("+" + "-" * width + "+")
        for name, rows in sections:
            print()
            print(f"  -- {name} --")
            for label, value in rows:
                print(f"  {label} {value}")
        print()

    # canonical command -> (method name, pass cmd_original?). Absent commands resolve to
    # ``_handle_<name>_command(cmd)``. Looked up via getattr at dispatch time so
    # monkeypatching works. A handler returning False exits the REPL.
    _SLASH_DISPATCH: dict[str, tuple[str, bool]] = {
        "exit": ("_cmd_exit", True), "quit": ("_cmd_exit", True), "help": ("_cmd_help", True),
        "palette": ("_open_command_palette", False), "whoami": ("_handle_whoami_command", False),
        "profile": ("_handle_profile_command", False), "toolsets": ("show_toolsets", False),
        "config": ("show_config", False), "redraw": ("_cmd_redraw", True), "clear": ("_cmd_clear", True),
        "history": ("show_history", False), "title": ("_cmd_title", True), "new": ("_cmd_new", True),
        "model": ("_handle_model_switch", True), "codex-runtime": ("_handle_codex_runtime", True),
        "retry": ("_cmd_retry", True), "prompt": ("_handle_prompt_compose_command", True),
        "undo": ("_cmd_undo", True), "save": ("save_conversation", True), "skills": ("_cmd_skills", True),
        "platforms": ("_show_gateway_status", False), "status": ("_show_session_status", False),
        "context": ("_show_context_breakdown", True), "egress": ("_cmd_egress", True),
        "statusbar": ("_cmd_statusbar", True), "verbose": ("_toggle_verbose", False), "yolo": ("_toggle_yolo", False),
        "compress": ("_manual_compress", True), "subscription": ("_show_subscription", False),
        "topup": ("_show_billing", True), "insights": ("_show_insights", True), "update": ("_cmd_update", True),
        "version": ("_cmd_version", True), "paste": ("_handle_paste_command", False), "reload": ("_cmd_reload", True),
        "reload-mcp": ("_confirm_and_reload_mcp", True), "reload-skills": ("_cmd_reload_skills", True),
        "plugins": ("_cmd_plugins", True), "stop": ("_handle_stop_command", False),
        "agents": ("_handle_agents_command", False), "bg": ("_handle_background_command", True),
        "queue": ("_cmd_queue", True), "steer": ("_cmd_steer", True), "moa": ("_cmd_moa", True),
        "verify": ("_handle_verify_command", False),
    }

    @classmethod
    def _slash_handler(cls, canonical: str) -> tuple[str, bool] | None:
        """(method name, pass cmd_original?) for a registered command, else None."""
        entry = cls._SLASH_DISPATCH.get(canonical)
        if entry is None:
            name = f"_handle_{canonical.replace('-', '_')}_command"
            if callable(getattr(cls, name, None)):
                entry = (name, True)
        return entry

    def process_command(self, command: str) -> bool:
        """Dispatch a slash command; returns False to exit the REPL."""
        cmd_lower = command.lower().strip()  # lowercase only for matching; args keep their case
        cmd_original = command.strip()

        # Aliases resolve via the central registry (hermes_cli/commands.py).
        from hermes_cli.commands import resolve_command as _resolve_cmd
        _base_word = cmd_lower.split()[0].lstrip("/")
        _cmd_def = _resolve_cmd(_base_word)
        canonical = _cmd_def.name if _cmd_def else _base_word

        # Observer-only pre_command plugin hook (return values ignored; never raises).
        if _cmd_def is not None:
            from hermes_cli.plugins import fire_pre_command_hook
            fire_pre_command_hook(
                surface="cli", command=canonical, alias_used=_base_word, args_raw=_slash_args(cmd_original),
                session_key=getattr(self, "session_id", None), platform="cli",
            )

        # A bare `/resume` prompt is one-shot: any other command disarms it so a later
        # number isn't swallowed as a stale selection.
        # See #34584.
        if canonical not in {"resume", "sessions"}:
            # Armed when a bare `/resume` prints the recent-sessions list so the very next bare numeric
            # input (e.g. `3`) resolves to that session. Holds the exact list used for index resolution;
            # one-shot (cleared on the next submitted input, whether it's the selection or anything else).
            # See #34584.
            self._pending_resume_sessions = None

        entry = self._slash_handler(canonical)
        if entry is None:
            return self._process_unregistered_slash(cmd_original, cmd_lower)
        method_name, pass_arg = entry
        handler = getattr(self, method_name)
        result = handler(cmd_original) if pass_arg else handler()
        return result is not False

    def _process_unregistered_slash(self, cmd_original: str, cmd_lower: str) -> bool:
        """Slash input with no built-in handler; precedence: quick_commands -> plugins -> bundles -> skills -> prefix expansion."""
        base_cmd = cmd_lower.split()[0]
        bare = base_cmd.lstrip("/")
        skill_commands = _ensure_skill_commands()
        skill_bundles = get_skill_bundles()
        quick_commands = self.config.get("quick_commands", {})
        user_args = cmd_original[len(base_cmd):].strip()
        if bare in quick_commands:
            return self._run_quick_command(base_cmd, quick_commands[bare], user_args)
        if bare in _get_plugin_cmd_handler_names():
            self._run_plugin_slash_command(base_cmd, user_args)
        elif base_cmd in skill_bundles:
            self._run_skill_bundle_command(base_cmd, skill_bundles[base_cmd], user_args)
        elif base_cmd in skill_commands:
            self._run_skill_slash_command(base_cmd, skill_commands[base_cmd], user_args)
        else:
            return self._expand_slash_prefix(cmd_original, cmd_lower, skill_commands, skill_bundles)
        return True

    def _run_quick_command(self, base_cmd: str, qcmd: dict, user_args: str) -> bool:
        """User-defined quick command (config.yaml): ``exec`` runs a shell snippet, ``alias`` re-dispatches."""
        qtype = qcmd.get("type")
        if qtype == "alias":
            target = qcmd.get("target", "").strip()
            if target:
                target = target if target.startswith("/") else f"/{target}"
                return self.process_command(f"{target} {user_args}".strip())
            self._console_print(f"[bold red]Quick command '{base_cmd}' has no target defined[/]")
            return True
        if qtype != "exec":
            self._console_print(f"[bold red]Quick command '{base_cmd}' has unsupported type (supported: 'exec', 'alias')[/]")
            return True
        import subprocess
        exec_cmd = qcmd.get("command", "")
        if not exec_cmd:
            self._console_print(f"[bold red]Quick command '{base_cmd}' has no command defined[/]")
            return True
        try:
            # shell=True is intentional (user-authored config snippets, never LLM controlled);
            # the env is sanitized because this process holds every API key.
            from tools.environments.local import build_subprocess_env
            from hermes_cli._subprocess_compat import windows_hide_flags
            result = subprocess.run(
                exec_cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, env=build_subprocess_env(),
                creationflags=windows_hide_flags(),  # no console flash on Windows (#56747)
            )
            # See #56747.
            output = result.stdout.strip() or result.stderr.strip()
            if output:
                from agent.redact import redact_sensitive_text
                self._console_print(_rich_text_from_ansi(redact_sensitive_text(output)))
            else:
                self._console_print("[dim]Command returned no output[/]")
        except subprocess.TimeoutExpired:
            self._console_print("[bold red]Quick command timed out (30s)[/]")
        except Exception as e:
            self._console_print(f"[bold red]Quick command error: {e}[/]")
        return True

    def _run_plugin_slash_command(self, base_cmd: str, user_args: str) -> None:
        from hermes_cli.plugins import get_plugin_command_handler, resolve_plugin_command_result

        plugin_handler = get_plugin_command_handler(base_cmd.lstrip("/"))
        if not plugin_handler:
            return
        try:
            result = resolve_plugin_command_result(plugin_handler(user_args))
            if result:
                _cprint(str(result))
        except Exception as e:
            _cprint(f"\033[1;31mPlugin command error: {e}{_RST}")

    def _queue_skill_message(self, msg) -> None:
        if hasattr(self, '_pending_input'):
            self._pending_input.put(msg)

    def _run_skill_bundle_command(self, base_cmd: str, bundle_info: dict, user_instruction: str) -> None:
        """``/<bundle>`` loads several skills at once (bundles win over same-named skills)."""
        bundle_result = build_bundle_invocation_message(base_cmd, user_instruction, task_id=self.session_id)
        if not bundle_result:
            ChatConsole().print(f"[bold red]Failed to load bundle for {base_cmd}[/]")
            return
        msg, loaded_names, missing = bundle_result
        self._queue_loaded_skills(msg, f"Loading bundle: {bundle_info['name']} ({len(loaded_names)} skills)", missing)

    def _queue_loaded_skills(self, msg, label: str, missing) -> None:
        print(f"\n⚡ {label}")
        if missing:
            ChatConsole().print(f"[yellow]Skipped missing skills: {', '.join(missing)}[/]")
        self._queue_skill_message(msg)

    def _run_skill_slash_command(self, base_cmd: str, skill_info: dict, rest: str) -> None:
        """``/<skill> ...``; stacked ``/skill-a /skill-b do XYZ`` loads every leading skill (up to 5)."""
        from agent.skill_commands import build_stacked_skill_invocation_message, split_stacked_skill_commands

        extra_keys, user_instruction = split_stacked_skill_commands(rest)
        if extra_keys:
            stacked_result = build_stacked_skill_invocation_message(
                [base_cmd, *extra_keys], user_instruction, task_id=self.session_id,
            )
            if not stacked_result:
                ChatConsole().print(f"[bold red]Failed to load stacked skills for {base_cmd}[/]")
                return
            msg, loaded_names, missing = stacked_result
            self._queue_loaded_skills(
                msg, f"Loading {len(loaded_names)} stacked skills: {', '.join(loaded_names)}", missing
            )
            return
        msg = build_skill_invocation_message(base_cmd, rest, task_id=self.session_id)
        if msg:
            self._queue_loaded_skills(msg, f"Loading skill: {skill_info['name']}", None)
        else:
            ChatConsole().print(f"[bold red]Failed to load skill for {base_cmd}[/]")

    def _expand_slash_prefix(self, cmd_original: str, cmd_lower: str, skill_commands, skill_bundles) -> bool:
        """Unique-prefix expansion against built-in COMMANDS + skill commands/bundles (agrees with tab-completion)."""
        from hermes_cli.commands import COMMANDS
        typed_base = cmd_lower.split()[0]
        all_known = set(COMMANDS) | set(skill_commands) | set(skill_bundles)
        matches = [c for c in all_known if c.startswith(typed_base)]
        if len(matches) > 1:
            if typed_base in matches:
                matches = [typed_base]
            else:
                # Unique shortest match wins: /qui -> /quit (5) over /quint-pipeline (15)
                min_len = min(len(c) for c in matches)
                shortest = [c for c in matches if len(c) == min_len]
                if len(shortest) == 1:
                    matches = shortest
        if len(matches) == 1 and matches[0] != typed_base:
            # Expand to the full name, preserving arguments.
            return self.process_command(matches[0] + cmd_original.strip()[len(typed_base):])
        if len(matches) > 1:
            _cprint(f"{_ACCENT}Ambiguous command: {cmd_lower}{_RST}")
            _cprint(f"{_DIM}Did you mean: {', '.join(sorted(matches))}?{_RST}")
        else:
            # Exact token with no handler (never re-dispatch the same token: recursion), or no match.
            from hermes_cli.cli_unknown_command import unknown_command_lines
            lead, pointer = unknown_command_lines(cmd_lower, all_known)
            _cprint(f"\033[1;31m{lead}{_RST}")
            _cprint(f"{_DIM}{_ACCENT}{pointer}{_RST}")
        return True

    def _drain_interrupt_queue_to_pending_input(self) -> None:
        """Move stray ``_interrupt_queue`` messages into ``_pending_input`` after every turn.

        Busy-time input lands in ``_interrupt_queue`` and is only drained by the explicit
        interrupt path; a turn that finishes naturally would otherwise strand it and the
        CLI appears to hang. Never raises.

        Called once at the end of every turn from ``process_loop``'s ``finally`` block. Catches and swallows
        ``Exception`` because the drain must never break the main loop. (#20271)
        """
        try:
            while not self._interrupt_queue.empty():
                stray = self._interrupt_queue.get_nowait()
                if stray:
                    self._pending_input.put(stray)
        except Exception:
            pass

    def _on_reasoning(self, reasoning_text: str):
        """Callback for intermediate reasoning display during tool-call loops."""
        if not reasoning_text:
            return
        self._reasoning_preview_buf = getattr(self, "_reasoning_preview_buf", "") + reasoning_text
        self._flush_reasoning_preview(force=False)

    # Inline tokens that bypass the destructive-slash confirmation modal (scripting, or
    # when the modal can't be marshaled onto the app loop).
    # A general escape hatch for non-interactive use (scripting/automation) and for the degraded path where
    # the modal can't be marshaled onto the app loop — lets users self-serve without flipping
    # approvals.destructive_slash_confirm in config. (Native Windows now drives the modal normally — see
    # #33961.)
    _DESTRUCTIVE_SKIP_TOKENS = frozenset({"now", "--yes", "-y"})


    def run(self):
        """Run the interactive CLI loop with persistent input at bottom."""
        if not self._claim_active_session("cli"):
            return

        self._tui_print_startup()
        self._tui_init_run_state()
        kb = self._tui_build_key_bindings()
        layout, style = self._tui_build_layout(kb)

        app = self._tui_build_application(layout, kb, style)
        _disable_prompt_toolkit_cpr_warning(app)
        app.after_render += self._pet_flush_kitty_frame
        self._app = app

        # Ghost status-bar lines on resize: pt's renderer scrolls the terminal after each
        # paint, pushing chrome into scrollback where a column-shrink reflows it into
        # duplicates. Wrapping _output_screen_diff keeps its reserve-space branch from firing.
        try:
            # Background: prompt_toolkit's renderer (renderer.py L232-242) explicitly moves the cursor to
            # the bottom of the canvas after painting "to make sure the terminal scrolls up, even when the
            # lower lines of the canvas just contain whitespace". In non-fullscreen mode this scrolls chrome
            # content (status bar, input rules) into terminal scrollback on every render. When the terminal
            # column-shrinks, the emulator reflows the previously rendered full-width rows into multiple
            # narrower rows that get pushed up — leaving ghost duplicates AND polluting scrollback. Same
            # issue as pt #29 (open since 2014), #1675, #1933. Surgical fix: wrap _output_screen_diff so
            # that when its internal `if current_height > previous_screen.height` branch fires (the one that
            # does the bottom-cursor-move), we make it fall through by inflating previous_screen.height
            # first.
            import prompt_toolkit.renderer as _pt_renderer
            from prompt_toolkit.renderer import _output_screen_diff as _orig_osd

            if not getattr(_pt_renderer, "_hermes_osd_patched", False):
                _pt_renderer._output_screen_diff = functools.partial(
                    _hermes_call_output_screen_diff, _orig_osd
                )
                _pt_renderer._hermes_osd_patched = True
        except Exception:
            pass

        _apply_bracketed_paste_timeout_patch()

        self._install_resize_recovery(app)

        threading.Thread(target=self._tui_spinner_loop, daemon=True).start()
        threading.Thread(target=self._tui_process_loop, daemon=True).start()
        # Wake word listener off-thread so a first-run engine install never blocks the prompt.
        threading.Thread(target=self._tui_wake_startup, daemon=True, name="wake-startup").start()

        atexit.register(_run_cleanup)
        self._tui_install_signal_handlers()

        if not self._tui_stdin_usable():
            _run_cleanup()
            self._print_exit_summary()
            return

        try:
            with patch_stdout():
                try:
                    # run_in_terminal() may return either: • a coroutine / Future (prompt_toolkit ≥ 3.0) —
                    # must be scheduled via ensure_future so the coroutine is actually awaited; calling it
                    # bare would leave it unawaited and silently drop the output (fixes #23185 Bug A). •
                    # None (some mocks / older PT builds) — just call the inner function directly since PT
                    # already executed it synchronously. Do NOT fall back to a bare _pt_print when
                    # ensure_future raises, because run_in_terminal already invoked the lambda in that case
                    # (the mock path), which would double-print the line.
                    import asyncio as _aio
                    _aio.get_running_loop().set_exception_handler(self._tui_suppress_closed_loop_errors)
                except Exception:
                    pass  # no running loop -- nothing to patch
                # Record that the app enables focus reporting + mouse tracking so _run_cleanup
                # resets them; extended key modes are popped by the same reset.
                # When multiline shortcuts are on, also ask supported terminals (e.g. iTerm2) to report
                # modified keys distinctly (kitty protocol + modifyOtherKeys); the cleanup reset pops both
                # modes. See #36823.
                _mark_tui_input_modes_active()
                if self._tui_multiline_shortcuts:
                    _enable_extended_enter_keys(app.output)
                self._pet_start_anim()
                app.run()
        except (EOFError, KeyboardInterrupt, BrokenPipeError):
            pass
        except (KeyError, OSError) as _stdin_err:
            # Selector registration failures from broken stdin and I/O errors from a
            # broken stdout during interrupt (EIO is suppressed).
            _errno = getattr(_stdin_err, "errno", None) if isinstance(_stdin_err, OSError) else None
            _msg = str(_stdin_err)
            if _errno == errno.EIO:
                pass
            elif _errno in {errno.EINVAL, errno.EBADF} or any(
                s in _msg for s in ("is not registered", "Bad file descriptor", "Invalid argument")
            ):
                print(
                    f"\nError: stdin is not usable ({_stdin_err}).\n"
                    "This can happen with certain Python installations (e.g. uv-managed cPython on macOS)\n"
                    "where kqueue cannot register fd 0.\n"
                    "Try reinstalling Python via pyenv or Homebrew, then re-run: hermes setup"
                )
            else:
                raise
        finally:
            # A resize right before exit leaves its recovery (and the paints it held) unrun.
            _release_paints()
            self._tui_shutdown()

        # /update relaunch happens here, after prompt_toolkit restored terminal modes, on the
        # main thread (the process_loop thread would skip cleanup / only exit itself on Windows).
        if self._pending_relaunch:
            from hermes_cli.relaunch import relaunch
            relaunch(self._pending_relaunch, preserve_inherited=False)


def _build_cli_from_args(model, toolsets, provider, reasoning, api_key, base_url, max_turns, run_budget, verbose, compact, resume, checkpoints, pass_session_id, ignore_rules, skills):
    """Resolve the toolset list (explicit / coding posture / platform default), construct HermesCLI, and start the background skills preload."""
    toolsets_list = None
    if isinstance(toolsets, str) and toolsets:
        toolsets_list = [t.strip() for t in toolsets.split(",")]
    elif isinstance(toolsets, (list, tuple)) and toolsets:
        # Fire may pass multiple --toolsets as a tuple
        toolsets_list = []
        for t in toolsets:
            toolsets_list.extend([x.strip() for x in t.split(",")] if isinstance(t, str) else [str(t)])
    elif not toolsets:
        # Coding posture inside a code workspace, else the shared platform resolver.
        try:
            from agent.coding_context import coding_selection
            toolsets_list = coding_selection(platform="cli", config=CLI_CONFIG)
        except Exception:
            toolsets_list = None
        if toolsets_list is None:
            from hermes_cli.tools_config import _get_platform_tools
            toolsets_list = sorted(_get_platform_tools(CLI_CONFIG, "cli"))

    parsed_skills = _parse_skills_argument(skills)

    try:
        cli = HermesCLI(
            model=model,
            toolsets=toolsets_list,
            provider=provider,
            reasoning=reasoning,
            api_key=api_key,
            base_url=base_url,
            max_turns=max_turns,
            run_budget=run_budget,
            verbose=verbose,
            compact=compact,
            resume=resume,
            checkpoints=checkpoints,
            pass_session_id=pass_session_id,
            ignore_rules=ignore_rules,
        )
    except ImportError as e:
        # Direct `python cli.py` bypasses cmd_chat's partial-update ImportError handler.
        from hermes_constants import emit_partial_update_hint

        if emit_partial_update_hint(e):
            sys.exit(1)
        raise

    # skills.auto_load rides the same background preload as -s; --ignore-rules skips it with
    # the rest of the auto-injected context. Resolved here (not lazily in the agent) so the
    # session id is real for ${HERMES_SESSION_ID} and -s can dedupe against it.
    from agent.skill_commands import build_auto_load_prompt, resolve_auto_load_skills
    auto_load_names = [] if getattr(cli, "ignore_rules", ignore_rules) else resolve_auto_load_skills(CLI_CONFIG)
    if not auto_load_names:
        cli._auto_load_skills_result = ("", [], [])
    if parsed_skills or auto_load_names:
        # Load the skill payloads in the background: skill_view walks the full skills
        # tree per skill (~0.5s for a large library) and the result is only consumed
        # at agent init, not by the banner. finalize_preloaded_skills() joins the
        # thread before any consumer reads cli.system_prompt.
        def _load_preloaded_skills() -> None:
            try:
                if auto_load_names:
                    cli._auto_load_skills_result = build_auto_load_prompt(task_id=cli.session_id, user_config=CLI_CONFIG)
                if parsed_skills:
                    cli._preload_skills_result = build_preloaded_skills_prompt(
                        parsed_skills, task_id=cli.session_id, excluded_loaded_names=set(cli._auto_load_skills_result[1]))
            except Exception as exc:  # surfaced by finalize
                cli._preload_skills_error = exc

        cli._preload_skills_requested = [*auto_load_names, *(s for s in parsed_skills if s not in auto_load_names)]
        cli._preload_skills_thread = threading.Thread(target=_load_preloaded_skills, name="skills-preload", daemon=True)
        cli._preload_skills_thread.start()
    return cli


def _run_legacy_gateway():
    """Legacy `cli.py --gateway` entry: arm the startup watchdog (before importing the gateway graph), then run it."""
    import asyncio
    with suppress(Exception):
        from hermes_startup_watchdog import arm_startup_watchdog
        arm_startup_watchdog()
    from gateway.run import start_gateway
    print("Starting Hermes Gateway (messaging platforms)...")
    asyncio.run(start_gateway())


def _start_worktree_setup(list_tools, list_toolsets, worktree, w):
    """Start isolated-worktree creation (+ tool prewarm) in the background.

    Returns a join callable that publishes ``_active_worktree``/TERMINAL_CWD and
    schedules stale-worktree GC, or None when no worktree is wanted.
    """
    if list_tools or list_toolsets or not (worktree or w or CLI_CONFIG.get("worktree", False)):
        return None
    # Overlap tool discovery with the I/O-bound worktree setup so show_banner() hits a warm
    # cache (~0.4s). Only on the -w path: plain `hermes` has no I/O wait to hide.
    def _prewarm_tools() -> None:
        try:
            import model_tools as _mt
            _mt.get_tool_definitions(quiet_mode=True)
        except Exception:
            logger.debug("tool prewarm failed", exc_info=True)

    threading.Thread(target=_prewarm_tools, name="tool-prewarm", daemon=True).start()
    _sync_base = CLI_CONFIG.get("worktree_sync", True)
    _wt_result: dict = {}

    def _create_worktree() -> None:
        try:
            _wt_result["info"] = _setup_worktree(sync_base=_sync_base)
        except Exception:
            logger.debug("worktree setup failed", exc_info=True)
            _wt_result["info"] = None

    _wt_thread = threading.Thread(target=_create_worktree, name="worktree-setup", daemon=True)
    _wt_thread.start()

    def _worktree_maintenance(repo: str) -> None:
        _prune_stale_worktrees(repo)
        _maintain_pack_health(repo)

    def _join_worktree() -> Optional[Dict[str, str]]:
        _wt_thread.join(timeout=120)
        info = _wt_result.get("info")
        if not info:
            return info
        global _active_worktree
        _active_worktree = info
        os.environ["TERMINAL_CWD"] = info["path"]
        atexit.register(_cleanup_worktree, info)
        # GC stale worktrees AFTER _setup_worktree so they never race on git's worktree
        # metadata (the new tree is immune: <24h age gate + live pid lock); then repack
        # once refs are final so lookups stay fast on multi-agent boxes.
        _repo = _git_repo_root()
        if _repo:
            threading.Thread(target=_worktree_maintenance, args=(_repo,), name="worktree-prune", daemon=True).start()
        return info

    return _join_worktree


def main(
    query: str = None,
    q: str = None,
    oneshot: bool = False,
    image: str = None,
    toolsets: str = None,
    skills: str | list[str] | tuple[str, ...] = None,
    model: str = None,
    provider: str = None,
    reasoning: str = None,
    api_key: str = None,
    base_url: str = None,
    max_turns: int = None,
    run_budget: float = None,
    verbose: Optional[bool] = None,
    quiet: bool = False,
    compact: bool = False,
    list_tools: bool = False,
    list_toolsets: bool = False,
    gateway: bool = False,
    resume: str = None,
    worktree: bool = False,
    w: bool = False,
    checkpoints: bool = False,
    pass_session_id: bool = False,
    output_format: str = "text",
    ignore_user_config: bool = False,
    ignore_rules: bool = False,
):
    """
    Hermes Agent CLI - Interactive AI Assistant
    
    Args:
        query: Query to run. On a real TTY this seeds an interactive session
            (submitted literally as the first turn); with --oneshot/-Q or a
            non-TTY it answers and exits. Alias: -q
        q: Shorthand for --query
        oneshot: With -q: force the legacy answer-and-exit single-query mode
            even on a TTY.
        image: Optional local image path to attach to a single query
        toolsets: Comma-separated list of toolsets to enable (e.g., "web,terminal")
        skills: Comma-separated or repeated list of skills to preload for the session
        model: Model to use (default: anthropic/claude-opus-4-20250514)
        provider: Inference provider ("auto", "openrouter", "nous", "openai-codex", "zai", "kimi-coding", "minimax", "minimax-cn")
        reasoning: Reasoning effort for this run (none|minimal|low|medium|high|xhigh|max|ultra). Overrides agent.reasoning_effort.
        api_key: API key for authentication
        base_url: Base URL for the API
        max_turns: Maximum tool-calling iterations (default: 60)
        verbose: Enable verbose logging
        compact: Use compact display mode
        list_tools: List available tools and exit
        list_toolsets: List available toolsets and exit
        resume: Resume a previous session by its ID (e.g., 20260225_143052_a1b2c3)
        worktree: Run in an isolated git worktree (for parallel agents). Alias: -w
        w: Shorthand for --worktree
    
    Examples:
        python cli.py                            # Start interactive mode
        python cli.py --toolsets web,terminal    # Use specific toolsets
        python cli.py --skills hermes-agent-dev,github-auth
        python cli.py -q "What is Python?"       # Single query mode
        python cli.py -q "Describe this" --image ~/storage/shared/Pictures/cat.png
        python cli.py --list-tools               # List tools and exit
        python cli.py --resume 20260225_143052_a1b2c3  # Resume session
        python cli.py -w                         # Start in isolated git worktree
        python cli.py -w -q "Fix issue #123"     # Single query in worktree
    """
    # UTF-8 stdio on Windows before any print (Rich box-drawing would UnicodeEncodeError on cp1252).
    with suppress(Exception):
        from hermes_cli.stdio import configure_windows_stdio
        configure_windows_stdio()

    os.environ["HERMES_INTERACTIVE"] = "1"  # terminal_tool: interactive sudo prompts with timeout
    # The banner names affected plugins; the raw per-name compat warnings would only duplicate it on stderr.
    with suppress(Exception):
        from hermes_cli.plugin_compat import quiet_for_interactive
        quiet_for_interactive()

    if gateway:
        _run_legacy_gateway()
        return

    _join_worktree = _start_worktree_setup(list_tools, list_toolsets, worktree, w)
    query = query or q
    # ``hermes chat`` already validated this; the direct Fire entry point gets the same contract.
    if output_format == "stream-json":
        if not query:
            raise ValueError("--format stream-json requires -q/--query")
        quiet = True
    cli = _build_cli_from_args(model, toolsets, provider, reasoning, api_key, base_url, max_turns, run_budget,
                               verbose, compact, resume, checkpoints, pass_session_id, ignore_rules, skills)

    # Join the background worktree creation before anything consumes TERMINAL_CWD.
    # A requested worktree whose setup failed aborts: never silently run without isolation.
    wt_info = _join_worktree() if _join_worktree is not None else None
    if _join_worktree is not None and not wt_info:
        return

    # Inject worktree context into agent's system prompt
    if wt_info:
        wt_note = (
            f"\n\n[System note: You are working in an isolated git worktree at "
            f"{wt_info['path']}. Your branch is `{wt_info['branch']}`. "
            f"Changes here do not affect the main working tree or other agents. "
            f"Remember to commit and push your changes, and create a PR if appropriate. "
            f"The original repo is at {wt_info['repo_root']}.]"
        )
        cli.system_prompt = (cli.system_prompt or "") + wt_note

    if list_tools or list_toolsets:
        cli.show_banner()
        (cli.show_tools if list_tools else cli.show_toolsets)()
        sys.exit(0)

    atexit.register(_run_cleanup)  # interactive mode registers again in run() (idempotent)
    _install_single_query_signal_handlers(cli)

    if query or image:
        _run_single_query_mode(cli, query, image, quiet, oneshot, stream_json=output_format == "stream-json")
        return
    cli.run()


if __name__ == "__main__":
    import fire

    fire.Fire(main)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from prompt_toolkit.layout.menus import CompletionsMenu  # noqa: F401,E402
from prompt_toolkit.filters import Condition  # noqa: F401,E402
from prompt_toolkit.layout import ConditionalContainer  # noqa: F401,E402
from prompt_toolkit.layout.processors import ConditionalProcessor  # noqa: F401,E402
from prompt_toolkit.layout.dimension import Dimension  # noqa: F401,E402
from prompt_toolkit.history import FileHistory  # noqa: F401,E402
from prompt_toolkit.layout import FormattedTextControl  # noqa: F401,E402
from prompt_toolkit.layout import HSplit  # noqa: F401,E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: F401,E402
from prompt_toolkit.layout import Layout  # noqa: F401,E402
from prompt_toolkit.styles import Style as PTStyle  # noqa: F401,E402
from rich.panel import Panel  # noqa: F401,E402
from prompt_toolkit.layout.processors import PasswordProcessor  # noqa: F401,E402
from prompt_toolkit.layout.processors import Processor  # noqa: F401,E402
from prompt_toolkit.widgets import TextArea  # noqa: F401,E402
from prompt_toolkit.layout.processors import Transformation  # noqa: F401,E402
from prompt_toolkit.layout import Window  # noqa: F401,E402
from prompt_toolkit.layout import WindowAlign  # noqa: F401,E402
import base64  # noqa: F401,E402
import concurrent.futures  # noqa: F401,E402
import copy  # noqa: F401,E402
from rich import box as rich_box  # noqa: F401,E402
import tempfile  # noqa: F401,E402

def AIAgent(*args, **kwargs):
    from run_agent import AIAgent as _AIAgent

    return _AIAgent(*args, **kwargs)

def CanonicalUsage(*args, **kwargs):
    from agent.usage_pricing import CanonicalUsage as _CanonicalUsage

    return _CanonicalUsage(*args, **kwargs)


_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_BROWSER_CDP_URL': ('hermes_cli.browser_connect', 'DEFAULT_BROWSER_CDP_URL'),
    'HERMES_AGENT_LOGO': ('hermes_cli.banner', 'HERMES_AGENT_LOGO'),
    'HERMES_CADUCEUS': ('hermes_cli.banner', 'HERMES_CADUCEUS'),
    'SlashCommandAutoSuggest': ('hermes_cli.commands_completion', 'SlashCommandAutoSuggest'),
    'SlashCommandCompleter': ('hermes_cli.commands_completion', 'SlashCommandCompleter'),
    'build_welcome_banner': ('hermes_cli.banner', 'build_welcome_banner'),
    'display_hermes_home': ('hermes_constants', 'display_hermes_home'),
    'estimate_usage_cost': ('agent.usage_pricing', 'estimate_usage_cost'),
    'get_all_toolsets': ('toolsets', 'get_all_toolsets'),
    'get_job': ('cron.jobs', 'get_job'),
    'get_toolset_for_tool': ('model_tools', 'get_toolset_for_tool'),
    'get_toolset_info': ('toolsets', 'get_toolset_info'),
    'init_skin_from_config': ('hermes_cli.skin_engine', 'init_skin_from_config'),
    'is_browser_debug_ready': ('hermes_cli.browser_connect', 'is_browser_debug_ready'),
    'is_table_divider': ('agent.markdown_tables', 'is_table_divider'),
    'looks_like_table_row': ('agent.markdown_tables', 'looks_like_table_row'),
    'manual_chrome_debug_command': ('hermes_cli.browser_connect', 'manual_chrome_debug_command'),
    'print_config_warnings': ('hermes_cli.config', 'print_config_warnings'),
    'prompt_for_secret': ('hermes_cli.callbacks', 'prompt_for_secret'),
    'set_friendly_tool_labels': ('agent.display', 'set_friendly_tool_labels'),
    'set_tool_preview_max_len': ('agent.display', 'set_tool_preview_max_len'),
    'setup_logging': ('hermes_logging', 'setup_logging'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
