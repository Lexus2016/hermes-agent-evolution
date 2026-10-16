"""Repair durable message sequences while preserving turn identities and origins."""
from __future__ import annotations
from typing import Any, Optional
from agent.message_sanitization import tool_call_id_variants, tool_result_id_variants
from agent.message_metadata import (TOOL_CALL_UIDS, MERGED_TURN_PREFIX, merge_tool_call_uids,
    per_occurrence_tool_call_uids, record_absorbed_message)
from agent.prompt_builder import STEER_DISPLAY_KIND
from agent.turn_context import drop_stale_api_content

def _is_codex_interim(m: dict) -> bool:
    """Codex Responses interim turn: carries its own continuation state, replayed verbatim."""
    return bool(
        m.get("codex_reasoning_items")
        or m.get("codex_message_items")
        or m.get("finish_reason") == "incomplete"
    )

def _merge_assistant_into(prev: dict, msg: dict) -> bool:
    """Fold consecutive assistant *msg* into *prev* (union tool_calls, concat text). Returns whether *msg*'s
    text survives: multimodal (list) content is never joined."""
    from agent.context_compressor import _DB_PERSISTED_MARKER

    prev_calls = list(prev.get("tool_calls") or [])
    new_calls = list(msg.get("tool_calls") or [])
    calls_changed = False
    if new_calls:
        prev["tool_calls"] = prev_calls + new_calls
        # The absorbed turn's calls keep the per-occurrence ids they were persisted with.
        if isinstance(extra := msg.get(TOOL_CALL_UIDS), dict) and extra:
            prev[TOOL_CALL_UIDS] = merge_tool_call_uids(
                per_occurrence_tool_call_uids(
                    own if isinstance(own := prev.get(TOOL_CALL_UIDS), dict) else {}, prev_calls),
                per_occurrence_tool_call_uids(extra, new_calls))
        calls_changed = True
    elif prev_calls:
        prev["tool_calls"] = prev_calls
    else:
        # Drop a stale ``tool_calls: []`` at the source: strict providers (DeepSeek v4, Kimi) 400 on
        # it and it persists into replayed history.
        # Neither turn carries tool calls, but the surviving turn may still carry a stale ``tool_calls: []``
        # from the earlier message. An empty array is semantically "no tool calls", yet strict
        # OpenAI-compatible providers (DeepSeek v4, Moonshot/Kimi) reject it with HTTP 400 ("Invalid
        # 'messages[N].tool_calls': empty array..."). Drop the key HERE, at the source:
        # ``sanitize_api_messages`` only fixes the per-call wire copy, so a ``[]`` left on the repaired turn
        # survives in the live/persisted trajectory returned to callers (gateway/WebUI transcripts, session
        # resume, subagents, cron) and is replayed on the next turn — which is how #58755 kept reproducing
        # after the chokepoint fix (#77921). Popping is non-destructive: an empty array carries no
        # information.
        calls_changed = "tool_calls" in prev
        prev.pop("tool_calls", None)
    # Concatenate plain-text content only; leave multimodal (list) content alone.
    prev_content = prev.get("content")
    new_content = msg.get("content")
    content_rewritten = False
    text_kept = not new_content  # nothing to lose
    if isinstance(prev_content, str) and isinstance(new_content, str):
        text_kept = True
        joined = "\n".join(p for p in (prev_content.strip(), new_content.strip()) if p)
        prev["content"] = joined
        # A falsy new_content leaves ``joined`` == prev_content; that is not a rewrite.
        # "") strips to nothing and ``joined`` collapses back to ``prev_content`` unchanged -- that must NOT
        # count as a rewrite (wz-heng, #78063 review).
        content_rewritten = joined != prev_content
    elif not prev_content and new_content is not None:
        prev["content"] = new_content
        content_rewritten = new_content != prev_content
        text_kept = True
    # Carry reasoning_content from the later turn only if the earlier lacks it (strict thinking
    # providers need one on the merged tool-call turn).
    reasoning_carried = False
    if not prev.get("reasoning_content") and msg.get("reasoning_content"):
        prev["reasoning_content"] = msg["reasoning_content"]
        reasoning_carried = True
    # A stale ``api_content`` sidecar overrides ``content`` at API-build time and would replay
    # pre-merge bytes; drop it only when content actually changed.
    # ``prev`` may carry an ``api_content`` sidecar (the exact bytes previously sent to the API, e.g. a
    # sanitize-divergence stamp — see ``_flush_messages_to_session_db``) from BEFORE this merge. The sidecar
    # takes priority over ``content`` at API-build time (``conversation_loop``'s ``api_messages`` build
    # substitutes it back in for role ``assistant``), so leaving it in place while ``prev["content"]``
    # changes would silently replay the pre-merge bytes and discard everything this merge just concatenated
    # on — the same stale-field-survives-the-merge shape as the ``tool_calls`` gap above, just for a
    # different field. Only drop it when the merge actually changed the resulting value (e.g. the later
    # turn's content is ``None``, or either side is multimodal/list — both branches skip the reassignment
    # and ``prev["content"]`` is untouched; a falsy ``new_content`` that strips to nothing also leaves
    # ``joined`` equal to the original ``prev_content``): in those cases the sidecar is still the exact
    # bytes previously sent for the UNCHANGED content, and dropping it would break the prompt-cache replay
    # invariant for no reason (wz-heng, #78063 review).
    if content_rewritten:
        drop_stale_api_content(prev)
    # The persist marker asserts the whole row is durable (content, tool_calls, reasoning sidecar), so
    # any merged field stales it; pop it or the flush scan identity-skips the merged dict and the DB
    # keeps the pre-merge row. The caller recomputes the flush cursor for the surviving sequence.
    if content_rewritten or calls_changed or reasoning_carried:
        prev.pop(_DB_PERSISTED_MARKER, None)
    return text_kept

