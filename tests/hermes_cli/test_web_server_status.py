"""Web model configuration and gateway status endpoint contracts."""

import re
from datetime import UTC
from unittest.mock import patch

import pytest

import gateway.status as _gw_status
import hermes_cli.config as _cfg_mod
import hermes_cli.web_server_gateway as _web_server_gateway


class TestModelContextLength:
    """Tests for model_context_length in normalize/denormalize and /api/model/info."""

    def test_normalize_extracts_context_length_from_dict(self):
        """normalize should surface context_length from model dict."""
        from hermes_cli.web_server_config import _normalize_config_for_web

        cfg = {
            "model": {
                "default": "anthropic/claude-opus-4.6",
                "provider": "openrouter",
                "context_length": 200000,
            }
        }
        result = _normalize_config_for_web(cfg)
        assert result["model"] == "anthropic/claude-opus-4.6"
        assert result["model_context_length"] == 200000

    def test_normalize_bare_string_model_yields_zero(self):
        """normalize should set model_context_length=0 for bare string model."""
        from hermes_cli.web_server_config import _normalize_config_for_web

        result = _normalize_config_for_web({"model": "anthropic/claude-sonnet-4"})
        assert result["model"] == "anthropic/claude-sonnet-4"
        assert result["model_context_length"] == 0


    def test_denormalize_writes_context_length_into_model_dict(self):
        """denormalize should write model_context_length back into model dict."""
        from hermes_cli.web_server_config import _denormalize_config_from_web
        from hermes_cli.config import save_config

        # Set up disk config with model as a dict
        save_config({
            "model": {"default": "anthropic/claude-opus-4.6", "provider": "openrouter"}
        })

        result = _denormalize_config_from_web({
            "model": "anthropic/claude-opus-4.6",
            "model_context_length": 100000,
        })
        assert isinstance(result["model"], dict)
        assert result["model"]["context_length"] == 100000
        assert "model_context_length" not in result  # virtual field removed

    def test_denormalize_context_length_alone_is_applied(self):
        """The Settings autosave now sends a diff, not the full draft: editing
        only the Context Window control must not omit ``model`` and thereby
        drop the context_length edit on the floor (#89597 review)."""
        from hermes_cli.web_server_config import _denormalize_config_from_web
        from hermes_cli.config import save_config

        save_config({
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic",
                      "context_length": 100000}
        })

        result = _denormalize_config_from_web({"model_context_length": 200000})
        assert isinstance(result["model"], dict)
        assert result["model"]["context_length"] == 200000
        assert result["model"]["default"] == "anthropic/claude-sonnet-4"

    def test_denormalize_model_alone_preserves_context_length(self):
        """The mirror case: editing only the Model field must not silently
        wipe an existing context_length override just because the diff omits
        the unrelated model_context_length key (#89597 review).

        No ``provider`` on disk here on purpose: that keeps this test isolated
        to the diff-omission bug rather than the separate, pre-existing (and
        intentional, see ``_apply_main_model_assignment``) behavior where a
        real provider switch drops the context_length override."""
        from hermes_cli.web_server_config import _denormalize_config_from_web
        from hermes_cli.config import save_config

        save_config({
            "model": {"default": "anthropic/claude-sonnet-4", "context_length": 150000}
        })

        result = _denormalize_config_from_web({"model": "anthropic/claude-opus-4.6"})
        assert result["model"]["context_length"] == 150000
        assert result["model"]["default"] == "anthropic/claude-opus-4.6"


