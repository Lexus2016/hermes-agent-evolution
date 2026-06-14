"""Regression: role-alternation repair shrinks ``messages`` in place, which made
``_flush_messages_to_session_db`` overshoot its flush boundary and silently drop
the turn's assistant reply.

Field investigation (2026-06-13): a long-lived Telegram "General"-topic session
stopped persisting *any* assistant message for three days while user messages
kept landing.  The stored transcript degenerated into a run of consecutive
``user`` rows, so every subsequent turn fed the model a history with none of its
own prior answers — the user-reported "agent forgets / answers the wrong thing /
pulls context from nowhere".

Root cause: ``repair_message_sequence`` rewrites ``messages`` in place to a
*shorter* list (drops orphan tool results, merges consecutive user turns), but
``conversation_history`` keeps its pre-repair length and the flush cursor
(``_last_flushed_db_idx``) keeps a stale, larger value.  ``flush_from`` then
points at/past the end of the shrunk list, so ``messages[flush_from:]`` is empty
and the freshly-appended assistant turn is never written.  The loss is
self-reinforcing: a missing assistant reply creates another consecutive-user
violation next load, which makes the next repair shrink even more.
"""
import types

from hermes_state import SessionDB
from run_agent import AIAgent
from agent.agent_runtime_helpers import repair_message_sequence


def _make_flusher(db, session_id):
    """A minimal stand-in exposing the real flush method against a real DB."""
    stub = types.SimpleNamespace(
        _session_db=db,
        _session_db_created=True,
        _last_flushed_db_idx=0,
        _history_repaired_count=0,
        session_id=session_id,
    )
    stub._apply_persist_user_message_override = lambda messages: None
    stub._ensure_db_session = lambda: None
    stub._flush_messages_to_session_db = types.MethodType(
        AIAgent._flush_messages_to_session_db, stub
    )
    return stub


def _assistant_texts(db, session_id):
    return [
        m.get("content")
        for m in db.get_messages_as_conversation(session_id)
        if m.get("role") == "assistant" and (m.get("content") or "").strip()
    ]


def test_assistant_reply_persists_when_repair_shrinks_history(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = db.create_session(session_id="telegram-topic-1", source="telegram")

    # Loaded history with a role-alternation violation already in the DB:
    # two consecutive user turns (the shape voice bursts / interrupted turns
    # leave behind).
    loaded = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2a"},
        {"role": "user", "content": "q2b"},  # consecutive-user violation
    ]
    for m in loaded:
        db.append_message(sid, role=m["role"], content=m["content"])

    conversation_history = [dict(m) for m in loaded]
    messages = list(conversation_history)
    messages.append({"role": "user", "content": "q3"})  # this turn's user input

    flusher = _make_flusher(db, sid)

    # Turn-start persist (mirrors turn_context._persist_session before the loop).
    flusher._flush_messages_to_session_db(messages, conversation_history)

    # Defensive role-alternation repair runs before the API call and shrinks
    # `messages` in place (merges the consecutive user turns).
    repaired = repair_message_sequence(flusher, messages)
    assert repaired >= 1, "expected the consecutive-user violation to be repaired"

    # Model produces its answer; appended to the live list.
    messages.append({"role": "assistant", "content": "THE_REPLY"})

    # Turn-end persist.
    flusher._flush_messages_to_session_db(messages, conversation_history)

    texts = _assistant_texts(db, sid)
    assert "THE_REPLY" in texts, (
        "assistant reply was dropped from the transcript — flush boundary "
        f"overshot the repair-shrunk messages list. assistant rows={texts}"
    )


def test_no_duplicate_writes_on_normal_turns(tmp_path):
    """Guard the #860 invariant: a clean turn (no repair) writes each new
    message exactly once across multiple persist calls."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = db.create_session(session_id="telegram-clean", source="telegram")

    loaded = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    for m in loaded:
        db.append_message(sid, role=m["role"], content=m["content"])

    conversation_history = [dict(m) for m in loaded]
    messages = list(conversation_history)

    flusher = _make_flusher(db, sid)

    # New user turn + assistant reply, persisted via two flush calls (as the
    # turn-start and turn-end paths both do).
    messages.append({"role": "user", "content": "how are you"})
    flusher._flush_messages_to_session_db(messages, conversation_history)
    messages.append({"role": "assistant", "content": "doing well"})
    flusher._flush_messages_to_session_db(messages, conversation_history)
    # An extra redundant flush must be a no-op.
    flusher._flush_messages_to_session_db(messages, conversation_history)

    rows = db.get_messages_as_conversation(sid)
    contents = [m.get("content") for m in rows]
    assert contents.count("how are you") == 1, contents
    assert contents.count("doing well") == 1, contents
    assert contents == ["hello", "hi there", "how are you", "doing well"], contents