def _remember_absorbed_row(survivor: dict[str, Any], dropped: dict[str, Any], *, folded: bool) -> None:
    """Retire *dropped*'s row ids onto *survivor*; record its uid as a merge witness only when *folded* (its
    text survives). An empty incoming turn still merges; stamping an empty list would change a message that
    absorbed nothing. A dropped id that equals the survivor's own live id (the display-marker merge adopts
    the plain row's id, #94486) is not an absorbed row: the survivor IS that row."""
    from agent.conversation_compression_archive import OWN_ROW, RETIRED_DURABLE_ROWS, UNNAMED_DURABLE_ROWS

    own_id = survivor.get("_row_id")
    ids = []
    row_id = dropped.get("_row_id")
    if isinstance(row_id, int) and not isinstance(row_id, bool) and row_id > 0 and row_id != own_id:
        ids.append(row_id)
    for older in dropped.get("_absorbed_row_ids") or ():
        if isinstance(older, int) and not isinstance(older, bool) and older > 0 and older not in ids:
            ids.append(older)
    if ids:
        absorbed = survivor.setdefault("_absorbed_row_ids", [])
        for row_id in ids:
            if row_id not in absorbed:
                absorbed.append(row_id)
    # The rows the retired dict counted are behind the survivor now.
    if dropped.get(UNNAMED_DURABLE_ROWS):
        survivor[UNNAMED_DURABLE_ROWS] = int(survivor.get(UNNAMED_DURABLE_ROWS) or 0) + int(dropped[UNNAMED_DURABLE_ROWS])
    # Its own row is behind the survivor now too.
    if dropped.get(RETIRED_DURABLE_ROWS):
        survivor.setdefault(RETIRED_DURABLE_ROWS, []).extend(
            {k: v for k, v in row.items() if k != OWN_ROW} for row in dropped[RETIRED_DURABLE_ROWS])
    # The uid witness claims the dropped dict's TEXT lives on in the survivor: only a fold earns it. A
    # superseded row (``folded=False``) is retired like any absorbed row but its content is discarded.
    if folded:
        record_absorbed_message(survivor, dropped)

