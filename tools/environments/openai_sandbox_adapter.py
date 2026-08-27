# -*- coding: utf-8 -*-
"""OpenAI Agents SDK sandbox runtime adapter (#3242).

Translates OpenAI Agents SDK Manifest and SandboxRunConfig structures
into Hermes sandboxed execution configurations (Docker environment).
Supports mount translation, environment mapping, lifecycle init commands,
and snapshot/resume tracking.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def parse_openai_manifest(manifest_input: Union[str, Dict[str, Any], Path]) -> Dict[str, Any]:
    """Parse an OpenAI Agents SDK manifest YAML/JSON string, dict, or file path."""
    if isinstance(manifest_input, Path):
        p = manifest_input
        raw_text = p.read_text(encoding="utf-8")
        if p.suffix in (".yaml", ".yml") and yaml is not None:
            data = yaml.safe_load(raw_text)
        else:
            try:
                data = json.loads(raw_text)
            except Exception:
                if yaml is not None:
                    data = yaml.safe_load(raw_text)
                else:
                    data = {}
    elif isinstance(manifest_input, str) and "\n" not in manifest_input and Path(manifest_input).is_file():
        p = Path(manifest_input)
        raw_text = p.read_text(encoding="utf-8")
        if p.suffix in (".yaml", ".yml") and yaml is not None:
            data = yaml.safe_load(raw_text)
        else:
            try:
                data = json.loads(raw_text)
            except Exception:
                if yaml is not None:
                    data = yaml.safe_load(raw_text)
                else:
                    data = {}
    elif isinstance(manifest_input, str):
        raw_text = manifest_input.strip()
        try:
            data = json.loads(raw_text)
        except Exception:
            if yaml is not None:
                data = yaml.safe_load(raw_text)
            else:
                data = {}
    elif isinstance(manifest_input, dict):
        data = dict(manifest_input)
    else:
        data = {}

    if not isinstance(data, dict):
        return {}

    # Extract nested sandbox or run_config if wrapped
    if "sandbox" in data and isinstance(data["sandbox"], dict):
        inner = data["sandbox"]
    elif "run_config" in data and isinstance(data["run_config"], dict):
        inner = data["run_config"]
    elif "sandbox_run_config" in data and isinstance(data["sandbox_run_config"], dict):
        inner = data["sandbox_run_config"]
    else:
        inner = data

    return inner


def openai_manifest_to_hermes_config(manifest_input: Union[str, Dict[str, Any], Path]) -> Dict[str, Any]:
    """Convert OpenAI Agents SDK sandbox manifest to Hermes terminal/docker config."""
    parsed = parse_openai_manifest(manifest_input)
    if not parsed:
        return {}

    image = parsed.get("image") or parsed.get("sandbox_image") or parsed.get("docker_image") or "ubuntu:latest"
    workdir = parsed.get("working_directory") or parsed.get("workdir") or parsed.get("cwd") or "/workspace"
    env = dict(parsed.get("env") or parsed.get("environment") or {})
    timeout = int(parsed.get("timeout", 300))

    # Parse and normalize mounts: list of strings 'host:container[:mode]' or dicts
    mounts: List[str] = []
    raw_mounts = parsed.get("mounts") or parsed.get("bind_mounts") or []
    if isinstance(raw_mounts, list):
        for m in raw_mounts:
            if isinstance(m, str) and m.strip():
                mounts.append(m.strip())
            elif isinstance(m, dict):
                h = m.get("host") or m.get("source")
                c = m.get("container") or m.get("target") or m.get("dest")
                mode = m.get("mode", "rw")
                if h and c:
                    mounts.append(f"{h}:{c}:{mode}")

    init_cmds = list(parsed.get("init_commands") or parsed.get("lifecycle_commands") or [])

    return {
        "environment": "docker",
        "docker_image": str(image),
        "docker_workdir": str(workdir),
        "docker_env": {str(k): str(v) for k, v in env.items()},
        "docker_bind_mounts": mounts,
        "timeout": timeout,
        "init_commands": [str(c) for c in init_cmds],
    }


class SandboxStateRegistry:
    """Thread-safe tracker for active and resumed sandbox container states."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registry: Dict[str, Dict[str, Any]] = {}

    def record_state(self, sandbox_id: str, container_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            sid = str(sandbox_id or "default").strip()
            self._registry[sid] = {
                "sandbox_id": sid,
                "container_id": str(container_id).strip(),
                "metadata": dict(metadata or {}),
            }

    def get_state(self, sandbox_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            sid = str(sandbox_id or "default").strip()
            entry = self._registry.get(sid)
            return dict(entry) if entry else None

    def clear(self, sandbox_id: Optional[str] = None) -> None:
        with self._lock:
            if sandbox_id:
                self._registry.pop(str(sandbox_id).strip(), None)
            else:
                self._registry.clear()


SANDBOX_REGISTRY = SandboxStateRegistry()
