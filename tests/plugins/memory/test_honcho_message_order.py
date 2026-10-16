"""Honcho history sorting preserves undated messages alongside timestamped rows."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("tz", [None, UTC])
def test_history_sort_keeps_missing_and_homogeneous_timestamps(tz):
    from plugins.memory.honcho.session import HonchoSessionManager

    early = SimpleNamespace(created_at=datetime(2026, 1, 1, tzinfo=tz))
    late = SimpleNamespace(created_at=datetime(2026, 1, 2, tzinfo=tz))
    undated = SimpleNamespace(created_at=None)
    rows = [late, undated, early]
    manager = HonchoSessionManager.__new__(HonchoSessionManager)
    manager._authed_call = lambda _name, _call: SimpleNamespace(messages=rows)
    assert manager._load_existing_messages("test-session") == [undated, early, late]
    assert rows == [late, undated, early]