def _count_unnamed_row(survivor: dict[str, Any], retired: dict[str, Any]) -> None:
    """On a reload without row ids nothing names *retired*'s durable row once the repair takes the dict
    out of the list, so *survivor* counts it. A dict that counts rows was loaded too: a merge may have
    popped its marker since. Call before the merge rewrites the survivor: *retired*'s own fields are
    recorded so the commit can name its row."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.conversation_compression_archive import (
        OWN_ROW, RETIRED_DURABLE_ROWS, UNNAMED_DURABLE_ROWS, retired_row_payload)

    def loaded(message: dict[str, Any]) -> bool:
        return bool(message.get(_DB_PERSISTED_MARKER) or message.get(UNNAMED_DURABLE_ROWS))

    if loaded(survivor) and loaded(retired) and not isinstance(retired.get("_row_id"), int):
        survivor[UNNAMED_DURABLE_ROWS] = int(survivor.get(UNNAMED_DURABLE_ROWS) or 0) + 1
        # A previously folded dict already records its original row. The transfer in
        # _remember_absorbed_row retires that record; its synthetic text/calls never existed in storage.
        if not any(row.get(OWN_ROW) for row in retired.get(RETIRED_DURABLE_ROWS) or ()):
            survivor.setdefault(RETIRED_DURABLE_ROWS, []).append(retired_row_payload(retired))

def _remember_own_row(survivor: dict[str, Any]) -> None:
    """An assistant fold rewrites *survivor*'s text, so on a reload without row ids its own row no longer
    matches it by content. Record its loaded fields first, once."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.conversation_compression_archive import OWN_ROW, RETIRED_DURABLE_ROWS, retired_row_payload

    recorded = survivor.get(RETIRED_DURABLE_ROWS) or ()
    if (survivor.get(_DB_PERSISTED_MARKER) and not isinstance(survivor.get("_row_id"), int)
            and not any(isinstance(row, dict) and row.get(OWN_ROW) for row in recorded)):
        survivor.setdefault(RETIRED_DURABLE_ROWS, []).append({**retired_row_payload(survivor), OWN_ROW: True})

def _retire_dropped_row(kept: list[dict], dropped: dict[str, Any], leading: list[dict]) -> None:
    """A durable row the repair drops was still handed to the caller, so the survivor before it stands
    for it. Left unnamed, an in-place compaction takes it for a row another surface appended and
    re-sequences it behind the running turn. A drop with no survivor before it waits in *leading*."""
    if not kept:
        leading.append(dropped)
    elif isinstance(kept[-1], dict):
        _count_unnamed_row(kept[-1], dropped)
        _remember_absorbed_row(kept[-1], dropped, folded=False)

def _retire_leading_drops(kept: list[dict], leading: list[dict]) -> None:
    """Rows dropped ahead of the first survivor sit right before its own row, so it stands for them."""
    for dropped in leading if kept and isinstance(kept[0], dict) else ():
        _count_unnamed_row(kept[0], dropped)
        _remember_absorbed_row(kept[0], dropped, folded=False)

def _merge_consecutive_assistants(messages: list[dict]) -> tuple[list[dict], int]:
    """Pass 0: merge consecutive assistant turns (codex interims exempt)."""
    repairs = 0
    collapsed: list[dict] = []
    for msg in messages:
        prev = collapsed[-1] if collapsed and isinstance(collapsed[-1], dict) else None
        if (
            prev is not None and prev.get("role") == "assistant"
            and isinstance(msg, dict) and msg.get("role") == "assistant"
            and not _is_codex_interim(msg) and not _is_codex_interim(prev)
        ):
            # A provisional verification candidate is superseded, not unioned.
            if prev.get("finish_reason") in {"verification_required", "verify_hook_continue"}:
                _count_unnamed_row(msg, prev)
                _remember_absorbed_row(msg, prev, folded=False)
                collapsed[-1] = msg
            else:
                _count_unnamed_row(prev, msg)
                _remember_own_row(prev)
                _remember_absorbed_row(prev, msg, folded=_merge_assistant_into(prev, msg))
            repairs += 1
            continue
        collapsed.append(msg)
    return collapsed, repairs

