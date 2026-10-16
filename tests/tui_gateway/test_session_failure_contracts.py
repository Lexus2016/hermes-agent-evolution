"""Failure contracts for inherited session handlers under the merged runtime bindings."""
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tui_gateway import server


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


def test_archive_storage_failure_returns_error_without_pending_success(isolated_home, monkeypatch):
    db = SimpleNamespace(resolve_session_id=lambda key: key,
                         set_session_archived=Mock(side_effect=RuntimeError("storage unavailable")))
    monkeypatch.setattr(server, "_profile_db", lambda *a, **kw: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_sessions", {})
    result = server._methods["session.archive"]("archive", {"session_id": "stored-key"})
    assert result["error"]["code"] == 5007
    assert "result" not in result
    db.set_session_archived.assert_called_once_with("stored-key", True)
    assert server._sessions == {}


def test_billing_read_failure_has_no_account_authority(isolated_home, monkeypatch):
    import agent.billing_view
    import hermes_cli.anon_auth

    monkeypatch.setattr(hermes_cli.anon_auth, "has_free_tier_account", lambda: False)
    monkeypatch.setattr(agent.billing_view, "build_billing_state",
                        Mock(side_effect=RuntimeError("portal unavailable")))
    result = server._methods["billing.state"]("billing", {})["result"]
    assert result == {"ok": True, "logged_in": False, "free_tier_account": False,
                      "error": "could not load billing state"}


def test_save_write_failure_is_not_reported_as_saved(isolated_home, monkeypatch):
    import hermes_cli.session_export

    # Existing directory path deliberately cannot become the export file's parent.
    isolated_home.mkdir()
    (isolated_home / "sessions").write_text("occupied")
    session = {"profile_home": str(isolated_home), "session_key": "saved-key"}
    monkeypatch.setattr(server, "_sess", lambda params, rid: (session, None))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda session: False)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda *a, **kw: contextlib.nullcontext())
    monkeypatch.setattr(server, "_session_db", lambda session: contextlib.nullcontext(object()))
    monkeypatch.setattr(server, "_submit_row_target_key", lambda session: "saved-key")
    monkeypatch.setattr(hermes_cli.session_export, "load_save_snapshot",
                        lambda *a: {"id": "saved-key", "messages": []})
    result = server._methods["session.save"]("save", {"session_id": "saved-key"})
    assert result["error"]["code"] == 5011
    assert "result" not in result
    assert (isolated_home / "sessions").read_text() == "occupied"
