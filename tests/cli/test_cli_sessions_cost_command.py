"""End-to-end CLI test for ``hermes sessions cost`` (issue #254).

Drives the full argparse path in ``hermes_cli.main`` via subprocess against an
isolated ``HERMES_HOME`` so the wiring (parser + handler + InsightsEngine reuse)
is exercised exactly as a user would hit it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]


def _seed_state_db(home: Path) -> None:
    """Create a state.db under ``home`` with a priced parent + subagent child."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    env["HERMES_HOME"] = str(home)
    seed = (
        "from hermes_state import SessionDB, DEFAULT_DB_PATH\n"
        "db = SessionDB(db_path=DEFAULT_DB_PATH)\n"
        "db.create_session(session_id='parent', source='cli',"
        " model='anthropic/claude-sonnet-4-20250514')\n"
        "db.update_token_counts('parent', input_tokens=100000, output_tokens=20000,"
        " billing_provider='anthropic')\n"
        "db.append_message('parent', role='assistant', content='x',"
        " tool_calls=[{'function': {'name': 'read_file'}}])\n"
        "db.append_message('parent', role='tool', content='y', tool_name='read_file')\n"
        "db.create_session(session_id='child', source='subagent',"
        " model='anthropic/claude-haiku-4-5', parent_session_id='parent',"
        " model_config={'_delegate_from': 'parent'})\n"
        "db.update_token_counts('child', input_tokens=30000, output_tokens=5000,"
        " billing_provider='anthropic')\n"
        "db.append_message('child', role='assistant', content='a',"
        " tool_calls=[{'function': {'name': 'terminal'}}])\n"
        "db.append_message('child', role='tool', content='b', tool_name='terminal')\n"
        "db._conn.commit(); db.close()\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", seed],
        env=env, capture_output=True, text=True, cwd=str(_WORKTREE), timeout=60,
    )
    assert res.returncode == 0, res.stderr


def _run_sessions_cost(home: Path, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "sessions", "cost"] + args,
        env=env, capture_output=True, text=True, cwd=str(_WORKTREE), timeout=60,
    )


def test_sessions_cost_json_output(tmp_path):
    _seed_state_db(tmp_path)
    res = _run_sessions_cost(tmp_path, ["--json"])
    assert res.returncode == 0, res.stderr
    report = json.loads(res.stdout)

    assert report["cost_attribution"] == (
        "proportional_by_response_chars_fallback_call_count"
    )
    assert report["totals"]["total_sessions"] == 2
    assert report["totals"]["estimated_cost"] == pytest.approx(0.655, abs=1e-4)

    rows = {r["session_id"]: r for r in report["sessions"]}
    assert rows["child"]["is_subagent"] is True
    assert rows["child"]["parent_session_id"] == "parent"

    tools = {t["tool"]: t for t in report["tool_costs"]}
    assert set(tools) == {"read_file", "terminal"}
    assert report["subagents"]["subagent_sessions"] == 1


def test_sessions_cost_terminal_output(tmp_path):
    _seed_state_db(tmp_path)
    res = _run_sessions_cost(tmp_path, [])
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "Hermes Cost Attribution" in out
    assert "Per-Session Spend" in out
    assert "Per-Tool Cost" in out
    assert "Subagent" in out


def test_sessions_cost_source_filter(tmp_path):
    _seed_state_db(tmp_path)
    res = _run_sessions_cost(tmp_path, ["--source", "subagent", "--json"])
    assert res.returncode == 0, res.stderr
    report = json.loads(res.stdout)
    assert report["totals"]["total_sessions"] == 1
    assert report["sessions"][0]["session_id"] == "child"