def _drop_stray_tool_results(messages: list[dict]) -> tuple[list[dict], int]:
    """Pass 1: drop tool results not following a known assistant tool call. Consumes the whole
    alias group (call_id/id/response_item_id/composite) so a duplicate keyed on a sibling
    alias is not replayed to strict providers."""
    repairs = 0
    known_tool_ids: dict[str, int] = {}  # alias -> group id; reset by assistant/user turns
    # Pass 1: drop stray tool messages that don't follow a known assistant tool call. A Responses call can
    # have several equivalent spellings (call_id, id, response_item_id, or a composite ``call|item`` id), so
    # consume the whole alias group when one spelling is matched. Alias expansion lives in
    # ``agent.message_sanitization.tool_call_id_variants`` / ``tool_result_id_variants`` (single policy
    # owner) — which also handles SDK tool_call objects, preserving the #91768 dict-or-object tolerance.
    matched_tool_groups: set = set()
    next_tool_group = 0
    filtered: list[dict] = []
    leading: list[dict] = []
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else None
        if role in ("assistant", "user"):
            # An assistant turn starts a new tool-result run; a user turn closes it (later tool
            # messages are orphans).
            known_tool_ids = {}
            matched_tool_groups = set()
            for tc in (msg.get("tool_calls") or []) if role == "assistant" else ():
                variants = tool_call_id_variants(tc)
                if variants:
                    for tc_id in variants:
                        known_tool_ids.setdefault(tc_id, next_tool_group)
                    next_tool_group += 1
        elif role == "tool":
            result_variants = tool_result_id_variants(msg.get("tool_call_id"))
            candidate_groups = {
                known_tool_ids[tc_id] for tc_id in result_variants
                if tc_id in known_tool_ids and known_tool_ids[tc_id] not in matched_tool_groups
            }
            if result_variants and not candidate_groups:
                _retire_dropped_row(filtered, msg, leading)
                repairs += 1
                continue
            if candidate_groups:
                matched_tool_groups.add(min(candidate_groups))
        filtered.append(msg)
    _retire_leading_drops(filtered, leading)
    return filtered, repairs

def _prune_unanswered_tool_calls(messages: list[dict]) -> tuple[list[dict], int]:
    """Pass 2: prune tool_calls not answered in the IMMEDIATELY following tool run (a displaced
    result masks the per-call stub pass and strict providers 400). Payload-empty turns are
    dropped; codex interims exempt."""
    from agent.context_compressor import _DB_PERSISTED_MARKER

    repairs = 0
    pruned: list[dict] = []
    leading: list[dict] = []
    for i, msg in enumerate(messages):
        if not (
            isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls")
            and not _is_codex_interim(msg)
        ):
            pruned.append(msg)
            continue
        answered: set = set()
        for follower in messages[i + 1:]:
            if not (isinstance(follower, dict) and follower.get("role") == "tool"):
                break
            tid = (follower.get("tool_call_id") or "").strip()
            if tid:
                answered.update(tool_result_id_variants(tid))
        kept_calls = [tc for tc in msg["tool_calls"] if tool_call_id_variants(tc) & answered]
        if len(kept_calls) != len(msg["tool_calls"]):
            repairs += 1
            if not kept_calls and not _msg_has_payload({k: v for k, v in msg.items() if k != "tool_calls"}):
                # Pruned calls were the only payload; drop the turn (empty assistant messages 400).
                _retire_dropped_row(pruned, msg, leading)
                continue
            if kept_calls:
                msg["tool_calls"] = kept_calls
            else:
                msg.pop("tool_calls", None)
            # tool_calls is part of the persisted row; rewriting it on a stamped dict stales the
            # marker, so pop it or the flush scan skips the dict and the DB keeps the old calls.
            msg.pop(_DB_PERSISTED_MARKER, None)
        pruned.append(msg)
    _retire_leading_drops(pruned, leading)
    return pruned, repairs

