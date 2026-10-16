"""Declared dependency failures keep instructional content usable and recoverable."""
from tools.skills_tool import _skill_dependency_note


def test_unknown_pm_failure_preserves_remedy_without_logging_exception_data(monkeypatch, caplog):
    import pm

    called = []
    def ensure(name):
        called.append(name)
        if name == "ffmpeg":
            raise RuntimeError("synthetic-private-installer-detail")
    monkeypatch.setattr(pm, "ensure", ensure)
    note = _skill_dependency_note({"deps": ["ffmpeg", "git"]})
    assert called == ["ffmpeg", "git"]
    assert "ffmpeg: synthetic-private-installer-detail" in note
    assert "hermes pm install ffmpeg git" in note
    assert "synthetic-private-installer-detail" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_string_dependency_and_no_dependency_keep_existing_contract(monkeypatch):
    import pm

    called = []
    monkeypatch.setattr(pm, "ensure", called.append)
    assert _skill_dependency_note({"deps": "git"}) is None
    assert called == ["git"]
    assert _skill_dependency_note({}) is None
