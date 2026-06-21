"""
Cron job management tools for Hermes Agent.

Expose a single compressed action-oriented tool to avoid schema/context bloat.
Compatibility wrappers remain for direct Python callers and legacy tests.
"""

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from hermes_constants import display_hermes_home

logger = logging.getLogger(__name__)

# Import from cron module (will be available when properly installed)
sys.path.insert(0, str(Path(__file__).parent.parent))

from cron.jobs import (
    AmbiguousJobReference,
    claim_job_for_fire,
    create_job,
    get_job,
    list_jobs,
    mark_job_run,
    parse_schedule,
    pause_job,
    remove_job,
    resolve_job_ref,
    resume_job,
    update_job,
)


def _notify_provider_jobs_changed_safe() -> None:
    """Tell the active cron scheduler provider the job set changed (no-op for
    the built-in). Best-effort — never lets a provider error break the tool."""
    try:
        from cron.scheduler import _notify_provider_jobs_changed
        _notify_provider_jobs_changed()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cron availability preflight + per-task retry rate limiter
# ---------------------------------------------------------------------------
#
# Issue #214: cronjob tool failures recur without surfacing underlying cause.
# We add:
#   1. A preflight probe that checks whether cron is usable (crontab binary
#      and, on Linux, a running cron daemon) before mutating state.
#   2. A per-task retry limiter so repeated failures back off with a clear
#      "too many attempts" message instead of looping noisily.
#   3. Richer error payloads that include the underlying CLI output.

_MAX_CONSECUTIVE_CRON_FAILURES = 3
_CRON_RETRY_WINDOW_SECONDS = 300.0  # failures older than this are forgotten
_cron_failure_tracker: Dict[str, List[float]] = {}
_cron_failure_tracker_lock = threading.Lock()


def _record_cron_failure(task_id: Optional[str]) -> int:
    """Record a cron failure for *task_id* and return the current streak."""
    if not task_id:
        return 1
    now = time.monotonic()
    with _cron_failure_tracker_lock:
        window = [
            ts
            for ts in _cron_failure_tracker.get(task_id, [])
            if now - ts < _CRON_RETRY_WINDOW_SECONDS
        ]
        window.append(now)
        _cron_failure_tracker[task_id] = window
        return len(window)


def _reset_cron_failure(task_id: Optional[str]) -> None:
    """Reset the failure streak for *task_id* after a successful operation."""
    if not task_id:
        return
    with _cron_failure_tracker_lock:
        _cron_failure_tracker.pop(task_id, None)


def _cron_preflight_check() -> Optional[str]:
    """Probe basic cron availability and return a user-facing error string.

    Returns ``None`` when cron appears usable, otherwise a short diagnostic
    message explaining what is missing.  The check is best-effort: on systems
    where ``crontab`` is intentionally absent or where Hermes is using the
    internal JSON scheduler only, we still verify that the cron directory is
    writable so job persistence does not fail silently.
    """
    from cron.jobs import CRON_DIR, ensure_dirs

    try:
        ensure_dirs()
        probe = CRON_DIR / ".preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        return (
            f"Cron directory is not writable ({CRON_DIR}): {exc}. "
            "Check permissions for the user running Hermes."
        )

    crontab = shutil.which("crontab")
    if crontab is None:
        return (
            "The 'crontab' command is not available on PATH. "
            "Install a cron implementation (e.g. cronie, vixie-cron) "
            "or verify that Hermes is running in an environment with cron support."
        )

    try:
        result = subprocess.run(
            [crontab, "-l"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return f"Cron preflight probe failed: crontab -l could not run: {exc}"

    if result.returncode != 0:
        # ``crontab -l`` exits 1 when the user has no crontab — that is normal.
        # Anything else (e.g. permission denied, service missing) is a real problem.
        stderr = (result.stderr or "").strip()
        if "no crontab" not in stderr.lower() and "no such file" not in stderr.lower():
            return (
                f"Cron preflight probe failed: crontab -l exited {result.returncode}."
                + (f"\nstderr: {stderr}" if stderr else "")
            )

    # Best-effort daemon presence check on common Unix paths.
    daemon_paths = [Path("/usr/sbin/cron"), Path("/usr/sbin/crond")]
    if sys.platform != "win32" and not any(p.exists() for p in daemon_paths):
        # Not a hard failure: some container/embedded environments have no
        # separate daemon binary, but Hermes still uses the internal scheduler.
        logger.debug("No separate cron daemon binary found; relying on internal scheduler")

    return None


def _format_cron_error(
    exc: Exception,
    operation: str,
    cli_output: Optional[str] = None,
) -> str:
    """Return a rich error string for cron failures.

    Includes the operation name, the exception message, and any captured CLI
    output so the user can see the root cause without re-running manually.
    """
    parts = [f"Cron operation '{operation}' failed: {exc}"]
    if cli_output:
        parts.append(f"Underlying output:\n{cli_output}")
    return "\n".join(parts)


def _validate_cron_action(action: Optional[str]) -> Tuple[str, Optional[str]]:
    """Normalize and validate the cron action parameter.

    Returns ``(normalized_action, error)``; ``error`` is ``None`` when valid.
    """
    normalized = (action or "").strip().lower()
    if not normalized:
        return "", "action is required"
    allowed = {"create", "list", "update", "pause", "resume", "remove", "run", "run_now", "trigger"}
    if normalized not in allowed:
        return normalized, f"Unknown cron action '{action}'. Allowed: {', '.join(sorted(allowed - {'run_now', 'trigger'}))}"
    return normalized, None


# ---------------------------------------------------------------------------
# Cron prompt scanning
# ---------------------------------------------------------------------------
#
# Two threat surfaces, two scanners:
#
#   1. User-supplied cron prompt (small, written as a directive).
#      Strict scanning is appropriate — a legit cron prompt has no business
#      saying "cat ~/.hermes/.env" or "rm -rf /". `_scan_cron_prompt()` runs
#      against this at create/update time and as a runtime defense-in-depth.
#
#   2. Assembled prompt that includes loaded skill content (large markdown
#      bodies, often security docs, postmortems, runbooks discussing attack
#      patterns in PROSE). Reusing the strict patterns here false-positives
#      every time a skill *describes* a command — see #3968 follow-up: the
#      `hermes-agent-dev` skill contains a security postmortem mentioning
#      `cat ~/.hermes/.env`, which tripped `read_secrets` and silently
#      killed all PR-scout jobs.
#
#      Skill bodies are user-curated and scanned at install time by
#      `skills_guard.py`. The runtime cron scan only needs to catch the
#      patterns whose phrasing does NOT survive normal English prose:
#      classic prompt-injection directives ("ignore previous instructions",
#      "disregard your rules"), deception directives, and invisible
#      unicode. `_scan_cron_skill_assembled()` runs against the assembled
#      prompt with this tighter pattern set.
#
# Both scanners share the invisible-unicode check and the GitHub Authorization
# header exemption.

# Strict patterns — applied to the user prompt only.
_CRON_THREAT_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"),
    (r'/etc/sudoers|visudo', "sudoers_mod"),
    (r'rm\s+-rf\s+/', "destructive_root_rm"),
]

# Looser pattern set — applied to the assembled prompt when skills are
# attached. Only patterns whose phrasing is unambiguous in any context;
# command-shape patterns are dropped because they false-positive on prose
# in security docs / postmortems. Skill bodies are scanned at install time
# by `skills_guard.py`, so the runtime cron scan is purely a tripwire for
# obvious injection directives surviving a malicious skill that slipped
# through install.
_CRON_SKILL_ASSEMBLED_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
]