def _merge_consecutive_users(messages: list[dict]) -> tuple[list[dict], int]:
    """Pass 3: merge consecutive plain-text user messages (no user input lost)."""
    from agent.context_compressor import _DB_PERSISTED_MARKER, split_user_originated_turn
    from agent.conversation_compression_archive import MERGED_DURABLE_ROWS
    from hermes_state import SessionDB

    def _plain_text(content: Any) -> bool:
        # never rewrite the persisted row around undecodable content
        return isinstance(content, str) and not content.startswith(SessionDB._CONTENT_JSON_PREFIX)

    repairs = 0
    merged: list[dict] = []
    for msg in messages:
        prev = merged[-1] if merged and isinstance(merged[-1], dict) else None
        if (
            prev is not None and prev.get("role") == "user"
            and isinstance(msg, dict) and msg.get("role") == "user"
            # A summary carrier followed by a new user row is a deliberate durable shape after
            # retry/rewind; never mutate the persisted carrier (sanitizers merge copies later).
            and split_user_originated_turn(prev)[0] is None
            # A /steer row that ended the previous run is already persisted; merging the next
            # prompt into it would rewrite it in place and re-break replay parity.
            and prev.get("display_kind") != STEER_DISPLAY_KIND
            # Only merge plain-text content; leave multimodal (list or undecodable sentinel) content alone.
            and _plain_text(prev.get("content", "")) and _plain_text(msg.get("content", ""))
        ):
            prev_content, new_content = prev.get("content", ""), msg.get("content", "")
            merged_content = (
                (prev_content + "\n\n" + new_content) if prev_content and new_content else (prev_content or new_content)
            )
            had_api_sidecar = "api_content" in prev
            # Read before the marker is popped below. An unpersisted turn folded in ends the claim:
            # the dict no longer stands for durable rows only.
            if (prev.get(_DB_PERSISTED_MARKER) or prev.get(MERGED_DURABLE_ROWS)) and msg.get(_DB_PERSISTED_MARKER):
                prev[MERGED_DURABLE_ROWS] = int(prev.get(MERGED_DURABLE_ROWS) or 1) + 1
            else:
                prev.pop(MERGED_DURABLE_ROWS, None)
            prev["content"] = merged_content
            # The clean-text persist override must replace only the absorbed turn, never the
            # unanswered text before it; kept across replay passes (an empty turn absorbs too).
            if prev_content:
                prev[MERGED_TURN_PREFIX] = prev_content
            # Merged content invalidates the api_content sidecar; drop it so replay cannot use stale bytes.
            drop_stale_api_content(prev)
            # A display-marker row (e.g. a model-switch marker persisted as role=user on
            # purpose, #48338) merging with a plain user row must not bury the plain row's
            # addressable identity: keeping the marker's display_kind hides the merged pair
            # from every user-turn index used for rewind/submit addressing (display rows are
            # excluded), so the plain row's durable id becomes unresolvable and the client's
            # next prompt.submit fails closed with the input silently dropped (#94486). Keep
            # the pair addressable instead: drop the display classification and carry the
            # plain row's id, retiring the marker's own id onto the absorbed list. display_kind
            # never reaches providers (stripped from every outgoing copy), so the merged
            # turn's wire payload is unchanged. Deliberate scope: when BOTH rows carry
            # display_kind (two consecutive model-switch markers) the pair keeps the first
            # marker's classification and id — no plain row is swallowed there, so no
            # addressable turn is lost.
            if prev.get("display_kind") and not msg.get("display_kind"):
                marker_row_id = prev.get("_row_id")
                prev.pop("display_kind", None)
                if msg.get("_row_id") is not None:
                    if isinstance(marker_row_id, int) and not isinstance(marker_row_id, bool):
                        absorbed_ids = prev.setdefault("_absorbed_row_ids", [])
                        if marker_row_id not in absorbed_ids:
                            absorbed_ids.append(marker_row_id)
                    prev["_row_id"] = msg["_row_id"]
                # display_kind is part of the persisted row; reclassifying stales it even
                # when the merged bytes reproduce the persisted content (empty absorb).
                prev.pop(_DB_PERSISTED_MARKER, None)
            # Pop the persist marker only when the durable row actually changed: a merge that
            # reproduces the persisted bytes (e.g. an empty incoming turn) keeps its stamp.
            if merged_content != prev_content or had_api_sidecar:
                prev.pop(_DB_PERSISTED_MARKER, None)
            _remember_absorbed_row(prev, msg, folded=True)
            repairs += 1
            continue
        merged.append(msg)
    return merged, repairs

_SEQUENCE_REPAIR_PASSES = (
    _merge_consecutive_assistants, _drop_stray_tool_results, _prune_unanswered_tool_calls,
    _merge_consecutive_users,
)