class TestDenormalizeProviderSwitch:
    """The flat Config-page Model field carries no provider info. When the
    model string changes to one served by a different provider, the saved
    provider must follow it (issue #14058)."""

    def test_vendor_slug_switches_off_non_aggregator_provider(self):
        """ollama-local + a vendor/model slug → switch to openrouter and drop
        the stale local base_url (the issue's exact repro)."""
        from hermes_cli.web_server_config import _denormalize_config_from_web
        from unittest.mock import patch as _patch
        from hermes_cli.config import save_config

        save_config({
            "model": {
                "default": "llama3.2",
                "provider": "ollama-local",
                "base_url": "http://localhost:11434/v1",
                "api_mode": "chat_completions",
            }
        })

        with _patch("hermes_cli.models_detect.provider_has_credentials", lambda p: p == "openrouter"):
            result = _denormalize_config_from_web({"model": "google/gemini-2.5-flash"})
        model = result["model"]
        assert model["provider"] == "openrouter"
        assert model["default"] == "google/gemini-2.5-flash"
        # The old ollama-local endpoint must not carry over to openrouter (the switch resolves
        # the aggregator's own endpoint instead of leaving the field blank or stale).
        assert model.get("base_url") != "http://localhost:11434/v1"


    def test_context_length_override_survives_provider_switch(self):
        """An explicit context-length override must persist alongside a
        provider switch."""
        from hermes_cli.web_server_config import _denormalize_config_from_web
        from unittest.mock import patch as _patch
        from hermes_cli.config import save_config

        save_config({"model": {"default": "llama3.2", "provider": "ollama-local"}})

        with _patch("hermes_cli.models_detect.provider_has_credentials", lambda p: p == "openrouter"):
            result = _denormalize_config_from_web({
                "model": "google/gemini-2.5-flash",
                "model_context_length": 128000,
            })
        model = result["model"]
        assert model["provider"] == "openrouter"
        assert model["context_length"] == 128000

    def test_rejected_switch_is_400_and_leaves_the_model_block_byte_identical(self, monkeypatch):
        """``switch_model`` rejecting the inferred provider must surface as 400 from
        ``PUT /api/config`` — not fall back to the flat string, which the deep-merge would
        write OVER the on-disk ``model:`` dict (provider/base_url/api_mode/slots destroyed)."""
        from starlette.testclient import TestClient
        from hermes_constants import get_hermes_home
        from hermes_cli.model_switch import ModelSwitchResult
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        cfg_path = get_hermes_home() / "config.yaml"
        cfg_path.write_text(
            "model:\n"
            "  default: llama3.2\n"
            "  provider: ollama-local\n"
            "  base_url: http://localhost:11434/v1\n"
            "  api_mode: chat_completions\n"
            "  context_length: 32000\n"
            "  model_slots:\n"
            "    fast: qwen3\n",
            encoding="utf-8")
        before = cfg_path.read_bytes()
        monkeypatch.setattr("hermes_cli.models_detect.provider_has_credentials", lambda p: p == "openrouter")
        monkeypatch.setattr("hermes_cli.model_switch.switch_model",
                            lambda **_kw: ModelSwitchResult(success=False, error_message="models.dev offline"))

        client = TestClient(app)
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        resp = client.put("/api/config", json={"config": {"model": "openai/gpt-5.5-zzz"}})

        assert resp.status_code == 400 and "models.dev offline" in resp.json()["detail"]
        assert cfg_path.read_bytes() == before


