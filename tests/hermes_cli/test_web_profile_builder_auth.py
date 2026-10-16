"""Profile-builder MCP credentials remain scoped to the new profile."""
import pytest
import hermes_yaml as yaml


class TestProfileBuilderAuth:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN


    def test_profiles_create_builder_mcp_auth_is_profile_scoped(
        self, monkeypatch
    ):
        from hermes_constants import get_hermes_home
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "create_wrapper_script", lambda name: None)
        monkeypatch.setenv("HERMES_NO_TQMEMORY", "1")

        secret = "profile-builder-secret"
        resp = self.client.post(
            "/api/profiles",
            json={
                "name": "builder-auth",
                "mcp_servers": [
                    {
                        "name": "Bearer Server",
                        "url": "https://example.com/mcp",
                        "auth": "header",
                        "bearer_token": f"Bearer {secret}",
                    },
                    {
                        "name": "oauth-server",
                        "url": "https://example.com/oauth-mcp",
                        "auth": "oauth",
                    },
                    {
                        "name": "local-server",
                        "command": "uvx",
                        "args": ["mcp-server", "--debug"],
                        "env": {"API_KEY": "stdio-secret"},
                    },
                    {
                        "name": "missing-token",
                        "url": "https://example.com/bad",
                        "auth": "header",
                    },
                    {
                        "name": "http-with-env",
                        "url": "https://example.com/bad-env",
                        "env": {"NOT_SUPPORTED": "value"},
                    },
                ],
            },
        )

        assert resp.status_code == 200
        assert resp.json()["mcp_written"] == 3

        root = get_hermes_home()
        profile_dir = root / "profiles" / "builder-auth"
        config_text = (profile_dir / "config.yaml").read_text(encoding="utf-8")
        config = yaml.safe_load(config_text)
        servers = config["mcp_servers"]

        assert sorted(servers) == [
            "Bearer Server",
            "local-server",
            "oauth-server",
        ]
        assert servers["Bearer Server"] == {
            "url": "https://example.com/mcp",
            "headers": {
                "Authorization": "Bearer ${MCP_BEARER_SERVER_API_KEY}",
            },
        }
        assert servers["oauth-server"] == {
            "url": "https://example.com/oauth-mcp",
            "auth": "oauth",
        }
        assert servers["local-server"] == {
            "command": "uvx",
            "args": ["mcp-server", "--debug"],
            "env": {"API_KEY": "stdio-secret"},
        }

        assert secret not in config_text
        profile_env = (profile_dir / ".env").read_text(encoding="utf-8")
        assert f"MCP_BEARER_SERVER_API_KEY={secret}" in profile_env
        assert "Bearer Bearer" not in profile_env
        assert not (root / ".env").exists()
