"""Tests for scripts/evolution_experience_harvest.py — deterministic harvest."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_experience_harvest as eh  # noqa: E402
from agent import experience_bank as eb  # noqa: E402
from agent.experience_bank import get_harvest_state, iter_entries  # noqa: E402
from hermes_state import SessionDB  # noqa: E402

# Fixed clock: sessions are aged 1h behind "now" so they clear the 30-minute
# possibly-active grace window with room to spare.
NOW = 1_800_000_000.0
OLD = NOW - 3600.0

UNIQUE_USER_STRING = "zqxjvk-unique-user-string-9f8e7d"


def _make_db(tmp_path):
    db_path = tmp_path / "state.db"
    return SessionDB(db_path=db_path), db_path


def _add_session(
    db,
    sid,
    messages,
    *,
    source="cli",
    model="test-model",
    ended=True,
    ts=OLD,
):
    """Insert a session with messages, aged to *ts* (epoch seconds).

    ``create_session``/``end_session`` stamp real wall-clock time, so the row
    is back-dated with a direct UPDATE afterwards — the same approach
    existing SessionDB tests use for compression-chain fixtures.
    """
    db.create_session(sid, source, model=model)
    for m in messages:
        db.append_message(
            sid,
            m["role"],
            content=m.get("content"),
            tool_name=m.get("tool_name"),
            timestamp=m.get("ts", ts),
        )
    if ended:
        db.end_session(sid, "cli_close")
    db._conn.execute(
        "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
        (ts, ts if ended else None, sid),
    )


def _user(text="do the thing"):
    return {"role": "user", "content": text}


def _assistant(text="Done."):
    return {"role": "assistant", "content": text}


def _tool_error(tool, content):
    return {"role": "tool", "tool_name": tool, "content": content}


def _tool_ok(tool, content):
    return {"role": "tool", "tool_name": tool, "content": content}


def _harvest(db, db_path, now=NOW):
    """Close the writer and run one harvest pass against the same file."""
    db.close()
    return eh.run_harvest(db_path=db_path, now=now)


def _entries():
    return list(iter_entries())


class TestSessionSelection:
    def test_active_session_skipped(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        recent = time.time()
        _add_session(
            db, "s-active", [_user(), _assistant()], ended=False, ts=recent
        )
        summary = _harvest(db, db_path, now=time.time())
        assert summary["entries_appended"] == 0
        assert _entries() == []

    def test_dedup_second_run_processes_nothing_new(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])
        first = _harvest(db, db_path)
        assert first["entries_appended"] == 1

        # Second run: the session sits inside the cursor overlap window, so
        # only seen_ids dedups it — entries must not double.
        second = eh.run_harvest(db_path=db_path, now=NOW)
        assert second["sessions_harvested"] == 0
        assert second["entries_appended"] == 0
        assert len(_entries()) == 1

        state = get_harvest_state()
        assert state["cursor_ts"] == OLD
        assert "s-clean" in state["seen_ids"]
        assert state["last_message_id"] > 0
        assert len(state["seen_revisions"]) == 1

    def test_reopened_session_with_new_messages_is_new_revision(self, tmp_path):
        """A resumed and re-ended session is harvested once per completion.

        Повторно відкрита й завершена сесія збирається один раз на редакцію.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-reopened", [_user(), _assistant("First end.")])
        first = _harvest(db, db_path)
        assert first["entries_appended"] == 1

        resumed = SessionDB(db_path=db_path)
        resumed._conn.execute(
            "UPDATE sessions SET ended_at=NULL WHERE id=?", ("s-reopened",)
        )
        resumed.append_message(
            "s-reopened", "user", content="Continue.", timestamp=OLD + 10
        )
        resumed.append_message(
            "s-reopened", "assistant", content="Second end.", timestamp=OLD + 20
        )
        resumed.end_session("s-reopened", "cli_close")
        resumed._conn.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?",
            (OLD + 30, "s-reopened"),
        )

        second = _harvest(resumed, db_path)
        assert second["entries_appended"] == 1
        revisions = _entries()
        assert [entry.session_id for entry in revisions] == [
            "s-reopened",
            "s-reopened",
        ]
        assert revisions[0].stats["source_last_message_id"] < revisions[1].stats[
            "source_last_message_id"
        ]
        assert len(get_harvest_state()["seen_revisions"]) == 2

        third = eh.run_harvest(db_path=db_path, now=NOW)
        assert third["entries_appended"] == 0
        assert len(_entries()) == 2

    def test_late_historical_import_is_found_by_message_id_cursor(self, tmp_path):
        """A newly imported old completion is not hidden by the time cursor.

        Новий історичний імпорт не губиться за часовим курсором.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-current", [_user(), _assistant()], ts=OLD)
        assert _harvest(db, db_path)["entries_appended"] == 1

        imported = SessionDB(db_path=db_path)
        _add_session(
            imported,
            "s-imported",
            [_user("historical"), _assistant("imported")],
            ts=OLD - 3600,
        )
        second = _harvest(imported, db_path)

        assert second["entries_appended"] == 1
        assert {entry.session_id for entry in _entries()} == {
            "s-current",
            "s-imported",
        }
        state = get_harvest_state()
        assert state["cursor_ts"] == OLD
        assert state["last_message_id"] >= max(
            entry.stats["source_last_message_id"] for entry in _entries()
        )

    def test_legacy_state_bootstraps_bounded_message_cursor(self, tmp_path):
        """Old cursor_ts/seen_ids state upgrades without replaying history.

        Старий стан оновлюється без повторного збору всієї історії.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-legacy", [_user(), _assistant()], ts=OLD)
        db.close()
        assert eb.append_entry(
            eb.ExperienceEntry(ts=OLD, session_id="s-legacy", success=True)
        )
        assert eb.set_harvest_state(
            {"cursor_ts": OLD, "seen_ids": ["s-legacy"]}
        )

        upgraded = eh.run_harvest(db_path=db_path, now=NOW)
        assert upgraded["entries_appended"] == 0
        state = get_harvest_state()
        assert state["cursor_ts"] == OLD
        assert state["last_message_id"] > 0
        assert state["seen_ids"] == ["s-legacy"]
        assert len(state["seen_revisions"]) == 1
        assert state["revision_state_version"] == 1

        imported = SessionDB(db_path=db_path)
        _add_session(
            imported,
            "s-after-upgrade",
            [_user(), _assistant()],
            ts=OLD - 3600,
        )
        assert _harvest(imported, db_path)["entries_appended"] == 1
        assert [entry.session_id for entry in _entries()] == [
            "s-legacy",
            "s-after-upgrade",
        ]

    def test_late_import_before_first_upgrade_is_harvested(self, tmp_path):
        """The one-time migration must scan imports predating the upgrade.

        Одноразова міграція має знайти імпорт, зроблений до оновлення.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-known", [_user(), _assistant()], ts=OLD)
        _add_session(
            db,
            "s-imported-before-upgrade",
            [_user(), _assistant()],
            ts=OLD - 3600,
        )
        db.close()
        assert eb.append_entry(
            eb.ExperienceEntry(ts=OLD, session_id="s-known", success=True)
        )
        assert eb.set_harvest_state(
            {"cursor_ts": OLD, "seen_ids": ["s-known"]}
        )

        migrated = eh.run_harvest(db_path=db_path, now=NOW)

        assert migrated["entries_appended"] == 1
        assert [entry.session_id for entry in _entries()] == [
            "s-known",
            "s-imported-before-upgrade",
        ]
        assert get_harvest_state()["revision_state_version"] == 1

        # After migration, SQL is bounded again: the old import is excluded.
        bounded = eh.run_harvest(db_path=db_path, now=NOW)
        assert bounded["sessions_scanned"] == 1
        assert bounded["entries_appended"] == 0

    def test_reopened_revision_before_first_upgrade_is_harvested(self, tmp_path):
        """A bare legacy seen_id must not hide a newer completion revision.

        Старий seen_id не повинен приховати новішу завершену редакцію.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-reopened-before-upgrade", [_user(), _assistant()], ts=OLD)
        db.close()
        assert eb.append_entry(
            eb.ExperienceEntry(
                ts=OLD,
                session_id="s-reopened-before-upgrade",
                success=True,
            )
        )
        assert eb.set_harvest_state(
            {
                "cursor_ts": OLD + 3600,
                "seen_ids": ["s-reopened-before-upgrade"],
            }
        )

        resumed = SessionDB(db_path=db_path)
        resumed._conn.execute(
            "UPDATE sessions SET ended_at=NULL WHERE id=?",
            ("s-reopened-before-upgrade",),
        )
        resumed.append_message(
            "s-reopened-before-upgrade", "user", content="Continue.", timestamp=OLD + 10
        )
        resumed.append_message(
            "s-reopened-before-upgrade",
            "assistant",
            content="Completed again.",
            timestamp=OLD + 20,
        )
        resumed._conn.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?",
            (OLD + 30, "s-reopened-before-upgrade"),
        )

        migrated = _harvest(resumed, db_path)

        assert migrated["entries_appended"] == 1
        assert [entry.ts for entry in _entries()] == [OLD, OLD + 30]
        assert get_harvest_state()["revision_state_version"] == 1

    def test_changed_snapshot_is_not_marked_processed(self, tmp_path, monkeypatch):
        """A session reopened after get_messages remains eligible next run.

        Сесія, відкрита після get_messages, лишається доступною наступного разу.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-race", [_user(), _assistant()])
        db.close()

        original = SessionDB.get_messages
        changed = False

        def reopening_get_messages(self, session_id, *args, **kwargs):
            nonlocal changed
            messages = original(self, session_id, *args, **kwargs)
            if not changed:
                changed = True
                writer = SessionDB(db_path=db_path)
                writer._conn.execute(
                    "UPDATE sessions SET ended_at=NULL WHERE id=?", (session_id,)
                )
                writer.close()
            return messages

        monkeypatch.setattr(SessionDB, "get_messages", reopening_get_messages)
        first = eh.run_harvest(db_path=db_path, now=NOW)
        assert first["entries_appended"] == 0
        assert get_harvest_state().get("seen_revisions", []) == []

        monkeypatch.setattr(SessionDB, "get_messages", original)
        writer = SessionDB(db_path=db_path)
        writer.append_message(
            "s-race", "assistant", content="Ended later.", timestamp=OLD + 1
        )
        writer._conn.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?", (OLD + 2, "s-race")
        )
        second = _harvest(writer, db_path)
        assert second["entries_appended"] == 1

    def test_unfinished_session_is_not_seen_then_harvests_after_end(self, tmp_path):
        """An unfinished row remains eligible after it is later completed.

        Незавершений запис лишається доступним після подальшого завершення.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-resume", [_user()], ended=False)
        first = _harvest(db, db_path)
        assert first["entries_appended"] == 0
        assert "s-resume" not in get_harvest_state().get("seen_ids", [])

        resumed = SessionDB(db_path=db_path)
        resumed.append_message(
            "s-resume", "assistant", content="Completed later.", timestamp=OLD
        )
        resumed.end_session("s-resume", "cli_close")
        resumed._conn.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?", (OLD, "s-resume")
        )
        second = _harvest(resumed, db_path)
        assert second["entries_appended"] == 1
        assert "s-resume" in get_harvest_state()["seen_ids"]

    def test_rows_are_ordered_by_completion_time(self, tmp_path):
        db, _db_path = _make_db(tmp_path)
        _add_session(db, "long", [_user(), _assistant()], ts=100.0)
        _add_session(db, "short", [_user(), _assistant()], ts=200.0)
        db._conn.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?", (300.0, "long")
        )
        db._conn.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?", (250.0, "short")
        )
        assert [row["id"] for row in eh._iter_session_rows(db)] == [
            "short",
            "long",
        ]


