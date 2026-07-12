"""A2A (Agent2Agent) Agent Card model and builder.

Slice 1 of #748 (issue #879): expose Hermes' existing tools and skills as
an A2A Agent Card served at ``/.well-known/agent.json``.

This is a read-only *discovery view*. It registers **no** new core tools --
all capability lives at the edge (the ``skills/a2a/`` scaffold + this thin
model). The card only reflects tools/skills Hermes already has.

The serialized shape follows the A2A protocol Agent Card (camelCase JSON):
``name``, ``description``, ``url``, ``version``, ``capabilities``,
``authentication``, ``provider`` and a ``skills[]`` array -- see
https://google.github.io/A2A/ . Each Hermes tool and each ``SKILL.md`` skill
maps to one entry in ``skills[]``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Repo root is two levels up: ``hermes_cli/a2a.py`` -> ``<repo>/``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "skills" / "a2a" / "config.yaml"
_SKILLS_ROOT = _REPO_ROOT / "skills"

# Defaults used when ``skills/a2a/config.yaml`` is absent or partial. The
# config file overlays these keys (see that file for the documented schema).
_DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "name": "Hermes",
    "description": "Hermes agent advertising its tools and skills over A2A.",
    "url": "/a2a",
    "provider": {
        "organization": "NousResearch",
        "url": "https://github.com/NousResearch/hermes",
    },
    "authentication": {"schemes": ["bearer"]},
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    },
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
    "expose": {"tools": True, "skills": True, "max_skills": 128},
}


@dataclass
class AgentSkill:
    """One advertised competency -- an A2A ``skill`` entry.

    Maps a Hermes tool or ``SKILL.md`` skill into the Agent Card's
    ``skills[]`` array.
    """

    id: str
    name: str
    description: str = ""
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
        }


@dataclass
class AgentCard:
    """A2A Agent Card. Serialized to JSON at ``/.well-known/agent.json``."""

    name: str
    description: str
    url: str
    version: str
    skills: List[AgentSkill] = field(default_factory=list)
    capabilities: Dict[str, bool] = field(default_factory=dict)
    authentication: Dict[str, Any] = field(default_factory=dict)
    provider: Optional[Dict[str, str]] = None
    default_input_modes: List[str] = field(default_factory=lambda: ["text"])
    default_output_modes: List[str] = field(default_factory=lambda: ["text"])

    def to_dict(self) -> Dict[str, Any]:
        card: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": dict(self.capabilities),
            "authentication": dict(self.authentication),
            "defaultInputModes": list(self.default_input_modes),
            "defaultOutputModes": list(self.default_output_modes),
            "skills": [skill.to_dict() for skill in self.skills],
        }
        if self.provider:
            card["provider"] = dict(self.provider)
        return card


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Return the A2A config, overlaying ``skills/a2a/config.yaml`` on defaults."""
    cfg: Dict[str, Any] = {**_DEFAULT_CONFIG}
    cfg_path = path or _CONFIG_PATH
    try:
        import yaml

        if cfg_path.is_file():
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                cfg.update(loaded)
    except Exception:  # pragma: no cover - config read is best-effort
        logger.debug("A2A: could not read %s; using defaults", cfg_path, exc_info=True)
    return cfg


def collect_capabilities(config: Dict[str, Any]) -> List[AgentSkill]:
    """Gather Hermes tools + ``SKILL.md`` skills as A2A skills.

    Side-effect free: reads the live tool registry (populated once tool
    modules are imported, e.g. by the running dashboard) and scans the
    ``skills/`` directory. Tools are listed first; the ``max_skills`` cap
    bounds the total.
    """
    expose = config.get("expose") or {}
    exclude = {str(x) for x in (expose.get("exclude") or [])}
    skills: List[AgentSkill] = []
    seen: set[str] = set()

    if expose.get("tools", True):
        for name, desc, toolset in _iter_registered_tools():
            if name in seen or name in exclude:
                continue
            seen.add(name)
            skills.append(
                AgentSkill(
                    id=name,
                    name=name,
                    description=desc,
                    tags=[t for t in (toolset,) if t],
                )
            )

    if expose.get("skills", True):
        for name, desc, category in _iter_skill_docs():
            skill_id = f"skill:{name}"
            if skill_id in seen or skill_id in exclude or name in exclude:
                continue
            seen.add(skill_id)
            skills.append(
                AgentSkill(
                    id=skill_id,
                    name=name,
                    description=desc,
                    tags=[t for t in ("skill", category) if t],
                )
            )

    max_skills = expose.get("max_skills")
    if isinstance(max_skills, int) and max_skills >= 0:
        skills = skills[:max_skills]
    return skills


# The discovery endpoint is public, so we must not re-read config + re-scan
# skills/**/SKILL.md on every request (a cheap resource-amplification vector,
# and needless disk I/O). The card content is request-independent and changes
# rarely, so cache the (config, capabilities) snapshot for a short TTL.
_SNAPSHOT_TTL = 30.0
_snapshot_lock = threading.Lock()
_snapshot: Optional[Tuple[float, Dict[str, Any], List[AgentSkill]]] = None