_CRON_SECRET_VAR_RE = r'\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?'
_CRON_EXFIL_COMMAND_PATTERNS = [
    # Tighten exfil detection to obvious leak paths: embedding a secret
    # directly in the destination URL, sending it in POST/FORM payloads,
    # or shipping it via Authorization headers to arbitrary hosts. The
    # only intended allowlist exception today is the bundled GitHub skill
    # pattern that talks to api.github.com.
    (rf'curl\s+[^\n]*https?://[^\s"\'`]*{_CRON_SECRET_VAR_RE}', "exfil_curl_url"),
    (rf'wget\s+[^\n]*https?://[^\s"\'`]*{_CRON_SECRET_VAR_RE}', "exfil_wget_url"),
    (rf'curl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|-d|--form|-F)\s+[^\n]*{_CRON_SECRET_VAR_RE}', "exfil_curl_data"),
    (rf'wget\s+[^\n]*--post-(?:data|file)=[^\n]*{_CRON_SECRET_VAR_RE}', "exfil_wget_post"),
    (rf'curl\s+[^\n]*(?:-H|--header)\s+["\']Authorization:\s*(?:Bearer|token)\s+{_CRON_SECRET_VAR_RE}["\']', "exfil_curl_auth_header"),
]

_CRON_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}

# U+200D Zero-Width Joiner is also a legitimate, required part of many
# Unicode emoji sequences (for example 👨‍👩‍👧, 🏳️‍🌈, ❤️‍🩹, 🧑‍💻).
# It is also used in some multilingual scripts (e.g. Arabic, Devanagari,
# Cyrillic formatting) to control ligature/joining behavior.
# We should still block ZWJ when it is hiding between plain text characters
# (Latin, digits, punctuation), but not when it is clearly part of an emoji
# grapheme cluster or between letters from a script that legitimately uses ZWJ.
_EMOJI_NEIGHBOUR_CP_RANGES = (
    (0x1F000, 0x1FFFF),
    (0x2600, 0x27BF),
    (0x2300, 0x23FF),
    (0x1F1E6, 0x1F1FF),
    (0x20E3, 0x20E3),
)
_VARIATION_SELECTOR_CP = 0xFE0F


def _is_emoji_cp(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _EMOJI_NEIGHBOUR_CP_RANGES)


