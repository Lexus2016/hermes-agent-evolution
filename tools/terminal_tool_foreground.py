"""Foreground execution, backend compatibility and retry handling for terminal_tool.

Runtime facade lookups preserve the terminal tool's existing patch points.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from tools.terminal_tool import _ExecPlan

logger = logging.getLogger("tools.terminal_tool")


def _compatible_yield_kwargs(command: str, env: Any, **ctx) -> dict:
    """Only forward the yield handler when the backend's execute signature accepts it."""
    from tools import terminal_tool as tt

    kwargs = tt._yield_kwargs(command, **ctx)
    if kwargs and "yield_handler" in kwargs:
        import inspect
        try:
            signature = inspect.signature(env.execute)
            if "yield_handler" not in signature.parameters and not any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in signature.parameters.values()
            ):
                kwargs = {}
        except Exception:
            logger.debug("Could not inspect terminal backend execute signature", exc_info=True)
    return kwargs


def _run_foreground(
    command: str, env: Any, plan: _ExecPlan, *,
    task_id: Optional[str], session_id: Optional[str], session_key: str,
    workdir: Optional[str], approval_note: Optional[str], clear_interrupt: bool,
    metered: bool = True,
    _observe_backend_outcome: Callable[[str | None], None],
) -> str:
    """Execute in the foreground with retry on transient errors, then finalize. ``metered``
    is False for Hermes' own control-plane commands (``_host_local``)."""
    from hermes_cli.observability.shared_metrics_harness import record_terminal_outcome
    from hermes_cli.observability.shared_metrics_loop import terminal_backend_error_class
    from tools import terminal_tool as tt
    max_retries = 3
    env_type, eff, effective_timeout = plan.env_type, plan.effective_task_id, plan.effective_timeout

    if clear_interrupt:
        from tools.interrupt import clear_current_thread_interrupt
        clear_current_thread_interrupt()

    streak = tt._increment_terminal_streak(task_id)
    retry_count = 0
    while True:
        try:
            command_cwd = tt._resolve_command_cwd(
                workdir=workdir, default_cwd=plan.cwd, session_key=session_key, env_type=env_type,
                mounted_host=getattr(env, "host_cwd", None) or plan.host_cwd,
                env=env,
            )
            kwargs = _compatible_yield_kwargs(
                command, env, env_type=env_type, cwd=command_cwd, effective_task_id=eff,
                task_id=task_id, session_key=session_key,
            )
            result = env.execute(
                command, timeout=effective_timeout, cwd=command_cwd, bounded_capture=True,
                **kwargs,
            )
        except Exception as e:
            _observe_backend_outcome("timeout" if isinstance(e, TimeoutError) else "exception")
            classification = tt.classify_terminal_failure(
                command,
                exit_code=-1,
                stdout="",
                stderr=str(e),
                consecutive_count=streak,
            )

            if classification.category in {
                tt.FailureCategory.timeout,
                tt.FailureCategory.timeout_deterministic,
            } or "timeout" in str(e).lower() or "timed out" in str(e).lower():
                result_dict = {
                    "output": "",
                    "exit_code": 124,
                    "error": (
                        f"Command timed out after {effective_timeout} seconds. "
                        "The process may still be running or have produced "
                        "partial output. This command looks long-running — "
                        "re-run with background=true and notify_on_complete=true."
                    ),
                    "failure_class": classification.category.value if classification.category != tt.FailureCategory.unknown else "timeout",
                    "suggestion": classification.hint,
                    "should_retry": False,
                    "terminal_streak": streak,
                }
                repeat = tt._note_terminal_failure(task_id, command, 124, "")
                budget = tt._get_max_command_repeats()
                if repeat > budget:
                    diagnostic = tt.spiral_break_diagnostic(command, repeat, budget)
                    result_dict["error"] = diagnostic
                    result_dict["suggestion"] = diagnostic
                    result_dict["failure_class"] = "retry_spiral"
                    result_dict["failure_repeat_count"] = repeat
                rec = tt.streak_recommendation(streak)
                if rec:
                    result_dict["recommendation"] = rec
                return tt.json.dumps(result_dict, ensure_ascii=False)

            if retry_count < max_retries:
                retry_count += 1
                wait_time = 2 ** retry_count
                tt.logger.warning("Execution error, retrying in %ds (attempt %d/%d) - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                               wait_time, retry_count, max_retries, tt._safe_command_preview(command), type(e).__name__, e, eff, env_type)
                tt.time.sleep(wait_time)
                continue

            tt.logger.error("Execution failed after %d retries - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                         max_retries, tt._safe_command_preview(command), type(e).__name__, e, eff, env_type)
            result_dict = {
                "output": "",
                "exit_code": -1,
                "error": tt._redact_terminal_error_text(
                    f"Command execution failed: {type(e).__name__}: {e}"
                ),
                "failure_class": classification.category.value,
                "suggestion": classification.hint,
                "should_retry": False,
                "terminal_streak": streak,
            }
            rec = tt.streak_recommendation(streak)
            if rec:
                result_dict["recommendation"] = rec
            return tt.json.dumps(result_dict, ensure_ascii=False)

        _observe_backend_outcome(terminal_backend_error_class(result))
        if result.get("yielded_session_id"):
            return tt.json.dumps({
                "output": result.get("output", ""), "exit_code": None, "error": None,
                "status": "yielded_to_background", "session_id": result["yielded_session_id"],
                "pid": result.get("pid"), "notify_on_complete": True, "note": tt._YIELDED_NOTE,
            }, ensure_ascii=False)

        if metered:
            record_terminal_outcome(command, env_type, result)
        output = result.get("output", "")
        returncode = result.get("returncode", 0)

        if returncode != 0:
            classification = tt.classify_terminal_failure(
                command,
                exit_code=returncode,
                stdout=output,
                stderr="",
                consecutive_count=streak,
            )
            from tools.terminal_tool_result import _interpret_exit_code
            exit_note = _interpret_exit_code(command, returncode)
            if classification.category == tt.FailureCategory.unknown and exit_note:
                classification = tt.TerminalFailureClassification(
                    category=tt.FailureCategory.unknown,
                    hint=exit_note,
                    should_retry=False,
                )

            if classification.should_retry and retry_count < max_retries:
                retry_count += 1
                wait_time = 2 ** retry_count
                tt.logger.warning(
                    "Non-zero exit %d, retrying in %ds (attempt %d/%d) - Command: %s - Task: %s, Backend: %s",
                    returncode,
                    wait_time,
                    retry_count,
                    max_retries,
                    tt._safe_command_preview(command),
                    eff,
                    env_type,
                )
                tt.time.sleep(wait_time)
                continue

            if classification.category not in {
                tt.FailureCategory.unknown,
            }:
                result_dict = {
                    "output": output,
                    "exit_code": returncode,
                    "error": classification.hint,
                    "failure_class": classification.category.value,
                    "suggestion": classification.hint,
                    "should_retry": False,
                    "terminal_streak": streak,
                }
                repeat = tt._note_terminal_failure(
                    task_id, command, returncode, output
                )
                budget = tt._get_max_command_repeats()
                if repeat > budget:
                    diagnostic = tt.spiral_break_diagnostic(
                        command, repeat, budget
                    )
                    result_dict["error"] = diagnostic
                    result_dict["suggestion"] = diagnostic
                    result_dict["failure_class"] = "retry_spiral"
                    result_dict["failure_repeat_count"] = repeat
                if approval_note:
                    if returncode == 130 and "[Command interrupted]" in output:
                        result_dict["approval"] = (
                            approval_note.rstrip(".") + ", then interrupted."
                        )
                    else:
                        result_dict["approval"] = approval_note
                if exit_note:
                    result_dict["exit_code_meaning"] = exit_note
                rec = tt.streak_recommendation(streak)
                if rec:
                    result_dict["recommendation"] = rec
                tt._apply_classified_enrichments(result_dict, result, command, output)
                return tt.json.dumps(result_dict, ensure_ascii=False)
            else:
                tt._note_terminal_failure(task_id, command, returncode, output)
        else:
            tt._reset_terminal_streak(task_id)
            tt._reset_terminal_failure_repeats(task_id)

        return tt.finalize_foreground_result(
            command=command, result=result, env=env, env_type=env_type, effective_task_id=eff,
            task_id=task_id, session_id=session_id, session_key=session_key, workdir=workdir,
            command_cwd=command_cwd, approval_note=approval_note,
        )