class TestVerdicts:
    def test_clean_session_success_low_confidence(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant("All finished.")])
        summary = _harvest(db, db_path)
        assert summary["entries_appended"] == 1
        (entry,) = _entries()
        assert entry.success is True
        assert entry.confidence == "low"
        assert entry.outcome_source == "heuristic:clean_completion"
        assert entry.terminal_reason == "completed"
        assert entry.failure_category is None
        assert entry.primary_dimension is None

    def test_loop_guard_hard_stop_is_strong_failure(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-loopguard",
            [
                _user(),
                _tool_error("terminal", json.dumps({"exit_code": 1, "error": "boom"})),
                _assistant(
                    "[loop-guard] `terminal` has been failing repeatedly — "
                    "3 advisory warnings were ignored across 5 consecutive "
                    "calls with no course-correction."
                ),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.success is False
        assert entry.confidence == "high"
        assert entry.outcome_source == "heuristic:loop_guard_hard_stop"
        assert entry.terminal_reason == "loop_guard_hard_stop"

    def test_apology_without_error_evidence_is_low_confidence(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-crash",
            [
                _user(),
                _assistant(
                    "I apologize, but I encountered an error while processing "
                    "the model response: boom"
                ),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.success is False
        assert entry.confidence == "low"
        assert entry.outcome_source == "heuristic:unhandled_exception"
        assert entry.terminal_reason == "unhandled_exception"

    def test_apology_with_unrecovered_error_is_strong_failure(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-crash-evidence",
            [
                _user(),
                _tool_error("terminal", {"exit_code": 1, "error": "boom"}),
                _assistant(
                    "I apologize, but I encountered an error while processing "
                    "the model response: boom"
                ),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.success is False
        assert entry.confidence == "high"

    def test_mid_conversation_max_iteration_quote_is_not_failure(self):
        verdict = eh.analyze_session(
            [
                _user(eh._MAX_ITER_MARKER),
                _assistant("That text is only a quote."),
                _user("Continue normally."),
                _assistant("Done."),
            ],
            ended=True,
        )
        assert verdict["success"] is True
        assert verdict["outcome_source"] == "heuristic:clean_completion"

    def test_terminal_max_iteration_marker_is_strong_failure(self):
        verdict = eh.analyze_session(
            [_user(eh._MAX_ITER_MARKER), _assistant("Partial summary.")],
            ended=True,
        )
        assert verdict["success"] is False
        assert verdict["confidence"] == "high"
        assert verdict["outcome_source"] == "heuristic:max_iterations_exhausted"

    def test_recovered_only_errors_are_never_a_failure(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-recovered",
            [
                _user(),
                _tool_error("terminal", json.dumps({"exit_code": 1, "error": "boom"})),
                _tool_ok("terminal", json.dumps({"exit_code": 0, "output": "ok"})),
                _assistant("Fixed it."),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.success is not False
        assert entry.success is True  # recovered + clean end = clean completion
        assert "recovered=1" in entry.analysis
        assert "unrecovered=0" in entry.analysis


class TestDimensionAttribution:
    def test_named_tool_timeout_maps_to_tool(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-timeout",
            [
                _user(),
                _tool_error(
                    "terminal",
                    json.dumps({"exit_code": 124, "error": "command timed out"}),
                ),
                _assistant("I could not finish in time."),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.failure_category == "timeout"
        assert entry.tool == "terminal"
        assert entry.primary_dimension == "tool"
        assert entry.secondary_dimensions == ["generation", "orchestration"]
        assert entry.success is None  # unrecovered errors, no strong signal

    def test_unknown_tool_timeout_stays_honest_null(self):
        verdict = eh.analyze_session(
            [
                _user(),
                _tool_error(
                    "",
                    json.dumps({"exit_code": 124, "error": "command timed out"}),
                ),
                _assistant("I could not finish in time."),
            ],
            ended=True,
        )
        assert verdict["failure_category"] == "timeout"
        assert verdict["tool"] == "unknown"
        assert verdict["primary_dimension"] is None
        assert verdict["secondary_dimensions"] == [
            "tool",
            "generation",
            "orchestration",
        ]

    def test_not_found_maps_to_context(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-notfound",
            [
                _user(),
                _tool_error(
                    "read_file",
                    json.dumps({"success": False, "error": "File not found: /x/y.py"}),
                ),
                _assistant("The file was missing."),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.failure_category == "not_found"
        assert entry.primary_dimension == "context"
        assert entry.secondary_dimensions == []

    def test_analysis_is_controlled_vocabulary_only(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-vocab",
            [
                _user(UNIQUE_USER_STRING),
                _tool_error(
                    "terminal",
                    json.dumps({"exit_code": 1, "error": UNIQUE_USER_STRING}),
                ),
                _assistant(f"reply mentioning {UNIQUE_USER_STRING}"),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert UNIQUE_USER_STRING not in entry.analysis
        # The whole analysis string is built from scrubbed tokens + counts.
        import re

        assert re.fullmatch(
            r"tool=[a-z0-9_]+ category=[a-z_]+ unrecovered=\d+ recovered=\d+",
            entry.analysis,
        )

    def test_structured_tool_content_is_classified(self):
        verdict = eh.analyze_session(
            [
                _user(),
                _tool_error(
                    "read_file",
                    {"success": False, "error": "File not found: /x/y.py"},
                ),
                _assistant("The file was missing."),
            ],
            ended=True,
        )
        assert verdict["has_unrecovered"] is True
        assert verdict["failure_category"] == "not_found"
        assert verdict["primary_dimension"] == "context"


class TestDurability:
    def test_entry_uses_session_end_time(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-time", [_user(), _assistant()], ts=OLD)
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.ts == OLD

    def test_failed_append_is_not_counted_or_marked_seen(
        self, tmp_path, monkeypatch
    ):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-write-fail", [_user(), _assistant()])
        db.close()
        monkeypatch.setattr(eh, "append_entry", lambda _entry: False)

        summary = eh.run_harvest(db_path=db_path, now=NOW)

        assert summary["entries_appended"] == 0
        assert summary["write_failures"] == 1
        assert "s-write-fail" not in get_harvest_state().get("seen_ids", [])

    def test_existing_entry_heals_failed_state_write_without_duplicate(
        self, tmp_path, monkeypatch
    ):
        """The append log is a second dedup source when state persistence fails.

        Журнал записів є другим джерелом усунення дублів при помилці стану.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-state-fail", [_user(), _assistant()])
        db.close()
        original = eh.set_harvest_state
        monkeypatch.setattr(eh, "set_harvest_state", lambda _state: False)
        first = eh.run_harvest(db_path=db_path, now=NOW)
        assert first["entries_appended"] == 1
        assert first["state_saved"] is False
        assert "revision_state_version" not in get_harvest_state()

        monkeypatch.setattr(eh, "set_harvest_state", original)
        second = eh.run_harvest(db_path=db_path, now=NOW)
        assert second["entries_appended"] == 0
        assert len(_entries()) == 1
        assert "s-state-fail" in get_harvest_state()["seen_ids"]
        assert get_harvest_state()["revision_state_version"] == 1

    def test_failed_append_keeps_migration_incomplete_and_retry_heals(
        self, tmp_path, monkeypatch
    ):
        """A partial migration is replayed without duplicating durable entries.

        Часткова міграція повторюється без дублювання збережених записів.
        """
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-first", [_user(), _assistant()], ts=OLD)
        _add_session(db, "s-second", [_user(), _assistant()], ts=OLD + 1)
        db.close()
        assert eb.set_harvest_state({"cursor_ts": OLD + 100, "seen_ids": []})

        original = eh.append_entry
        calls = 0

        def fail_second(entry):
            nonlocal calls
            calls += 1
            return original(entry) if calls == 1 else False

        monkeypatch.setattr(eh, "append_entry", fail_second)
        first = eh.run_harvest(db_path=db_path, now=NOW)

        assert first["entries_appended"] == 1
        assert first["write_failures"] == 1
        assert "revision_state_version" not in get_harvest_state()
        assert [entry.session_id for entry in _entries()] == ["s-first"]

        monkeypatch.setattr(eh, "append_entry", original)
        second = eh.run_harvest(db_path=db_path, now=NOW)

        assert second["entries_appended"] == 1
        assert [entry.session_id for entry in _entries()] == [
            "s-first",
            "s-second",
        ]
        assert get_harvest_state()["revision_state_version"] == 1

    def test_corrupt_stored_message_id_is_treated_as_legacy(self, tmp_path):
        """Malformed nested revision metadata must not crash the next harvest."""
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-bad-revision", [_user(), _assistant()], ts=OLD)
        db.close()
        assert eb.append_entry(
            eb.ExperienceEntry(
                ts=OLD,
                session_id="s-bad-revision",
                success=True,
                stats={"source_last_message_id": "not-an-integer"},
            )
        )

        summary = eh.run_harvest(db_path=db_path, now=NOW)

        assert summary["entries_appended"] == 0
        assert len(_entries()) == 1
        assert "s-bad-revision" in get_harvest_state()["seen_ids"]


class TestDistillation:
    @pytest.fixture()
    def fake_distill(self, monkeypatch):
        """Fake sibling distiller; records calls. Returns the recorder."""
        import types

        calls = []

        def run_distillation(now=None, *, acquire_lock=True):
            calls.append((now, acquire_lock))
            return {"patterns": 2}

        module = types.ModuleType("evolution_experience_distill")
        module.run_distillation = run_distillation
        monkeypatch.setitem(sys.modules, "evolution_experience_distill", module)
        return calls

    def test_distill_invoked_when_entries_appended(self, tmp_path, fake_distill):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])
        summary = _harvest(db, db_path)
        assert summary["entries_appended"] == 1
        assert len(fake_distill) == 1
        assert fake_distill[0][1] is False
        assert summary["distill"] == {"patterns": 2}

    def test_distill_not_invoked_when_nothing_appended(self, tmp_path, fake_distill):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-active", [_user()], ended=False, ts=time.time())
        summary = _harvest(db, db_path, now=time.time())
        assert summary["entries_appended"] == 0
        assert summary["distill"] is None
        assert fake_distill == []

    def test_prior_distill_failure_retries_without_new_append(
        self, tmp_path, fake_distill
    ):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])

        mod = sys.modules["evolution_experience_distill"]
        successful = mod.run_distillation

        def raising(now=None, *, acquire_lock=True):
            raise RuntimeError("first distill failed")

        mod.run_distillation = raising
        first = _harvest(db, db_path)
        assert first["entries_appended"] == 1
        assert first["distill"] is None

        mod.run_distillation = successful
        second = eh.run_harvest(db_path=db_path, now=NOW)
        assert second["entries_appended"] == 0
        assert second["distill"] == {"patterns": 2}
        assert len(fake_distill) == 1

    def test_harvest_survives_distill_raising(self, tmp_path, fake_distill, capsys):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])

        import types

        mod = sys.modules["evolution_experience_distill"]
        original = mod.run_distillation

        def raising(now=None, *, acquire_lock=True):
            raise RuntimeError("distill boom")

        mod.run_distillation = raising
        try:
            summary = _harvest(db, db_path)
        finally:
            mod.run_distillation = original
        assert summary["entries_appended"] == 1
        assert summary["distill"] is None
        assert "distillation failed" in capsys.readouterr().err


class TestLock:
    def test_second_concurrent_instance_exits_cleanly(self, tmp_path, capsys):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])
        db.close()

        with eb.experience_lock() as acquired:
            assert acquired is True
            rc = eh.main(["evolution_experience_harvest.py"])
        assert rc == 0
        assert "another bank operation is running" in capsys.readouterr().out
        assert _entries() == []


class TestImportFallback:
    """The cron installs this script into HERMES_HOME/scripts where the
    ``evolution`` namespace package is absent.  The import must degrade
    gracefully (issue #1304) rather than crashing every tick with
    ``ModuleNotFoundError``.
    """

    def _reload_with_evolution_blocked(self, tmp_path, monkeypatch):
        """Reload ``evolution_experience_harvest`` with the ``evolution``
        import blocked, returning the reloaded module with the fallback
        ``ErrorClassifier`` active.

        Patches ``builtins.__import__`` to raise ``ImportError`` for the
        ``evolution`` package — the exact failure the installed cron sees —
        then reloads.  A finalizer restores the real ``ErrorClassifier`` by
        reloading once more with the import unblocked, so sibling tests are
        unaffected.
        """
        import builtins
        import importlib
        from unittest import mock

        real_import = builtins.__import__

        def blocking_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "evolution" or name.startswith("evolution."):
                raise ImportError(f"No module named '{name.split('.')[0]}'")
            return real_import(name, globals, locals, fromlist, level)

        removed = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "evolution" or name.startswith("evolution.")
        }
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with mock.patch("builtins.__import__", side_effect=blocking_import):
            reloaded = importlib.reload(eh)

        def _restore():
            sys.modules.update(removed)
            try:
                importlib.reload(eh)
            except Exception:
                pass

        reloaded._restore_fn = _restore  # type: ignore[attr-defined]
        return reloaded

    def test_fallback_classifier_returns_unknown(self, tmp_path, monkeypatch):
        reloaded = self._reload_with_evolution_blocked(tmp_path, monkeypatch)
        try:
            category = reloaded.ErrorClassifier().classify("Connection refused")
            assert category.value == "unknown"
        finally:
            reloaded._restore_fn()

    def test_harvest_runs_with_fallback_classifier(self, tmp_path, monkeypatch):
        """A full harvest pass must not raise even when only the fallback
        classifier is available — entries are still emitted with
        ``failure_category='unknown'``."""
        reloaded = self._reload_with_evolution_blocked(tmp_path, monkeypatch)
        try:
            db, db_path = _make_db(tmp_path)
            _add_session(
                db,
                "s-err",
                [
                    _user(),
                    {
                        "role": "tool",
                        "content": "Error: connection refused",
                        "tool_name": "web_search",
                    },
                    _assistant("I apologize, but I encountered an error."),
                ],
            )
            db.close()
            summary = reloaded.run_harvest(db_path=db_path, now=NOW)
            assert summary["entries_appended"] == 1
            entries = _entries()
            assert entries
            assert entries[-1].failure_category == "unknown"
        finally:
            reloaded._restore_fn()