def _is_script_using_zwj(cp: int) -> bool:
    """Return True for codepoints from scripts that legitimately use ZWJ."""
    # Arabic (U+0600–U+06FF, U+0750–U+077F, U+08A0–U+08FF, U+FB50–U+FDFF, U+FE70–U+FEFF)
    if 0x0600 <= cp <= 0x06FF:
        return True
    if 0x0750 <= cp <= 0x077F:
        return True
    if 0x08A0 <= cp <= 0x08FF:
        return True
    if 0xFB50 <= cp <= 0xFDFF:
        return True
    if 0xFE70 <= cp <= 0xFEFF:
        return True
    # Devanagari (U+0900–U+097F)
    if 0x0900 <= cp <= 0x097F:
        return True
    # Bengali (U+0980–U+09FF)
    if 0x0980 <= cp <= 0x09FF:
        return True
    # Gurmukhi (U+0A00–U+0A7F)
    if 0x0A00 <= cp <= 0x0A7F:
        return True
    # Gujarati (U+0A80–U+0AFF)
    if 0x0A80 <= cp <= 0x0AFF:
        return True
    # Oriya (U+0B00–U+0B7F)
    if 0x0B00 <= cp <= 0x0B7F:
        return True
    # Tamil (U+0B80–U+0BFF)
    if 0x0B80 <= cp <= 0x0BFF:
        return True
    # Telugu (U+0C00–U+0C7F)
    if 0x0C00 <= cp <= 0x0C7F:
        return True
    # Kannada (U+0C80–U+0CFF)
    if 0x0C80 <= cp <= 0x0CFF:
        return True
    # Malayalam (U+0D00–U+0D7F)
    if 0x0D00 <= cp <= 0x0D7F:
        return True
    # Sinhala (U+0D80–U+0DFF)
    if 0x0D80 <= cp <= 0x0DFF:
        return True
    # Hebrew (U+0590–U+05FF)
    if 0x0590 <= cp <= 0x05FF:
        return True
    # Syriac (U+0700–U+074F)
    if 0x0700 <= cp <= 0x074F:
        return True
    # Thaana (U+0780–U+07BF)
    if 0x0780 <= cp <= 0x07BF:
        return True
    # Myanmar (U+1000–U+109F)
    if 0x1000 <= cp <= 0x109F:
        return True
    # Khmer (U+1780–U+17FF)
    if 0x1780 <= cp <= 0x17FF:
        return True
    # Tibetan (U+0F00–U+0FFF)
    if 0x0F00 <= cp <= 0x0FFF:
        return True
    # Georgian (U+10A0–U+10FF, U+2D00–U+2D2F)
    if 0x10A0 <= cp <= 0x10FF:
        return True
    if 0x2D00 <= cp <= 0x2D2F:
        return True
    # Ethiopic (U+1200–U+137F)
    if 0x1200 <= cp <= 0x137F:
        return True
    # Cherokee (U+13A0–U+13FF)
    if 0x13A0 <= cp <= 0x13FF:
        return True
    # Canadian Aboriginal (U+1400–U+167F)
    if 0x1400 <= cp <= 0x167F:
        return True
    # Mongolian (U+1800–U+18AF)
    if 0x1800 <= cp <= 0x18AF:
        return True
    # Limbu (U+1900–U+194F)
    if 0x1900 <= cp <= 0x194F:
        return True
    # Tai Le (U+1950–U+197F)
    if 0x1950 <= cp <= 0x197F:
        return True
    # New Tai Lue (U+1980–U+19DF)
    if 0x1980 <= cp <= 0x19DF:
        return True
    # Buginese (U+1A00–U+1A1F)
    if 0x1A00 <= cp <= 0x1A1F:
        return True
    # Tai Tham (U+1A20–U+1AAF)
    if 0x1A20 <= cp <= 0x1AAF:
        return True
    # Balinese (U+1B00–U+1B7F)
    if 0x1B00 <= cp <= 0x1B7F:
        return True
    # Sundanese (U+1B80–U+1BBF)
    if 0x1B80 <= cp <= 0x1BBF:
        return True
    # Batak (U+1BC0–U+1BFF)
    if 0x1BC0 <= cp <= 0x1BFF:
        return True
    # Lepcha (U+1C00–U+1C4F)
    if 0x1C00 <= cp <= 0x1C4F:
        return True
    # Ol Chiki (U+1C50–U+1C7F)
    if 0x1C50 <= cp <= 0x1C7F:
        return True
    # Cyrillic (U+0400–U+04FF, U+0500–U+052F, U+2DE0–U+2DFF, U+A640–U+A69F)
    if 0x0400 <= cp <= 0x04FF:
        return True
    if 0x0500 <= cp <= 0x052F:
        return True
    if 0x2DE0 <= cp <= 0x2DFF:
        return True
    if 0xA640 <= cp <= 0xA69F:
        return True
    return False


def _zwj_has_emoji_neighbour(text: str, idx: int) -> bool:
    """Return True when the ZWJ at text[idx] appears inside an emoji sequence."""
    left = idx - 1
    while left >= 0 and ord(text[left]) == _VARIATION_SELECTOR_CP:
        left -= 1
    right = idx + 1
    while right < len(text) and ord(text[right]) == _VARIATION_SELECTOR_CP:
        right += 1
    return (
        left >= 0 and right < len(text)
        and _is_emoji_cp(ord(text[left]))
        and _is_emoji_cp(ord(text[right]))
    )


def _zwj_has_script_neighbour(text: str, idx: int) -> bool:
    """Return True when the ZWJ at text[idx] sits between letters from
    scripts that legitimately use ZWJ for joining/ligature control."""
    left = idx - 1
    while left >= 0 and ord(text[left]) == _VARIATION_SELECTOR_CP:
        left -= 1
    right = idx + 1
    while right < len(text) and ord(text[right]) == _VARIATION_SELECTOR_CP:
        right += 1
    if left < 0 or right >= len(text):
        return False
    return _is_script_using_zwj(ord(text[left])) and _is_script_using_zwj(ord(text[right]))


def _strip_legitimate_emoji_zwj(prompt: str) -> str:
    if '\u200d' not in prompt:
        return prompt
    cleaned: list[str] = []
    for idx, ch in enumerate(prompt):
        if ch == '\u200d' and (_zwj_has_emoji_neighbour(prompt, idx) or _zwj_has_script_neighbour(prompt, idx)):
            continue
        cleaned.append(ch)
    return ''.join(cleaned)


def _strip_cron_safe_constructs(prompt: str) -> str:
    """Strip the GitHub `Authorization: token $GITHUB_TOKEN` auth-header
    pattern so it doesn't trip the broader curl-auth-header exfil rule.

    Allows the bundled GitHub skill fallback without opening a blanket
    exemption for arbitrary Authorization-header exfiltration.
    """
    github_auth_header = re.search(
        rf'curl\s+[^\n]*(?:-H|--header)\s+["\']Authorization:\s*token\s+{_CRON_SECRET_VAR_RE}["\']'
        r'\s+["\']?https://api\.github\.com(?:/|\b)',
        prompt,
        re.IGNORECASE,
    )
    if github_auth_header:
        return prompt.replace(github_auth_header.group(0), "curl https://api.github.com/user")
    return prompt


def _check_invisible_unicode(prompt: str) -> str:
    """Return an error string if the prompt contains invisible-unicode
    injection markers (ZWJ inside legitimate emoji sequences is allowed).
    """
    prompt_for_invisible_scan = _strip_legitimate_emoji_zwj(prompt)
    for char in _CRON_INVISIBLE_CHARS:
        if char in prompt_for_invisible_scan:
            return f"Blocked: prompt contains invisible unicode U+{ord(char):04X} (possible injection)."
    return ""


