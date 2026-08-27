# -*- coding: utf-8 -*-
"""Unit tests for tools.environments.openai_sandbox_adapter (#3242)."""

import json
import pytest
from tools.environments.openai_sandbox_adapter import (
    SANDBOX_REGISTRY,
    SandboxStateRegistry,
    openai_manifest_to_hermes_config,
    parse_openai_manifest,
)


class TestOpenAISandboxAdapter:
    def test_parse_manifest_dict(self):
        data = {
            "sandbox_run_config": {
                "image": "python:3.12-slim",
                "working_directory": "/app",
                "env": {"FOO": "bar"},
                "mounts": ["/tmp/host:/tmp/container:ro"],
            }
        }
        parsed = parse_openai_manifest(data)
        assert parsed["image"] == "python:3.12-slim"
        assert parsed["working_directory"] == "/app"
        assert parsed["env"] == {"FOO": "bar"}

    def test_openai_manifest_to_hermes_config(self):
        data = {
            "image": "node:20-alpine",
            "workdir": "/workspace",
            "env": {"NODE_ENV": "production"},
            "mounts": [
                {"host": "/host/data", "container": "/data", "mode": "rw"},
            ],
            "timeout": 120,
            "init_commands": ["npm install"],
        }
        cfg = openai_manifest_to_hermes_config(data)
        assert cfg["environment"] == "docker"
        assert cfg["docker_image"] == "node:20-alpine"
        assert cfg["docker_workdir"] == "/workspace"
        assert cfg["docker_env"] == {"NODE_ENV": "production"}
        assert cfg["docker_bind_mounts"] == ["/host/data:/data:rw"]
        assert cfg["timeout"] == 120
        assert cfg["init_commands"] == ["npm install"]

    def test_sandbox_state_registry_snapshot_and_resume(self):
        reg = SandboxStateRegistry()
        reg.record_state("sandbox-1", "container-abc123", {"user": "admin"})
        state = reg.get_state("sandbox-1")
        assert state is not None
        assert state["container_id"] == "container-abc123"
        assert state["metadata"] == {"user": "admin"}
        assert reg.get_state("unknown") is None