class TestModelInfoEndpoint:
    """Tests for GET /api/model/info endpoint."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import app
        self.client = TestClient(app)


    def test_model_info_with_dict_config(self, monkeypatch):

        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {
            "model": {
                "default": "anthropic/claude-opus-4.6",
                "provider": "openrouter",
                "context_length": 100000,
            }
        })

        with patch("agent.model_metadata.get_model_context_length", return_value=200000):
            resp = self.client.get("/api/model/info")

        data = resp.json()
        assert data["model"] == "anthropic/claude-opus-4.6"
        assert data["provider"] == "openrouter"
        assert data["auto_context_length"] == 200000
        assert data["config_context_length"] == 100000
        assert data["effective_context_length"] == 100000  # override wins


    def test_model_info_graceful_on_metadata_error(self, monkeypatch):
        """Endpoint should return zeros on import/resolution errors, not 500."""

        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {
            "model": "some/obscure-model"
        })

        with patch("agent.model_metadata.get_model_context_length", side_effect=Exception("boom")):
            resp = self.client.get("/api/model/info")

        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_context_length"] == 0


# ---------------------------------------------------------------------------
# Gateway health probe tests
# ---------------------------------------------------------------------------


class TestStatusRemoteGateway:
    """Tests for /api/status with remote gateway health fallback."""

    @pytest.fixture(autouse=True)
    def _setup_test_client(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_status_falls_back_to_remote_probe(self, monkeypatch):
        """When local PID check fails and remote probe succeeds, gateway shows running."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(_gw_status, "get_running_pid_cached", lambda: None)
        monkeypatch.setattr(_gw_status, "read_runtime_status", lambda: None)
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(_web_server_gateway, "_probe_gateway_health", lambda: (True, {
            "status": "ok",
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
            "pid": 999,
        }))

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway_running"] is True
        assert data["gateway_pid"] == 999
        assert data["gateway_state"] == "running"
        assert data["gateway_health_url"] == "http://gw:8642"


    def test_status_remote_running_null_pid(self, monkeypatch):
        """Remote gateway running but PID not in response — pid should be None."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(_gw_status, "get_running_pid_cached", lambda: None)
        monkeypatch.setattr(_gw_status, "read_runtime_status", lambda: None)
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(_web_server_gateway, "_probe_gateway_health", lambda: (True, {
            "status": "ok",
        }))

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway_running"] is True
        assert data["gateway_pid"] is None
        assert data["gateway_state"] == "running"


class TestStatusInstallId:
    """Stable per-install identity on /api/status.

    Behaviour contracts: the id is minted once, persisted under the ROOT
    Hermes home (not the profile home), survives a fresh process-cache read,
    and is byte-identical for every profile served by the same install — the
    desktop uses it to collapse duplicate roster rows when one backend is
    registered under two addresses.
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_cli.web_server as ws
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        # Fresh process cache per test: the cache is process-global by design
        # (stability), so tests must not observe a previous test's id.
        monkeypatch.setattr(ws, "_INSTALL_ID_CACHE", {"value": None})
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_status_reports_persistent_install_id(self, monkeypatch):
        from hermes_constants import get_default_hermes_root

        monkeypatch.setattr(_gw_status, "get_running_pid_cached", lambda: None)
        monkeypatch.setattr(_gw_status, "read_runtime_status", lambda: None)

        first = self.client.get("/api/status")
        assert first.status_code == 200
        install_id = first.json().get("install_id")
        assert isinstance(install_id, str)
        # Opaque random hex only — no hardware/user-derived material.
        assert re.fullmatch(r"[0-9a-f]{32}", install_id)

        # Persisted under the ROOT home, and stable across calls.
        id_file = get_default_hermes_root() / "install_id"
        assert id_file.is_file()
        assert id_file.read_text(encoding="utf-8").strip() == install_id

        second = self.client.get("/api/status")
        assert second.json().get("install_id") == install_id

    def test_install_id_survives_process_cache_reset(self, monkeypatch):
        """A restart (fresh cache) re-reads the SAME persisted id."""
        import hermes_cli.web_server as ws

        first = ws.get_install_id()
        assert first
        monkeypatch.setattr(ws, "_INSTALL_ID_CACHE", {"value": None})
        assert ws.get_install_id() == first

    def test_all_profiles_of_one_install_share_the_id(self, monkeypatch, tmp_path):
        """HERMES_HOME=<root> and HERMES_HOME=<root>/profiles/<name> resolve to
        the same id file — profiles share one physical install identity."""
        import hermes_cli.web_server as ws

        root = tmp_path / "hermes-root"
        profile_home = root / "profiles" / "research"
        profile_home.mkdir(parents=True)

        monkeypatch.setenv("HERMES_HOME", str(root))
        monkeypatch.setattr(ws, "_INSTALL_ID_CACHE", {"value": None})
        root_id = ws.get_install_id()
        assert root_id

        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.setattr(ws, "_INSTALL_ID_CACHE", {"value": None})
        assert ws.get_install_id() == root_id
        # Exactly one id file exists — under the root, not the profile home.
        assert (root / "install_id").is_file()
        assert not (profile_home / "install_id").exists()

    def test_corrupt_id_file_is_replaced_not_propagated(self, monkeypatch, tmp_path):
        import hermes_cli.web_server as ws

        root = tmp_path / "hermes-root"
        root.mkdir()
        (root / "install_id").write_text("not-a-valid-id\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(root))
        monkeypatch.setattr(ws, "_INSTALL_ID_CACHE", {"value": None})

        value = ws.get_install_id()
        assert value and re.fullmatch(r"[0-9a-f]{32}", value)
        assert (root / "install_id").read_text(encoding="utf-8").strip() == value


class TestGatewayBusyReadout:
    """Tests for the NAS busy/drainable readout on /api/status.

    Behaviour contracts (not snapshots): assert how gateway_busy / gateway_drainable
    must RELATE to gateway_running + gateway_state + active_agents, and that every
    field degrades to a safe falsy value when the gateway is down or its status
    file is absent. Liveness must key off gateway_running, NEVER gateway_updated_at.
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN


    def test_draining_state_is_neither_busy_nor_drainable(self, monkeypatch):
        """While draining, the gateway is not a fresh begin-drain target, and
        busy is False even with a stale active_agents>0 in the file — the state
        gate dominates."""

        monkeypatch.setattr(_gw_status, "get_running_pid_cached", lambda: 1234)
        monkeypatch.setattr(_gw_status, "read_runtime_status", lambda: {
            "gateway_state": "draining",
            "platforms": {},
            "active_agents": 3,
        })

        data = self.client.get("/api/status").json()
        assert data["gateway_busy"] is False
        assert data["gateway_drainable"] is False


    def test_active_agents_unparseable_in_file_degrades_to_zero(self, monkeypatch):
        """A corrupt active_agents value in the status file must not 500 or
        produce a spurious busy — it degrades to 0/not-busy."""

        monkeypatch.setattr(_gw_status, "get_running_pid_cached", lambda: 1234)
        monkeypatch.setattr(_gw_status, "read_runtime_status", lambda: {
            "gateway_state": "running",
            "platforms": {},
            "active_agents": "garbage",
        })

        data = self.client.get("/api/status").json()
        assert data["active_agents"] == 0
        assert data["gateway_busy"] is False


class TestStatusMemoryBlock:
    """NS-656: /api/status must always carry a `memory` block."""

    @pytest.fixture(autouse=True)
    def _setup_test_client(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN


    def test_memory_block_degrades_when_collector_raises(self, monkeypatch):
        """A broken collector must never take down the status endpoint —
        the block degrades to pressure=unknown."""
        import gateway.memory_status as ms

        def _boom(*_a, **_k):
            raise RuntimeError("collector exploded")

        monkeypatch.setattr(ms, "collect_memory_status", _boom)
        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["memory"] == {"pressure": "unknown"}


    def test_disk_block_degrades_when_collector_raises(self, monkeypatch):
        """Same contract as the memory block: a broken collector must never
        take down the status endpoint."""
        import gateway.disk_status as ds

        def _boom(*_a, **_k):
            raise RuntimeError("collector exploded")

        monkeypatch.setattr(ds, "collect_disk_status", _boom)
        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["disk"] == {"pressure": "unknown"}


class TestGatewayUpdatedAtContract:
    """Contract tests for /api/status ``gateway_updated_at``.

    The field is promised to consumers (web/src/lib/api.ts declares
    ``string | null``) as an RFC3339 timestamp or null — NEVER a number.
    Legacy gateways wrote epoch floats into gateway_state.json and the file
    is hand-editable, so the endpoint must normalize whatever it reads.
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    @staticmethod
    def _assert_contract(value):
        """gateway_updated_at is None or a tz-aware-parseable ISO string."""
        from datetime import datetime

        assert not isinstance(value, bool), f"bool leaked: {value!r}"
        assert not isinstance(value, (int, float)), f"number leaked: {value!r}"
        if value is not None:
            assert isinstance(value, str)
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None, f"naive timestamp leaked: {value!r}"


    def test_local_runtime_valid_epoch_becomes_iso_string(self, monkeypatch):
        """A plausible legacy epoch value is converted, not dropped."""
        from datetime import datetime, timezone

        epoch = 1750000000
        monkeypatch.setattr(_gw_status, "get_running_pid_cached", lambda: 1234)
        monkeypatch.setattr(_gw_status, "read_runtime_status", lambda: {
            "gateway_state": "running",
            "platforms": {},
            "active_agents": 0,
            "updated_at": epoch,
        })

        value = self.client.get("/api/status").json()["gateway_updated_at"]
        assert isinstance(value, str)
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert parsed == datetime.fromtimestamp(epoch, tz=UTC)


    def test_remote_health_numeric_updated_at_normalized(self, monkeypatch):
        """Cross-container path: the remote /health/detailed body is the
        runtime source, and a numeric updated_at from an older gateway build
        must still come out as string|null."""
        import hermes_cli.web_server as ws

        monkeypatch.setattr(_gw_status, "get_running_pid_cached", lambda: None)
        monkeypatch.setattr(_gw_status, "read_runtime_status", lambda: None)
        monkeypatch.setattr(ws, "_GATEWAY_HEALTH_URL", "http://gw:8642")
        monkeypatch.setattr(_web_server_gateway, "_probe_gateway_health", lambda: (True, {
            "status": "ok",
            "gateway_state": "running",
            "platforms": {},
            "updated_at": 1750000000.25,
            "pid": 999,
        }))

        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway_running"] is True
        self._assert_contract(data["gateway_updated_at"])
        # A plausible epoch is converted, not nulled.
        assert isinstance(data["gateway_updated_at"], str)