def _strip_invisible_unicode(prompt: str) -> tuple[str, list[str]]:
    """Strip invisible-unicode characters from *prompt*, preserving the ZWJ
    that lives inside legitimate emoji sequences OR between letters from
    scripts that legitimately use ZWJ (Arabic, Devanagari, Cyrillic, etc.).

    Returns ``(cleaned_prompt, removed_codepoints)`` where ``removed_codepoints``
    is the sorted list of ``U+XXXX`` labels that were stripped (empty when the
    prompt was already clean). Used by the skills-attached cron path, where the
    skill body is already vetted at install time by ``skills_guard.py`` — a
    stray zero-width space in a code example should be sanitized, not turned
    into a hard block that permanently kills the job.
    """
    if not prompt:
        return prompt, []
    removed: set[str] = set()
    cleaned: list[str] = []
    for idx, ch in enumerate(prompt):
        if ch in _CRON_INVISIBLE_CHARS:
            if ch == '\u200d' and (_zwj_has_emoji_neighbour(prompt, idx) or _zwj_has_script_neighbour(prompt, idx)):
                cleaned.append(ch)  # legitimate ZWJ — keep
                continue
            removed.add(f"U+{ord(ch):04X}")
            continue
        cleaned.append(ch)
    return ''.join(cleaned), sorted(removed)


def _scan_cron_prompt(prompt: str) -> str:
    """Scan the USER-SUPPLIED cron prompt for critical threats.

    Strict pattern set — used at job create/update time and as a runtime
    defense-in-depth for prompts authored before the scanner existed.
    The user prompt is small and directive; bare `cat .env` or `rm -rf /`
    there is a smoking gun, not prose. Returns an error string when
    blocked, else empty string.
    """
    prompt_to_scan = _strip_cron_safe_constructs(prompt)
    invisible_err = _check_invisible_unicode(prompt_to_scan)
    if invisible_err:
        return invisible_err
    for pattern, pid in _CRON_THREAT_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return f"Blocked: prompt matches threat pattern '{pid}'. Cron prompts must not contain injection or exfiltration payloads."
    for pattern, pid in _CRON_EXFIL_COMMAND_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return f"Blocked: prompt matches threat pattern '{pid}'. Cron prompts must not contain injection or exfiltration payloads."
    return ""


def _scan_cron_skill_assembled(assembled: str) -> tuple[str, str]:
    """Scan an ASSEMBLED cron prompt that includes loaded skill content.

    Looser pattern set — only catches unambiguous prompt-injection
    directives. Drops command-shape patterns (cat .env, rm -rf /,
    authorized_keys, /etc/sudoers) because they false-positive on
    legitimate skill markdown that *describes* attack commands in
    security postmortems and runbooks.

    Invisible unicode is SANITIZED, not blocked. Skill bodies are
    user-curated and already scanned at install time by
    ``skills_guard.py``; a stray zero-width space in a code example
    (common in copy-pasted unicode docs) should not permanently kill the
    job. The offending codepoints are stripped and logged, the cleaned
    prompt is returned. The hard block remains for raw user prompts via
    ``_scan_cron_prompt`` — that path is the actual injection surface.

    Returns ``(cleaned_prompt, error)``; ``error`` is empty when the
    prompt passed (after sanitization).
    """
    cleaned, removed = _strip_invisible_unicode(assembled)
    if removed:
        logger.warning(
            "Cron skill-assembled prompt: stripped %d invisible-unicode "
            "char(s) (%s) from vetted skill content",
            len(removed), ", ".join(removed),
        )
    prompt_to_scan = _strip_cron_safe_constructs(cleaned)
    for pattern, pid in _CRON_SKILL_ASSEMBLED_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return cleaned, f"Blocked: prompt matches threat pattern '{pid}'. Cron prompts must not contain injection or exfiltration payloads."
    return cleaned, ""


def _origin_from_env() -> Optional[Dict[str, str]]:
    from gateway.session_context import get_session_env
    origin_platform = get_session_env("HERMES_SESSION_PLATFORM")
    origin_chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
    if origin_platform and origin_chat_id:
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID") or None
        if thread_id:
            logger.debug(
                "Cron origin captured thread_id=%s for %s:%s",
                thread_id, origin_platform, origin_chat_id,
            )
        return {
            "platform": origin_platform,
            "chat_id": origin_chat_id,
            "chat_name": get_session_env("HERMES_SESSION_CHAT_NAME") or None,
            "thread_id": thread_id,
        }
    return None


def _repeat_display(job: Dict[str, Any]) -> str:
    times = (job.get("repeat") or {}).get("times")
    completed = (job.get("repeat") or {}).get("completed", 0)
    if times is None:
        return "forever"
    if times == 1:
        return "once" if completed == 0 else "1/1"
    return f"{completed}/{times}" if completed else f"{times} times"


def _canonical_skills(skill: Optional[str] = None, skills: Optional[Any] = None) -> List[str]:
    if skills is None:
        raw_items = [skill] if skill else []
    elif isinstance(skills, str):
        raw_items = [skills]
    else:
        raw_items = list(skills)

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized




def _resolve_model_override(model_obj: Optional[Dict[str, Any]]) -> tuple:
    """Resolve a model override object into (provider, model) for job storage.

    If provider is omitted, pins the current main provider from config so the
    job doesn't drift when the user later changes their default via hermes model.

    Returns (provider_str_or_none, model_str_or_none).
    """
    if not model_obj or not isinstance(model_obj, dict):
        return (None, None)
    model_name = (model_obj.get("model") or "").strip() or None
    provider_name = (model_obj.get("provider") or "").strip() or None
    # Bare "custom" is usually an incomplete spec — the canonical form is
    # "custom:<name>" matching a custom_providers entry, and LLMs frequently
    # supply the bare type because the schema does not advertise the
    # ":<name>" suffix. It is only a problem when it can't resolve at runtime:
    # a user may literally name a ``providers.custom`` (or custom_providers
    # "custom") entry, in which case the job should keep ``provider="custom"``
    # and run against that endpoint. Only when no such entry exists do we treat
    # the bare value as "no provider supplied" and pin the current main
    # provider below — otherwise pinning to ``model.provider`` (e.g. codex)
    # silently hijacks a job that meant to use the configured custom endpoint.
    if provider_name == "custom":
        try:
            from hermes_cli.runtime_provider import has_named_custom_provider
            if not has_named_custom_provider("custom"):
                provider_name = None
        except Exception:
            provider_name = None
    if model_name and not provider_name:
        # Pin to the current main provider so the job is stable
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                provider_name = model_cfg.get("provider") or None
        except Exception:
            pass  # Best-effort; provider stays None
    return (provider_name, model_name)


