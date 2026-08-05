"""Tests for the #1704 web_search consecutive-search streak guard.

Mirrors tool_search streak tests (#1144/#1373): a per-session counter of
consecutive web_search calls with no intervening action/synthesis tool that
crosses ``web.search_streak_threshold`` and appends a ``fallback_directive``.
"""

import json

import pytest


@pytest.fixture(autouse=True)
def clear_streak():
    from tools import web_tools

    web_tools._web_search_streak.clear()
    yield
    web_tools._web_search_streak.clear()


def test_note_increments_and_reset_clears():
    from tools.web_tools import note_web_search, reset_web_search_streak

    assert note_web_search("sess-a") == 1
    assert note_web_search("sess-a") == 2
    reset_web_search_streak("sess-a")
    assert note_web_search("sess-a") == 1  # fresh again


def test_none_session_not_tracked():
    from tools.web_tools import _web_search_streak, note_web_search

    assert note_web_search(None) == 0
    assert _web_search_streak == {}


def test_empty_session_tracked_under_default_key():
    """Runtime sends ``agent.session_id or ""`` — must still fire (#1373)."""
    from tools.web_tools import (
        _web_search_default_key,
        _web_search_streak,
        note_web_search,
    )

    assert note_web_search("") == 1
    assert _web_search_streak[_web_search_default_key] == 1


def test_threshold_resolution(monkeypatch):
    from tools.web_tools import (
        DEFAULT_WEB_SEARCH_STREAK_THRESHOLD,
        _get_web_search_streak_threshold,
    )

    monkeypatch.setattr("tools.web_tools._load_web_config", lambda: {})
    assert _get_web_search_streak_threshold() == DEFAULT_WEB_SEARCH_STREAK_THRESHOLD
    cases = [
        ({"search_streak_threshold": 0}, 0),  # 0 disables
        ({"search_streak_threshold": 999}, 20),  # clamped to 20
    ]
    for cfg, expected in cases:
        monkeypatch.setattr("tools.web_tools._load_web_config", lambda c=cfg: c)
        assert _get_web_search_streak_threshold() == expected


def test_directive_steers_to_synthesize():
    from tools.web_tools import _web_search_fallback_directive

    d = _web_search_fallback_directive(6)
    assert "web_search 6 times" in d
    assert "STOP re-querying" in d
    assert "synthesize" in d
    assert "web_extract" in d


def test_injected_directive_after_threshold(monkeypatch):
    """Crossing the threshold yields a JSON-safe fallback_directive."""
    from tools import web_tools

    monkeypatch.setattr("tools.web_tools._get_web_search_streak_threshold", lambda: 3)
    web_tools._web_search_streak.clear()
    web_tools.note_web_search("sess-d")
    web_tools.note_web_search("sess-d")
    streak = web_tools.note_web_search("sess-d")  # 3

    payload = {"success": True, "data": {"web": [{"url": "x"}]}}
    if web_tools._get_web_search_streak_threshold() > 0 and streak >= 3:
        payload["fallback_directive"] = web_tools._web_search_fallback_directive(streak)
    assert "STOP re-querying" in payload["fallback_directive"]
    assert (
        json.loads(json.dumps(payload))["fallback_directive"]
        == payload["fallback_directive"]
    )
