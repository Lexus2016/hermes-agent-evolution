"""Tests for the agent-team coordination primitive (issue #252).

Covers three layers:
  * TeamStore — the SQLite-backed shared task list + per-teammate mailboxes,
    including the atomic-claim race and broadcast fan-out.
  * team_task / team_message — the teammate-facing tools and their env/
    thread-local gating (hidden from a normal chat session).
  * delegate_task wiring — a task carrying a ``team`` field becomes a teammate
    with the agent_team toolset and a bound thread identity.
"""
from __future__ import annotations

import json
import threading

import pytest

from tools.agent_team import (
    TASK_STATUSES,
    TEAM_ID_ENV,
    TEAM_MEMBER_ENV,
    TeamStore,
    clear_thread_identity,
    current_member,
    current_team_id,
    is_valid_slug,
    set_thread_identity,
    team_db_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = TeamStore("teamA", db_path=tmp_path / "teamA.db")
    s.ensure_schema()
    return s


@pytest.fixture(autouse=True)
def _clean_identity():
    """Ensure no thread-local identity bleeds across tests."""
    clear_thread_identity()
    yield
    clear_thread_identity()


# ---------------------------------------------------------------------------
# TeamStore: slugs + path safety
# ---------------------------------------------------------------------------

def test_valid_slug_accepts_safe_names():
    assert is_valid_slug("team-1")
    assert is_valid_slug("alice.bob_2")
    assert not is_valid_slug("../etc")
    assert not is_valid_slug("has space")
    assert not is_valid_slug("")
    assert not is_valid_slug("x" * 65)


def test_invalid_team_id_rejected():
    with pytest.raises(ValueError):
        TeamStore("../escape")


def test_team_db_path_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TEAM_DB", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = team_db_path("teamX")
    assert path == tmp_path / "agent_teams" / "teamX.db"


def test_team_db_path_env_override(monkeypatch, tmp_path):
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("HERMES_TEAM_DB", str(pinned))
    assert team_db_path("anything") == pinned


# ---------------------------------------------------------------------------
# TeamStore: shared task list
# ---------------------------------------------------------------------------

def test_add_and_list_tasks(store):
    t1 = store.add_task("research api")
    t2 = store.add_task("write report", assignee="bob")
    assert t1["status"] == "open"
    assert t2["status"] == "claimed" and t2["assignee"] == "bob"
    tasks = store.list_tasks()
    assert {t["title"] for t in tasks} == {"research api", "write report"}


def test_add_task_requires_title(store):
    with pytest.raises(ValueError):
        store.add_task("   ")


def test_list_tasks_status_filter(store):
    store.add_task("a")
    store.add_task("b", assignee="alice")
    assert len(store.list_tasks(status="open")) == 1
    assert len(store.list_tasks(status="claimed")) == 1


def test_claim_open_task(store):
    t = store.add_task("do thing")
    outcome = store.claim_task(t["id"], "alice")
    assert outcome["claimed"] is True
    assert outcome["task"]["assignee"] == "alice"
    assert outcome["task"]["status"] == "claimed"


def test_claim_already_claimed_loses(store):
    t = store.add_task("do thing")
    assert store.claim_task(t["id"], "alice")["claimed"] is True
    second = store.claim_task(t["id"], "bob")
    assert second["claimed"] is False
    assert "alice" in second["reason"]


def test_reclaim_by_same_member_is_idempotent(store):
    t = store.add_task("do thing")
    store.claim_task(t["id"], "alice")
    again = store.claim_task(t["id"], "alice")
    assert again["claimed"] is True


def test_claim_missing_task(store):
    outcome = store.claim_task("tt_nonexistent", "alice")
    assert outcome["claimed"] is False
    assert outcome["task"] is None


def test_concurrent_claim_exactly_one_winner(store):
    """The atomic UPDATE guarded on status='open' must let exactly one of N
    concurrent claimers win — this is the shared-task-list correctness core."""
    t = store.add_task("contested")
    results = {}
    barrier = threading.Barrier(8)

    def worker(name):
        barrier.wait()  # maximise contention
        results[name] = store.claim_task(t["id"], name)["claimed"]

    threads = [threading.Thread(target=worker, args=(f"m{i}",)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    winners = [name for name, won in results.items() if won]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert store.get_task(t["id"])["assignee"] == winners[0]


def test_complete_task_records_result(store):
    t = store.add_task("compute")
    store.claim_task(t["id"], "alice")
    outcome = store.complete_task(t["id"], result="answer=42", member="alice")
    assert outcome["completed"] is True
    assert outcome["task"]["status"] == "done"
    assert outcome["task"]["result"] == "answer=42"


# ---------------------------------------------------------------------------
# TeamStore: direct teammate messaging
# ---------------------------------------------------------------------------

def test_direct_message_reaches_recipient(store):
    store.add_member("alice")
    store.add_member("bob")
    sent = store.send_message("alice", "need the schema", recipient="bob")
    assert sent["sent"] is True and sent["broadcast"] is False
    bob_inbox = store.inbox("bob")
    assert len(bob_inbox) == 1
    assert bob_inbox[0]["body"] == "need the schema"
    assert bob_inbox[0]["sender"] == "alice"
    # alice did not receive her own direct message
    assert store.inbox("alice") == []


def test_inbox_marks_read(store):
    store.add_member("bob")
    store.send_message("alice", "hi", recipient="bob")
    assert len(store.inbox("bob")) == 1
    # second poll: unread-only is now empty
    assert store.inbox("bob") == []


def test_broadcast_fans_out_to_all_but_sender(store):
    for name in ("alice", "bob", "carol"):
        store.add_member(name)
    store.send_message("alice", "team sync now")  # no recipient => broadcast
    assert len(store.inbox("bob")) == 1
    assert len(store.inbox("carol")) == 1
    assert store.inbox("alice") == []  # sender excluded


def test_send_requires_body(store):
    with pytest.raises(ValueError):
        store.send_message("alice", "   ", recipient="bob")


def test_message_invalid_recipient_slug(store):
    with pytest.raises(ValueError):
        store.send_message("alice", "hi", recipient="../evil")


# ---------------------------------------------------------------------------
# TeamStore: lead-side merge
# ---------------------------------------------------------------------------

def test_snapshot_aggregates_results(store):
    store.add_member("alice")
    store.add_member("bob")
    t1 = store.add_task("task one")
    t2 = store.add_task("task two")
    store.claim_task(t1["id"], "alice")
    store.complete_task(t1["id"], result="found X", member="alice")
    store.claim_task(t2["id"], "bob")
    snap = store.snapshot()
    assert snap["team_id"] == "teamA"
    assert snap["done_count"] == 1
    assert snap["open_count"] == 0  # t2 is claimed, not open
    assert snap["results"][t1["id"]] == "found X"
    assert {m["name"] for m in snap["members"]} == {"alice", "bob"}


# ---------------------------------------------------------------------------
# Identity resolution: arg -> thread-local -> env
# ---------------------------------------------------------------------------

def test_identity_arg_wins(monkeypatch):
    monkeypatch.setenv(TEAM_ID_ENV, "from-env")
    set_thread_identity("from-thread", "tmember")
    assert current_team_id("explicit") == "explicit"
    assert current_member("explicit-m") == "explicit-m"


def test_identity_thread_local_beats_env(monkeypatch):
    monkeypatch.setenv(TEAM_ID_ENV, "from-env")
    monkeypatch.setenv(TEAM_MEMBER_ENV, "env-member")
    set_thread_identity("from-thread", "thread-member")
    assert current_team_id() == "from-thread"
    assert current_member() == "thread-member"


def test_identity_falls_back_to_env(monkeypatch):
    clear_thread_identity()
    monkeypatch.setenv(TEAM_ID_ENV, "env-team")
    monkeypatch.setenv(TEAM_MEMBER_ENV, "env-member")
    assert current_team_id() == "env-team"
    assert current_member() == "env-member"


def test_thread_local_is_per_thread():
    """A thread identity set in one thread must not leak into another."""
    set_thread_identity("main-team", "main-member")
    other = {}

    def worker():
        other["team"] = current_team_id()  # no identity set on this thread

    th = threading.Thread(target=worker)
    th.start()
    th.join()
    assert other["team"] is None
    assert current_team_id() == "main-team"


# ---------------------------------------------------------------------------
# Tool gating
# ---------------------------------------------------------------------------

def test_team_tools_hidden_without_identity(monkeypatch, tmp_path):
    monkeypatch.delenv(TEAM_ID_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clear_thread_identity()

    import tools.agent_team_tools  # noqa: F401  ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    team = {n for n in names if n and n.startswith("team_")}
    assert team == set(), f"team tools leaked into normal chat schema: {team}"


def test_team_tools_visible_with_env(monkeypatch, tmp_path):
    monkeypatch.setenv(TEAM_ID_ENV, "teamA")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import tools.agent_team_tools  # noqa: F401
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    team = {n for n in names if n and n.startswith("team_")}
    assert team == {"team_task", "team_message"}


# ---------------------------------------------------------------------------
# team_task tool
# ---------------------------------------------------------------------------

def _setup_team_env(monkeypatch, tmp_path, member="alice"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(TEAM_ID_ENV, "teamT")
    monkeypatch.setenv(TEAM_MEMBER_ENV, member)


def test_team_task_no_active_team(monkeypatch, tmp_path):
    monkeypatch.delenv(TEAM_ID_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clear_thread_identity()
    from tools.agent_team_tools import team_task

    out = json.loads(team_task("list"))
    assert "error" in out


def test_team_task_list_claim_complete_flow(monkeypatch, tmp_path):
    _setup_team_env(monkeypatch, tmp_path, member="alice")
    from tools.agent_team_tools import team_task

    # Seed a task via the store (the lead's side).
    s = TeamStore("teamT")
    s.ensure_schema()
    task = s.add_task("inspect logs")

    listed = json.loads(team_task("list"))
    assert listed["team_id"] == "teamT"
    assert any(t["id"] == task["id"] for t in listed["tasks"])

    claimed = json.loads(team_task("claim", task_id=task["id"]))
    assert claimed["claimed"] is True
    assert claimed["task"]["assignee"] == "alice"

    done = json.loads(team_task("complete", task_id=task["id"], result="all clear"))
    assert done["completed"] is True
    assert done["task"]["result"] == "all clear"


def test_team_task_add_creates_shared_task(monkeypatch, tmp_path):
    _setup_team_env(monkeypatch, tmp_path, member="alice")
    from tools.agent_team_tools import team_task

    added = json.loads(team_task("add", title="handle the migration"))
    assert added["added"] is True
    assert added["task"]["title"] == "handle the migration"
    assert added["task"]["status"] == "open"
    # visible to the team
    listed = json.loads(team_task("list"))
    assert any(t["title"] == "handle the migration" for t in listed["tasks"])


def test_team_task_add_requires_title(monkeypatch, tmp_path):
    _setup_team_env(monkeypatch, tmp_path)
    from tools.agent_team_tools import team_task

    out = json.loads(team_task("add"))
    assert "error" in out


def test_team_task_claim_requires_task_id(monkeypatch, tmp_path):
    _setup_team_env(monkeypatch, tmp_path)
    from tools.agent_team_tools import team_task

    out = json.loads(team_task("claim"))
    assert "error" in out


def test_team_task_unknown_action(monkeypatch, tmp_path):
    _setup_team_env(monkeypatch, tmp_path)
    from tools.agent_team_tools import team_task

    out = json.loads(team_task("frobnicate"))
    assert "error" in out


def test_team_task_bad_status_filter(monkeypatch, tmp_path):
    _setup_team_env(monkeypatch, tmp_path)
    from tools.agent_team_tools import team_task

    out = json.loads(team_task("list", status="bogus"))
    assert "error" in out
    assert sorted(TASK_STATUSES)[0] in out["error"] or "bogus" in out["error"]


# ---------------------------------------------------------------------------
# team_message tool
# ---------------------------------------------------------------------------

def test_team_message_send_and_inbox(monkeypatch, tmp_path):
    # alice sends to bob
    _setup_team_env(monkeypatch, tmp_path, member="alice")
    from tools.agent_team_tools import team_message

    s = TeamStore("teamT")
    s.ensure_schema()
    s.add_member("alice")
    s.add_member("bob")

    sent = json.loads(team_message("send", body="schema ready", to="bob"))
    assert sent["sent"] is True

    # switch identity to bob and read inbox
    monkeypatch.setenv(TEAM_MEMBER_ENV, "bob")
    inbox = json.loads(team_message("inbox"))
    assert inbox["member"] == "bob"
    assert len(inbox["messages"]) == 1
    assert inbox["messages"][0]["body"] == "schema ready"


def test_team_message_send_requires_body(monkeypatch, tmp_path):
    _setup_team_env(monkeypatch, tmp_path)
    from tools.agent_team_tools import team_message

    out = json.loads(team_message("send", to="bob"))
    assert "error" in out


def test_team_message_unknown_action(monkeypatch, tmp_path):
    _setup_team_env(monkeypatch, tmp_path)
    from tools.agent_team_tools import team_message

    out = json.loads(team_message("explode"))
    assert "error" in out


# ---------------------------------------------------------------------------
# delegate_task wiring
# ---------------------------------------------------------------------------

def test_resolve_team_identity():
    from tools.delegate_tool import _resolve_team_identity

    assert _resolve_team_identity({"goal": "x"}, 0) is None
    assert _resolve_team_identity(
        {"goal": "x", "team": {"team_id": "t1", "member": "alice"}}, 0
    ) == ("t1", "alice")
    # missing member => positional fallback
    assert _resolve_team_identity({"goal": "x", "team": {"team_id": "t1"}}, 3) == (
        "t1",
        "teammate-3",
    )
    # invalid slug => degrade to plain delegation
    assert _resolve_team_identity({"goal": "x", "team": {"team_id": "../x"}}, 0) is None


def test_ensure_team_toolset_adds_agent_team():
    from tools.delegate_tool import _ensure_team_toolset

    assert "agent_team" in _ensure_team_toolset(["terminal", "file"], None)
    # already present => not duplicated
    result = _ensure_team_toolset(["agent_team"], None)
    assert result.count("agent_team") == 1


def test_team_identity_scope_binds_and_clears():
    from tools.delegate_tool import _team_identity_scope

    with _team_identity_scope(("t1", "alice")):
        assert current_team_id() == "t1"
        assert current_member() == "alice"
    # cleared on exit
    assert current_team_id() is None


def test_team_identity_scope_noop_for_none():
    from tools.delegate_tool import _team_identity_scope

    with _team_identity_scope(None):
        assert current_team_id() is None


# ---------------------------------------------------------------------------
# delegate_task end-to-end: a team task becomes a teammate
# ---------------------------------------------------------------------------

def _make_mock_parent():
    """Minimal parent agent with the fields delegate_task reads."""
    import threading as _t
    from unittest.mock import MagicMock

    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = _t.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.enabled_toolsets = ["terminal", "file"]
    return parent


def test_delegate_task_with_team_grants_agent_team_toolset(monkeypatch, tmp_path):
    """A delegated task carrying a team field must (a) receive the agent_team
    toolset and (b) get a bound team identity stamped on the child."""
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = _make_mock_parent()

    captured = {}

    def _fake_build(*args, **kwargs):
        # Capture the toolsets that delegate_task computed for the teammate.
        captured["toolsets"] = kwargs.get("toolsets")
        # Confirm the team identity is bound on THIS (build) thread so the
        # team tools' check_fn would pass during the child's tool resolution.
        captured["team_id_at_build"] = current_team_id()
        captured["member_at_build"] = current_member()
        child = MagicMock()
        child.run_conversation.return_value = {
            "final_response": "teammate done",
            "completed": True,
            "api_calls": 1,
            "messages": [],
        }
        child._delegate_depth = 1
        child._delegate_role = "leaf"
        child.session_estimated_cost_usd = 0.0
        return child

    from tools.delegate_tool import delegate_task

    with patch("tools.delegate_tool._build_child_agent", side_effect=_fake_build):
        result = json.loads(
            delegate_task(
                parent_agent=parent,
                tasks=[
                    {
                        "goal": "research the api",
                        "team": {"team_id": "teamE", "member": "alice"},
                    }
                ],
            )
        )

    assert result["results"][0]["status"] == "completed"
    assert "agent_team" in captured["toolsets"]
    assert captured["team_id_at_build"] == "teamE"
    assert captured["member_at_build"] == "alice"