def _normalize_optional_job_value(value: Optional[Any], *, strip_trailing_slash: bool = False) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if strip_trailing_slash:
        text = text.rstrip("/")
    return text or None


def _normalize_deliver_param(value: Any) -> Optional[str]:
    """Normalize a user-supplied ``deliver`` value to the canonical string form.

    The cron schema documents ``deliver`` as a string (``"local"``, ``"origin"``,
    ``"telegram"``, ``"telegram:chat_id[:thread_id]"``, or comma-separated combos).
    Some callers — MCP clients passing arrays, scripts building the payload as a
    list — supply ``["telegram"]``.  ``create_job``/``update_job`` store it as-is,
    and the scheduler's ``str(deliver).split(",")`` then serializes the list to
    the literal ``"['telegram']"`` which is not a known platform.  Flatten lists
    / tuples at the API boundary so storage is always a string.  Returns ``None``
    for ``None``/empty so callers can treat it as "not supplied".
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value if str(p).strip()]
        return ",".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _validate_cron_script_path(script: Optional[str]) -> Optional[str]:
    """Validate a cron job script path at the API boundary.

    Scripts must be relative paths that resolve within HERMES_HOME/scripts/.
    Absolute paths and ~ expansion are rejected to prevent arbitrary script
    execution via prompt injection.

    Returns an error string if blocked, else None (valid).
    """
    if not script or not script.strip():
        return None  # empty/None = clearing the field, always OK

    from hermes_constants import get_hermes_home

    raw = script.strip()

    # Reject absolute paths and ~ expansion at the API boundary.
    # Only relative paths within ~/.hermes/scripts/ are allowed.
    if raw.startswith(("/", "~")) or (len(raw) >= 2 and raw[1] == ":"):
        return (
            f"Script path must be relative to ~/.hermes/scripts/. "
            f"Got absolute or home-relative path: {raw!r}. "
            f"Place scripts in ~/.hermes/scripts/ and use just the filename."
        )

    # Validate containment after resolution
    from tools.path_security import validate_within_dir

    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    containment_error = validate_within_dir(scripts_dir / raw, scripts_dir)
    if containment_error:
        return (
            f"Script path escapes the scripts directory via traversal: {raw!r}"
        )

    return None


def _format_job(job: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(job.get("prompt") or "")
    skills = _canonical_skills(job.get("skill"), job.get("skills"))
    job_id = str(job.get("id") or "unknown")
    name = str(job.get("name") or prompt[:50] or (skills[0] if skills else "") or job_id or "cron job")
    result = {
        "job_id": job_id,
        "name": name,
        "skill": skills[0] if skills else None,
        "skills": skills,
        "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
        "model": job.get("model"),
        "provider": job.get("provider"),
        "base_url": job.get("base_url"),
        "schedule": job.get("schedule_display") or "?",
        "repeat": _repeat_display(job),
        "deliver": job.get("deliver", "local"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_delivery_error": job.get("last_delivery_error"),
        "enabled": job.get("enabled", True),
        "state": job.get("state", "scheduled" if job.get("enabled", True) else "paused"),
        "paused_at": job.get("paused_at"),
        "paused_reason": job.get("paused_reason"),
    }
    if job.get("script"):
        result["script"] = job["script"]
    if job.get("no_agent"):
        result["no_agent"] = True
    if job.get("enabled_toolsets"):
        result["enabled_toolsets"] = job["enabled_toolsets"]
    if job.get("workdir"):
        result["workdir"] = job["workdir"]
    return result


def _execute_job_now(job: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a cron job immediately, outside the scheduler tick.

    Atomically claims the job first via ``claim_job_for_fire`` — the same
    at-most-once CAS the scheduler/external-provider fire path uses — so a
    concurrently-running gateway ticker cannot also fire it (the claim both
    blocks a duplicate fire and advances ``next_run_at`` for recurring jobs).
    If the claim is lost (another fire is in flight), this is a no-op.

    The actual firing is delegated to ``run_one_job`` — the single shared
    execute→save→deliver→mark body the ticker and external providers use — so
    failure delivery, ``[SILENT]`` handling, and live-adapter delivery stay
    identical across paths and can't drift.

    Returns {"claimed": bool, "success": bool, "error": str|None}.
    """
    job_id = job["id"]
    try:
        from cron.scheduler import run_one_job

        # At-most-once claim: bail without running if a tick/other fire owns it.
        if not claim_job_for_fire(job_id):
            return {"claimed": False, "success": False,
                    "error": "Job is already being fired by the scheduler; not run again."}

        # run_one_job records last_run_at/last_status via mark_job_run (which
        # also clears the fire claim) and returns True iff it processed the job.
        processed = run_one_job(job)
        refreshed = get_job(job_id) or {}
        ok = refreshed.get("last_status") == "ok"
        return {
            "claimed": True,
            "success": bool(processed and ok),
            "error": refreshed.get("last_error"),
        }

    except Exception as e:
        logger.error("Failed to execute cron job %s immediately: %s", job_id, e)
        try:
            mark_job_run(job_id, False, str(e))
        except Exception:
            pass
        return {"claimed": True, "success": False, "error": str(e)}


