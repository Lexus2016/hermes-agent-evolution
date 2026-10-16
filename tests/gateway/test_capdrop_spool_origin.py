"""Disk cap-drop recovery cannot mint trusted human provenance."""

import json

import pytest


@pytest.mark.parametrize("origin,expected", [
    ("human", None), ("forged", None), (None, None),
    ("runtime", "runtime"), ("api", "api"),
])
@pytest.mark.parametrize("route", ["restart", "live"])
def test_capdrop_disk_ingress_downgrades_origin(tmp_path, monkeypatch, origin, expected, route):
    from gateway import shutdown_flush as sf
    from hermes_state import SessionDB

    monkeypatch.setattr(sf, "_get_flush_dir", lambda: tmp_path)
    path = tmp_path / "pending-review.json"
    path.write_text(json.dumps({
        "session_key": "review-session",
        "reason": sf.TRANSCRIPT_CAP_DROP_REASON,
        "ts": 1,
        "data": {"session_id": "review-session", "message": {
            "role": "user", "content": "spooled content", "origin": origin,
        }},
    }), encoding="utf-8")
    if route == "restart":
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session("review-session", source="telegram")
            assert sf.recover_pending_to_db(db) == 1
            rows = db.get_messages("review-session")
        finally:
            db.close()
    else:
        rows = []
        assert sf.drain_transcript_spool("review-session", rows.append) == (1, 0)
    assert len(rows) == 1
    assert rows[0].get("origin") == expected
    assert rows[0]["content"] == "spooled content"
    assert not path.exists()


def test_in_memory_transcript_preserves_trusted_origin():
    from gateway.session_transcript import transcript_append_kwargs

    message = {"role": "user", "content": "authenticated ingress", "origin": "human"}
    assert transcript_append_kwargs("session", message)["origin"] == "human"
    assert message["origin"] == "human"