def _normalize_sentinel_encoded_content(messages: list[dict]) -> None:
    """Decode any sentinel-encoded row content in place before the alternation passes run, so an
    image-bearing turn is never merged as text (#125299). A multimodal turn can re-enter the working set
    as its ``\\x00json:[…]`` string (e.g. after a proactive prune re-inserts history, #124102). A body
    that no longer parses stays a sentinel string; ``_merge_consecutive_users`` refuses to weld it."""
    from hermes_state import SessionDB  # lazy: the persistence layer owns the sentinel codec

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        decoded = SessionDB._decode_content(content)
        if decoded is not content:
            msg["content"] = decoded

def repair_message_sequence(agent, messages: list[dict]) -> int:
    """Collapse malformed role-alternation left in the live history; returns repair count.
    Providers require strict alternation after the system message (violations: silent empty
    responses or 400s); this is the pre-call belt for host-fed, resumed or replayed histories.
    Passes in order: decode any sentinel-encoded multimodal rows (so an image is not merged as text);
    merge consecutive assistant turns (BEFORE orphan detection so the merged tool_call-id union is
    known); drop stray tool results; prune unanswered tool_calls; merge consecutive user turns. A user
    turn directly after an assistant turn is valid and left alone.
    """
    if not messages:
        return 0
    _normalize_sentinel_encoded_content(messages)
    repairs = 0
    current = messages
    for repair_pass in _SEQUENCE_REPAIR_PASSES:
        current, made = repair_pass(current)
        repairs += made
    if repairs > 0:
        # Rewrite in place so persistence/return value/DB flush see the repaired sequence.
        messages[:] = current
    return repairs

def repair_message_sequence_with_cursor(agent, messages: list[dict]) -> int:
    """Run :func:`repair_message_sequence` and keep ``_last_flushed_db_idx`` consistent. Repair
    shrinks the list in place; counting identity-preserved survivors of the flushed prefix gives
    the exact new cursor, whereas a ``min()`` clamp would skip unflushed rows (used only without a snapshot)."""
    from agent.context_compressor import _DB_PERSISTED_MARKER

    flush_cursor = getattr(agent, "_last_flushed_db_idx", None)
    flushed_ids = {id(m) for m in messages[:flush_cursor]} if isinstance(flush_cursor, int) and flush_cursor > 0 else None
    stamped_ids = {id(m) for m in messages if isinstance(m, dict) and m.get(_DB_PERSISTED_MARKER)}
    repairs = repair_message_sequence(agent, messages)
    if repairs > 0:
        # A stamped survivor that lost its marker was mutated in place by a merge/prune pass; the
        # bounded flush scan would skip past it inside the identity-matched prefix, so force a
        # full re-scan (same contract as the compressor's _flush_scan_cursor_invalidated).
        if stamped_ids and any(
            id(m) in stamped_ids and not m.get(_DB_PERSISTED_MARKER) for m in messages
        ):
            agent._db_flush_scan_prefix = None
        if hasattr(agent, "_last_flushed_db_idx"):
            if flushed_ids is not None:
                agent._last_flushed_db_idx = sum(1 for m in messages if id(m) in flushed_ids)
            else:
                agent._last_flushed_db_idx = min(agent._last_flushed_db_idx, len(messages))
    return repairs

def _content_has_payload(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return content not in (None, "")
    # Any typed block counts, as long as a text block is not itself blank.
    return any(
        (block.get("type") != "text" or (isinstance(block.get("text"), str) and block["text"].strip()))
        if isinstance(block, dict) else bool(block)
        for block in content
    )

def _msg_has_payload(msg: dict[str, Any]) -> bool:
    """True if ``msg`` carries anything the API treats as non-empty content (text, multimodal
    blocks, tool_calls, reasoning). Role-agnostic counterpart of ``AIAgent._is_thinking_only_assistant``.
    Codex Responses item carriers persist with content:"" by design (text lives in codex_*_items
    and is replayed); treating them as payload keeps the repair from rewriting a designed-empty turn."""
    return _content_has_payload(msg.get("content")) or bool(
        msg.get("tool_calls")
        or (isinstance(msg.get("reasoning_content"), str) and msg["reasoning_content"].strip())
        or msg.get("reasoning")
        or msg.get("reasoning_details")
        or msg.get("codex_message_items")
        or msg.get("codex_reasoning_items")
    )