def cronjob(
    action: str,
    job_id: Optional[str] = None,
    prompt: Optional[str] = None,
    schedule: Optional[str] = None,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    include_disabled: bool = False,
    skill: Optional[str] = None,
    skills: Optional[List[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    reason: Optional[str] = None,
    script: Optional[str] = None,
    context_from: Optional[Union[str, List[str]]] = None,
    enabled_toolsets: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    no_agent: Optional[bool] = None,
    task_id: str = None,
) -> str:
    """Unified cron job management tool."""

    normalized, action_error = _validate_cron_action(action)
    if action_error:
        return tool_error(action_error, success=False)

    # Retry-rate limiter: noisy failure loops are capped per task.
    failure_streak = _record_cron_failure(task_id)
    if failure_streak > _MAX_CONSECUTIVE_CRON_FAILURES:
        return tool_error(
            f"Cron tool has failed {failure_streak} consecutive times for this task "
            "(too many attempts). Please inspect the earlier errors or run "
            "cronjob(action='list') before retrying.",
            success=False,
        )

    # Preflight: ensure cron is available before invoking backend operations.
    preflight_error = _cron_preflight_check()
    if preflight_error:
        return tool_error(preflight_error, success=False)

    try:
        if normalized == "create":
            if not schedule:
                return tool_error("schedule is required for create", success=False)
            canonical_skills = _canonical_skills(skill, skills)
            _no_agent = bool(no_agent)
            # Job-shape validation differs by mode:
            #   - no_agent=True → script is the job; prompt/skills are optional
            #     (and irrelevant to execution).
            #   - no_agent=False (default) → at least one of prompt/skills must
            #     be set, same as before.
            if _no_agent:
                if not script:
                    return tool_error(
                        "create with no_agent=True requires a script — "
                        "the script is the job.",
                        success=False,
                    )
            elif not prompt and not canonical_skills:
                return tool_error("create requires either prompt or at least one skill", success=False)
            if prompt:
                scan_error = _scan_cron_prompt(prompt)
                if scan_error:
                    return tool_error(scan_error, success=False)

            # Validate script path before storing
            if script:
                script_error = _validate_cron_script_path(script)
                if script_error:
                    return tool_error(script_error, success=False)

            # Validate context_from references existing jobs
            if context_from:
                from cron.jobs import get_job as _get_job
                refs = [context_from] if isinstance(context_from, str) else context_from
                for ref_id in refs:
                    if not _get_job(ref_id):
                        return tool_error(
                            f"context_from job '{ref_id}' not found. "
                            "Use cronjob(action='list') to see available jobs.",
                            success=False,
                        )

            job = create_job(
                prompt=prompt or "",
                schedule=schedule,
                name=name,
                repeat=repeat,
                deliver=_normalize_deliver_param(deliver),
                origin=_origin_from_env(),
                skills=canonical_skills,
                model=_normalize_optional_job_value(model),
                provider=_normalize_optional_job_value(provider),
                base_url=_normalize_optional_job_value(base_url, strip_trailing_slash=True),
                script=_normalize_optional_job_value(script),
                context_from=context_from,
                enabled_toolsets=enabled_toolsets or None,
                workdir=_normalize_optional_job_value(workdir),
                no_agent=_no_agent,
            )
            _reset_cron_failure(task_id)
            _notify_provider_jobs_changed_safe()
            return json.dumps(
                {
                    "success": True,
                    "job_id": job["id"],
                    "name": job["name"],
                    "skill": job.get("skill"),
                    "skills": job.get("skills", []),
                    "schedule": job["schedule_display"],
                    "repeat": _repeat_display(job),
                    "deliver": job.get("deliver", "local"),
                    "next_run_at": job["next_run_at"],
                    "job": _format_job(job),
                    "message": f"Cron job '{job['name']}' created.",
                },
                indent=2,
            )

        if normalized == "list":
            jobs = [_format_job(job) for job in list_jobs(include_disabled=include_disabled)]
            _reset_cron_failure(task_id)
            return json.dumps({"success": True, "count": len(jobs), "jobs": jobs}, indent=2)

        if not job_id:
            return tool_error(f"job_id is required for action '{normalized}'", success=False)

        try:
            job = resolve_job_ref(job_id)
        except AmbiguousJobReference as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                    "matches": [
                        {
                            "id": m["id"],
                            "name": m.get("name"),
                            "schedule": m.get("schedule_display"),
                            "next_run_at": m.get("next_run_at"),
                        }
                        for m in exc.matches
                    ],
                },
                indent=2,
            )
        if not job:
            return json.dumps(
                {"success": False, "error": f"Job with ID or name '{job_id}' not found. Use cronjob(action='list') to inspect jobs."},
                indent=2,
            )
        # Resolve to canonical ID (supports name-based lookup)
        job_id = job["id"]

        if normalized == "remove":
            removed = remove_job(job_id)
            if not removed:
                return tool_error(f"Failed to remove job '{job_id}'", success=False)
            _reset_cron_failure(task_id)
            _notify_provider_jobs_changed_safe()
            return json.dumps(
                {
                    "success": True,
                    "message": f"Cron job '{job['name']}' removed.",
                    "removed_job": {
                        "id": job_id,
                        "name": job["name"],
                        "schedule": job.get("schedule_display"),
                    },
                },
                indent=2,
            )

        if normalized == "pause":
            updated = pause_job(job_id, reason=reason)
            _reset_cron_failure(task_id)
            _notify_provider_jobs_changed_safe()
            return json.dumps({"success": True, "job": _format_job(updated)}, indent=2)

        if normalized == "resume":
            updated = resume_job(job_id)
            _reset_cron_failure(task_id)
            _notify_provider_jobs_changed_safe()
            return json.dumps({"success": True, "job": _format_job(updated)}, indent=2)

        if normalized in {"run", "run_now", "trigger"}:
            _reset_cron_failure(task_id)
            # Execute the job immediately rather than only scheduling it for the
            # next scheduler tick — a manual `run` should actually run, even when
            # no gateway/ticker is active (the #41037 case). The claim inside
            # _execute_job_now advances next_run_at and blocks a concurrent tick
            # from double-firing.
            exec_result = _execute_job_now(job)
            # Re-read so the response reflects the post-run last_run_at/last_status.
            result = _format_job(get_job(job_id) or {"id": job_id})
            result["executed"] = exec_result.get("claimed", False)
            result["execution_success"] = exec_result.get("success", False)
            if not exec_result.get("claimed", False):
                result["execution_skipped"] = (
                    "Already being fired by the scheduler; not run again."
                )
            elif exec_result.get("error"):
                result["execution_error"] = exec_result["error"]
            return json.dumps({"success": True, "job": result}, indent=2)

        if normalized == "update":
            updates: Dict[str, Any] = {}
            if prompt is not None:
                scan_error = _scan_cron_prompt(prompt)
                if scan_error:
                    return tool_error(scan_error, success=False)
                updates["prompt"] = prompt
            if name is not None:
                updates["name"] = name
            if deliver is not None:
                updates["deliver"] = _normalize_deliver_param(deliver)
            if skills is not None or skill is not None:
                canonical_skills = _canonical_skills(skill, skills)
                updates["skills"] = canonical_skills
                updates["skill"] = canonical_skills[0] if canonical_skills else None
            if model is not None:
                updates["model"] = _normalize_optional_job_value(model)
            if provider is not None:
                updates["provider"] = _normalize_optional_job_value(provider)
            if base_url is not None:
                updates["base_url"] = _normalize_optional_job_value(base_url, strip_trailing_slash=True)
            if script is not None:
                # Pass empty string to clear an existing script
                if script:
                    script_error = _validate_cron_script_path(script)
                    if script_error:
                        return tool_error(script_error, success=False)
                updates["script"] = _normalize_optional_job_value(script) if script else None
            if context_from is not None:
                # Empty string / empty list clears the field; otherwise validate
                # each referenced job exists before storing. Normalized to a list
                # (or None) to match the shape stored by create_job().
                if isinstance(context_from, str):
                    refs = [context_from.strip()] if context_from.strip() else []
                else:
                    refs = [str(j).strip() for j in context_from if str(j).strip()]
                if refs:
                    from cron.jobs import get_job as _get_job
                    for ref_id in refs:
                        if not _get_job(ref_id):
                            return tool_error(
                                f"context_from job '{ref_id}' not found. "
                                "Use cronjob(action='list') to see available jobs.",
                                success=False,
                            )
                updates["context_from"] = refs or None
            if enabled_toolsets is not None:
                updates["enabled_toolsets"] = enabled_toolsets or None
            if workdir is not None:
                # Empty string clears the field (restores old behaviour);
                # otherwise pass raw — update_job() validates / normalizes.
                updates["workdir"] = _normalize_optional_job_value(workdir) or None
            if no_agent is not None:
                # Toggling no_agent on/off at update time. If flipping to True,
                # we need a script to already exist on the job (or be part of
                # the same update) — otherwise the next tick would error out.
                target_no_agent = bool(no_agent)
                if target_no_agent:
                    effective_script = updates.get("script") if "script" in updates else job.get("script")
                    if not effective_script:
                        return tool_error(
                            "Cannot set no_agent=True on a job without a script. "
                            "Set `script` in the same update, or on the job first.",
                            success=False,
                        )
                updates["no_agent"] = target_no_agent
            if repeat is not None:
                # Normalize: treat 0 or negative as None (infinite)
                normalized_repeat = None if repeat <= 0 else repeat
                repeat_state = dict(job.get("repeat") or {})
                repeat_state["times"] = normalized_repeat
                updates["repeat"] = repeat_state
            if schedule is not None:
                parsed_schedule = parse_schedule(schedule)
                updates["schedule"] = parsed_schedule
                updates["schedule_display"] = parsed_schedule.get("display", schedule)
                if job.get("state") != "paused":
                    updates["state"] = "scheduled"
                    updates["enabled"] = True
            if not updates:
                return tool_error("No updates provided.", success=False)
            updated = update_job(job_id, updates)
            _reset_cron_failure(task_id)
            _notify_provider_jobs_changed_safe()
            return json.dumps({"success": True, "job": _format_job(updated)}, indent=2)

        return tool_error(f"Unknown cron action '{action}'", success=False)

    except Exception as e:
        _record_cron_failure(task_id)
        cli_output = getattr(e, "output", None)
        return tool_error(_format_cron_error(e, normalized or action or "unknown", cli_output=cli_output), success=False)