def get_discovery_snapshot(
    ttl: float = _SNAPSHOT_TTL,
) -> Tuple[Dict[str, Any], List[AgentSkill]]:
    """Return the cached ``(config, capabilities)`` snapshot, rebuilding it at
    most once per *ttl* seconds. Thread-safe (double-checked locking)."""
    global _snapshot
    snap = _snapshot
    if snap is not None and (time.monotonic() - snap[0]) < ttl:
        return snap[1], snap[2]
    with _snapshot_lock:
        snap = _snapshot
        if snap is not None and (time.monotonic() - snap[0]) < ttl:
            return snap[1], snap[2]
        config = load_config()
        caps = collect_capabilities(config)
        _snapshot = (time.monotonic(), config, caps)
        return config, caps


def reset_discovery_cache() -> None:
    """Drop the cached snapshot (config/skills changes, tests)."""
    global _snapshot
    with _snapshot_lock:
        _snapshot = None


def build_agent_card(
    config: Optional[Dict[str, Any]] = None,
    capabilities: Optional[List[AgentSkill]] = None,
    *,
    version: Optional[str] = None,
    base_url: Optional[str] = None,
) -> AgentCard:
    """Build an :class:`AgentCard` from *config* and collected *capabilities*.

    ``capabilities`` may be injected (tests); otherwise it is collected live.
    ``base_url`` (e.g. the request's scheme+host) is prepended to a relative
    ``url`` so the card advertises an absolute A2A endpoint; an absolute
    ``url`` in config is left untouched.
    """
    cfg = config if config is not None else load_config()
    caps = capabilities if capabilities is not None else collect_capabilities(cfg)

    url = str(cfg.get("url", "/a2a"))
    if base_url and url.startswith("/"):
        url = base_url.rstrip("/") + url

    return AgentCard(
        name=cfg.get("name", "Hermes"),
        description=cfg.get("description", ""),
        url=url,
        version=version or cfg.get("version") or _hermes_version(),
        skills=caps,
        capabilities=cfg.get("capabilities", {}),
        authentication=cfg.get("authentication", {}),
        provider=cfg.get("provider"),
        default_input_modes=cfg.get("defaultInputModes", ["text"]),
        default_output_modes=cfg.get("defaultOutputModes", ["text"]),
    )


def _iter_registered_tools() -> Iterator[Tuple[str, str, str]]:
    """Yield ``(name, description, toolset)`` for each Hermes tool.

    Prefers the live tool registry (populated in a full agent process, so
    descriptions are rich). When the registry is empty -- e.g. a bare
    dashboard web server that never built a toolset -- falls back to the
    static ``toolsets.py`` definitions so the card still advertises Hermes'
    tools. Both paths are side-effect free (the fallback never imports tool
    handlers).
    """
    seen = False
    try:
        from tools.registry import registry

        for name in registry.get_all_tool_names():
            entry = registry.get_entry(name)
            if entry is None:
                continue
            desc = (entry.description or "").strip()
            if not desc:
                desc = str((entry.schema or {}).get("description", "")).strip()
            seen = True
            yield name, _first_line(desc), (entry.toolset or "")
    except Exception:  # pragma: no cover - registry is optional at read time
        logger.debug("A2A: tool registry unavailable", exc_info=True)

    if seen:
        return

    try:
        import toolsets

        static_map: Dict[str, str] = {}
        for toolset in toolsets.TOOLSETS:
            try:
                names = toolsets.resolve_toolset(toolset, include_registry=False)
            except Exception:  # pragma: no cover - defensive per-toolset
                continue
            for name in names:
                static_map.setdefault(name, toolset)
        for name in sorted(static_map):
            yield name, "", static_map[name]
    except Exception:  # pragma: no cover - static toolsets unavailable
        logger.debug("A2A: static toolsets unavailable", exc_info=True)


def _iter_skill_docs() -> Iterator[Tuple[str, str, str]]:
    """Yield ``(name, description, category)`` from ``skills/**/SKILL.md``."""
    if not _SKILLS_ROOT.is_dir():
        return
    try:
        import yaml
    except Exception:  # pragma: no cover - yaml is a hard dep in practice
        return
    for skill_md in sorted(_SKILLS_ROOT.rglob("SKILL.md")):
        meta = _read_frontmatter(skill_md, yaml)
        name = str(meta.get("name") or skill_md.parent.name)
        desc = _first_line(str(meta.get("description", "")).strip())
        category = ""
        metadata = meta.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("hermes"), dict):
            category = str(metadata["hermes"].get("category", "") or "")
        yield name, desc, category


def _read_frontmatter(path: Path, yaml_mod: Any) -> Dict[str, Any]:
    """Parse a ``SKILL.md`` YAML frontmatter block into a dict (or ``{}``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:  # pragma: no cover - unreadable file, skip
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml_mod.safe_load(text[3:end])
    except Exception:  # pragma: no cover - malformed frontmatter, skip
        return {}
    return data if isinstance(data, dict) else {}


def _first_line(text: str, limit: int = 240) -> str:
    """First non-empty line of *text*, trimmed to *limit* chars."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0].strip()[:limit]


def _hermes_version() -> str:
    try:
        from hermes_cli import __version__

        return str(__version__)
    except Exception:  # pragma: no cover
        return "0.0.0"
