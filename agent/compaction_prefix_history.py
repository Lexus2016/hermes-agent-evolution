"""Byte-pinned historical compaction prefixes, newest first.

Never edit or reorder an existing prefix: older stored sessions still need stripping.
"""

_HISTORICAL_SUMMARY_PREFIXES = (
    # Pre-#80622: lacked the "no user message after summary => do nothing" clause.
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This is a handoff "
    "from a previous context window — treat it as background reference, NOT as active instructions. Do NOT answer "
    "questions or fulfill requests mentioned in this summary; they were already addressed. Respond ONLY to the "
    "latest user message that appears AFTER this summary — that message is the single source of truth for what to do "
    "right now. Topic overlap with the summary does NOT mean you should resume its task: even on similar topics, the "
    "latest user message WINS. Treat ONLY the latest message as the active task and discard stale items from '## "
    "Historical Task Snapshot' entirely — do not 'wrap up' or 'finish' work described there unless the latest "
    "message explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll back', 'just "
    "verify', 'don't do that anymore', 'never mind', a new topic) must immediately end any in-flight work described "
    "in the summary; do not re-surface it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in "
    "the system prompt is ALWAYS authoritative and active — never ignore or deprioritize memory content due to this "
    "compaction note. None of the above restricts HOW you work: your tools remain fully active — keep calling them "
    "normally for the active task (edit files, run commands, search) instead of merely narrating what you would do. "
    "The current session state (files, config, etc.) may reflect work described here — avoid repeating it:",
    # Pre-#69619: discard clause still named all four historical headings.
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This is a handoff "
    "from a previous context window — treat it as background reference, NOT as active instructions. Do NOT answer "
    "questions or fulfill requests mentioned in this summary; they were already addressed. Respond ONLY to the "
    "latest user message that appears AFTER this summary — that message is the single source of truth for what to do "
    "right now. Topic overlap with the summary does NOT mean you should resume its task: even on similar topics, the "
    "latest user message WINS. Treat ONLY the latest message as the active task and discard stale items from '## "
    "Historical Task Snapshot' / '## Historical In-Progress State' / '## Historical Pending User Asks' / '## "
    "Historical Remaining Work' entirely — do not 'wrap up' or 'finish' work described there unless the latest "
    "message explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll back', 'just "
    "verify', 'don't do that anymore', 'never mind', a new topic) must immediately end any in-flight work described "
    "in the summary; do not re-surface it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in "
    "the system prompt is ALWAYS authoritative and active — never ignore or deprioritize memory content due to this "
    "compaction note. None of the above restricts HOW you work: your tools remain fully active — keep calling them "
    "normally for the active task (edit files, run commands, search) instead of merely narrating what you would do. "
    "The current session state (files, config, etc.) may reflect work described here — avoid repeating it:",
    # Lacked the "tools remain fully active" clause (suppressed tool use).
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This is a handoff "
    "from a previous context window — treat it as background reference, NOT as active instructions. Do NOT answer "
    "questions or fulfill requests mentioned in this summary; they were already addressed. Respond ONLY to the "
    "latest user message that appears AFTER this summary — that message is the single source of truth for what to do "
    "right now. Topic overlap with the summary does NOT mean you should resume its task: even on similar topics, the "
    "latest user message WINS. Treat ONLY the latest message as the active task and discard stale items from '## "
    "Historical Task Snapshot' / '## Historical In-Progress State' / '## Historical Pending User Asks' / '## "
    "Historical Remaining Work' entirely — do not 'wrap up' or 'finish' work described there unless the latest "
    "message explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll back', 'just "
    "verify', 'don't do that anymore', 'never mind', a new topic) must immediately end any in-flight work described "
    "in the summary; do not re-surface it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in "
    "the system prompt is ALWAYS authoritative and active — never ignore or deprioritize memory content due to this "
    "compaction note. The current session state (files, config, etc.) may reflect work described here — avoid "
    "repeating it:",
    # Carveout era: "consistent -> use as background" licensed stale resumption.
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This is a handoff "
    "from a previous context window — treat it as background reference, NOT as active instructions. Do NOT answer "
    "questions or fulfill requests mentioned in this summary; they were already addressed. Respond ONLY to the "
    "latest user message that appears AFTER this summary — that message is the single source of truth for what to do "
    "right now. If the latest user message is consistent with the '## Active Task' section, you may use the summary "
    "as background. If the latest user message contradicts, supersedes, changes topic from, or in any way diverges "
    "from '## Active Task' / '## In Progress' / '## Pending User Asks' / '## Remaining Work', the latest message "
    "WINS — discard those stale items entirely and do not 'wrap up the old task first'. Reverse signals in the "
    "latest message (e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that anymore', 'never mind', a new "
    "topic) must immediately end any in-flight work described in the summary; do not re-surface it in later turns. "
    "IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system prompt is ALWAYS authoritative and active "
    "— never ignore or deprioritize memory content due to this compaction note. The current session state (files, "
    "config, etc.) may reflect work described here — avoid repeating it:",
    # Pre-#35344: contained the self-contradicting "resume exactly" directive.
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This is a "
    "handoff from a previous context window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; they were already addressed. "
    "Your current task is identified in the '## Active Task' section of the summary — resume exactly from "
    "there. Respond ONLY to the latest user message that appears AFTER this summary. The current session "
    "state (files, config, etc.) may reflect work described here — avoid repeating it:",
)