CRONJOB_SCHEMA = {
    "name": "cronjob",
    "description": """Manage scheduled cron jobs with a single compressed tool.

Use action='create' to schedule a new job from a prompt or one or more skills.
Use action='list' to inspect jobs.
Use action='update', 'pause', 'resume', 'remove', or 'run' to manage an existing job.

To stop a job the user no longer wants: first action='list' to find the job_id, then action='remove' with that job_id. Never guess job IDs — always list first.

Jobs run in a fresh session with no current-chat context, so prompts must be self-contained.
If skills are provided on create, the future cron run loads those skills in order, then follows the prompt as the task instruction.
On update, passing skills=[] clears attached skills.

NOTE: The agent's final response is auto-delivered to the target. Put the primary
user-facing content in the final response. Cron jobs run autonomously with no user
present — they cannot ask questions or request clarification.

Important safety rule: cron-run sessions should not recursively schedule more cron jobs.""",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: create, list, update, pause, resume, remove, run. When action=create, the 'schedule' and 'prompt' fields are REQUIRED."
            },
            "job_id": {
                "type": "string",
                "description": "Required for update/pause/resume/remove/run"
            },
            "prompt": {
                "type": "string",
                "description": "For create: the full self-contained prompt. If skills are also provided, this becomes the task instruction paired with those skills."
            },
            "schedule": {
                "type": "string",
                "description": "REQUIRED for action=create. For create/update: '30m', 'every 2h', '0 9 * * *', or ISO timestamp. Examples: '30m' (every 30 minutes), 'every 2h' (every 2 hours), '0 9 * * *' (daily at 9am), '2026-06-01T09:00:00' (one-shot). You MUST include this field when action=create."
            },
            "name": {
                "type": "string",
                "description": "Optional human-friendly name"
            },
            "repeat": {
                "type": "integer",
                "description": "Optional repeat count. Omit for defaults (once for one-shot, forever for recurring)."
            },
            "deliver": {
                "type": "string",
                "description": "Omit this parameter to auto-deliver back to the current chat and topic (recommended). Auto-detection preserves thread/topic context. Only set explicitly when the user asks to deliver somewhere OTHER than the current conversation. Values: 'origin' (same as omitting), 'local' (no delivery, save only), 'all' (fan out to every connected home channel), or platform:chat_id:thread_id for a specific destination. Combine with comma: 'origin,all' delivers to the origin plus every other connected channel. Examples: 'telegram:-1001234567890:17585', 'discord:#engineering', 'sms:+15551234567', 'all'. WARNING: 'platform:chat_id' without :thread_id loses topic targeting. 'all' resolves at fire time, so a job created before a channel was wired up will pick it up automatically once connected."
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional ordered list of skill names to load before executing the cron prompt. On update, pass an empty array to clear attached skills."
            },
            "model": {
                "type": "object",
                "description": "Optional per-job model override. If provider is omitted, the current main provider is pinned at creation time so the job stays stable.",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Provider name (e.g. 'openrouter', 'anthropic', or 'custom:<name>' for a provider defined in custom_providers config — always include the ':<name>' suffix, never pass the bare 'custom'). Omit to use and pin the current provider."
                    },
                    "model": {
                        "type": "string",
                        "description": "Model name (e.g. 'anthropic/claude-sonnet-4', 'claude-sonnet-4')"
                    }
                },
                "required": ["model"]
            },
            "script": {
                "type": "string",
                "description": f"Optional path to a script that runs each tick. In the default mode its stdout is injected into the agent's prompt as context (data-collection / change-detection pattern). With no_agent=True, the script IS the job and its stdout is delivered verbatim (classic watchdog pattern). Relative paths resolve under {display_hermes_home()}/scripts/. ``.sh``/``.bash`` extensions run via bash, everything else via Python. On update, pass empty string to clear."
            },
            "no_agent": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Default: False (LLM-driven job — the agent runs the prompt each tick). "
                    "Set True to skip the LLM entirely: the scheduler just runs ``script`` on schedule and delivers its stdout verbatim. No tokens, no agent loop, no model override honoured. "
                    "\n\n"
                    "REQUIREMENTS when True: ``script`` MUST be set (``prompt`` and ``skills`` are ignored). "
                    "\n\n"
                    "DELIVERY SEMANTICS when True: "
                    "(a) non-empty stdout is sent verbatim as the message; "
                    "(b) EMPTY stdout means SILENT — nothing is sent to the user and they won't see anything happened, so design your script to stay quiet when there's nothing to report (the watchdog pattern); "
                    "(c) non-zero exit / timeout sends an error alert so a broken watchdog can't fail silently. "
                    "\n\n"
                    "WHEN TO USE True: recurring script-only pings where the script itself produces the exact message text (memory/disk/GPU watchdogs, threshold alerts, heartbeats, CI notifications, API pollers with a fixed output shape). "
                    "WHEN TO USE False (default): anything that needs reasoning — summarize a feed, draft a daily briefing, pick interesting items, rephrase data for a human, follow conditional logic based on content."
                ),
            },
            "context_from": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional job ID or list of job IDs whose most recent completed output is "
                    "injected into the prompt as context before each run. "
                    "Use this to chain cron jobs: job A collects data, job B processes it. "
                    "Each entry must be a valid job ID (from cronjob action='list'). "
                    "Note: injects the most recent completed output — does not wait for "
                    "upstream jobs running in the same tick. "
                    "On update, pass an empty array to clear."
                ),
            },
            "enabled_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of toolset names to restrict the job's agent to (e.g. [\"web\", \"terminal\", \"file\", \"delegation\"]). When set, only tools from these toolsets are loaded, significantly reducing input token overhead. When omitted, all default tools are loaded. Infer from the job's prompt — e.g. use \"web\" if it calls web_search, \"terminal\" if it runs scripts, \"file\" if it reads files, \"delegation\" if it calls delegate_task. On update, pass an empty array to clear."
            },
            "workdir": {
                "type": "string",
                "description": "Optional absolute path to run the job from. When set, AGENTS.md / CLAUDE.md / .cursorrules from that directory are injected into the system prompt, and the terminal/file/code_exec tools use it as their working directory — useful for running a job inside a specific project repo. Must be an absolute path that exists. When unset (default), preserves the original behaviour: no project context files, tools use the scheduler's cwd. On update, pass an empty string to clear. Jobs with workdir run sequentially (not parallel) to keep per-job directories isolated."
            },
        },
        "required": ["action"]
    }
}


def check_cronjob_requirements() -> bool:
    """
    Check if cronjob tools can be used.

    Available in interactive CLI mode and gateway/messaging platforms.
    The cron system is internal (JSON file-based scheduler ticked by the gateway),
    so no external crontab executable is required.

    Session env vars must hold an explicit truthy string (``1``, ``true``,
    ``yes``, ``on``) — false-like values (``0``, ``false``, ``no``, ``off``)
    leave the tool disabled. Uses the shared ``env_var_enabled`` helper so
    every consumer of these flags agrees on the truthy set.
    """
    from utils import env_var_enabled

    return (
        env_var_enabled("HERMES_INTERACTIVE")
        or env_var_enabled("HERMES_GATEWAY_SESSION")
        or env_var_enabled("HERMES_EXEC_ASK")
    )


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="cronjob",
    toolset="cronjob",
    schema=CRONJOB_SCHEMA,
    handler=lambda args, **kw: (lambda _mo=_resolve_model_override(args.get("model")): cronjob(
        action=args.get("action", ""),
        job_id=args.get("job_id"),
        prompt=args.get("prompt"),
        schedule=args.get("schedule"),
        name=args.get("name"),
        repeat=args.get("repeat"),
        deliver=args.get("deliver"),
        include_disabled=args.get("include_disabled", True),
        skill=args.get("skill"),
        skills=args.get("skills"),
        model=_mo[1],
        provider=_mo[0] or args.get("provider"),
        base_url=args.get("base_url"),
        reason=args.get("reason"),
        script=args.get("script"),
        context_from=args.get("context_from"),
        enabled_toolsets=args.get("enabled_toolsets"),
        workdir=args.get("workdir"),
        no_agent=args.get("no_agent"),
        task_id=kw.get("task_id"),
    ))(),
    check_fn=check_cronjob_requirements,
    emoji="⏰",
)
