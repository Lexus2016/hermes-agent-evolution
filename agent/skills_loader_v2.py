"""Skills loader v2 — graph-ordered (topological) skill loading (issue #298).

The long-standing skills loader (``agent/prompt_builder.build_skills_system_prompt``
cold path) scans ``SKILL.md`` files in **directory order** — a flat listing with
no notion of dependencies between skills.  Issue #246 (Skills-as-Graph / AIP)
added :mod:`agent.skill_graph`, which models typed ``requires`` /
``conflicts-with`` / ``composes-with`` / ``deprecates`` edges and can compute a
dependency-first :meth:`~agent.skill_graph.SkillGraph.topological_order`.

This module is the **opt-in** bridge between the two: given the same skills
directories the flat loader scans, it builds the graph at startup, computes the
topological load order (dependencies before dependents), and returns the
``SKILL.md`` paths in that order so the existing per-skill loading logic can run
unchanged.  It is wired behind a **default-OFF** flag
(``skills.skills_loader_v2`` in config.yaml, or ``HERMES_SKILLS_LOADER_V2`` in
the environment).  When the flag is off, the loader is never invoked and skill
loading is byte-identical to today.

Design constraints (this changes how skills load for EVERY profile):

* **Never drop a skill.**  A ``SKILL.md`` that the graph cannot place (a skill
  with no graph frontmatter, a legacy manifest, a file whose name the graph
  didn't register) is emitted in its original flat (directory) order *after*
  every graph-ordered file.  The set of returned files is always exactly the
  set the flat scan would have produced.
* **Reuse, don't reimplement.**  All graph logic lives in
  :mod:`agent.skill_graph`; this module only maps skill *names* back to their
  ``SKILL.md`` *paths* and orders the file list.  The caller still does the
  actual reading/parsing/indexing via the existing flat code path.
* **Fail clear on cycles.**  A ``requires`` cycle has no valid load order;
  :func:`ordered_skill_files` raises :class:`SkillRequiresCycleError` naming the
  cycle so the caller can log it and fall back to the flat loader rather than
  silently loading in an arbitrary order.
* **Warn on capability conflicts.**  When the graph reports a capability
  provided by two or more loaded skills (``GraphValidation.capability_conflicts``,
  issue #299), :func:`ordered_skill_files` logs a load-time WARNING naming the
  capability and its providers.  This is advisory only — it never changes the
  load order or the returned file set, and (being on the v2 path) it never fires
  when the flag is off.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.skill_graph import SkillGraph
from agent.skill_utils import iter_skill_index_files, parse_frontmatter

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")

# Env var name and config key for the opt-in flag (kept in sync with the docs
# in this module's docstring and the call site in agent/prompt_builder.py).
ENV_FLAG = "HERMES_SKILLS_LOADER_V2"
CONFIG_KEY = "skills_loader_v2"


class SkillRequiresCycleError(Exception):
    """Raised when the skill graph has a ``requires`` cycle (no load order).

    A ``requires`` cycle means "A needs B which (transitively) needs A": there
    is no order in which every skill loads after its prerequisites.  The error
    message names every detected cycle so the operator can fix the offending
    ``requires`` edges.  The caller is expected to catch this and fall back to
    the flat loader so skill loading never silently breaks.
    """

    def __init__(self, cycles: List[List[str]]):
        self.cycles = cycles
        rendered = "; ".join(" -> ".join(c + [c[0]]) for c in cycles if c)
        super().__init__(
            f"skill graph has {len(cycles)} requires cycle(s): {rendered}"
        )


def v2_enabled() -> bool:
    """Return True when the v2 ordered loader is opted in.

    Resolution order (first decisive wins):

    1. ``HERMES_SKILLS_LOADER_V2`` env var — an explicit truthy/falsy value
       overrides config (handy for tests and one-off runs).
    2. ``skills.skills_loader_v2`` in config.yaml.
    3. Default **False** — flat loading, byte-identical to pre-#298 behavior.

    Any read failure falls back to False (never enable the new path by
    accident).
    """
    raw_env = os.getenv(ENV_FLAG)
    if raw_env is not None:
        val = raw_env.strip().lower()
        if val in _TRUTHY:
            return True
        if val in _FALSY:
            return False
        # Unrecognized env value: ignore it and consult config.

    try:
        from agent.skill_preprocessing import load_skills_config

        cfg = load_skills_config()
        return bool(cfg.get(CONFIG_KEY, False))
    except Exception:  # noqa: BLE001 - config read must never break startup
        logger.debug("skills_loader_v2: could not read config flag", exc_info=True)
        return False


def _warn_capability_conflicts(
    validation, skills_dir: Path
) -> List[Tuple[str, List[str]]]:
    """Log a load-time warning for each ambiguously-provided capability (#299).

    A *capability conflict* is a capability (an abstract ability declared via
    ``provides``) that two or more loaded skills offer — resolving it is then
    ambiguous.  This is advisory, not an error: interchangeable providers are
    often legitimate.  The detection itself lives in
    :meth:`agent.skill_graph.SkillGraph.validate`
    (``GraphValidation.capability_conflicts``); this only surfaces the result as
    a log line at load time, the remaining slice of #299.

    Takes the already-computed :class:`~agent.skill_graph.GraphValidation` so it
    reuses the graph the caller already built — it never rebuilds anything.
    Returns the conflicts it warned about (empty when there are none), purely so
    callers/tests can introspect; the side effect is the log line(s).
    """
    conflicts = validation.capability_conflicts
    for capability, providers in conflicts:
        logger.warning(
            "skills_loader_v2: capability %r is provided by %d skills (%s) in "
            "%s — resolution is ambiguous (capability conflict)",
            capability,
            len(providers),
            ", ".join(providers),
            skills_dir,
        )
    return conflicts


def _name_for_skill_file(skill_file: Path, skills_dir: Path) -> str:
    """Resolve the graph node name for a ``SKILL.md`` path.

    Mirrors :meth:`SkillGraph.from_skills_dirs`: the node name is the
    frontmatter ``name`` if present, else the skill's directory name.  Parse
    failures fall back to the directory name — the file is still returned by
    :func:`ordered_skill_files`, just ordered as an un-graphed leaf.
    """
    try:
        frontmatter, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        name = str(frontmatter.get("name") or skill_file.parent.name).strip()
        return name or skill_file.parent.name
    except Exception:  # noqa: BLE001 - one bad file is non-fatal
        return skill_file.parent.name


def ordered_skill_files(skills_dir: Path) -> List[Path]:
    """Return *skills_dir*'s ``SKILL.md`` paths in topological load order.

    Builds a :class:`~agent.skill_graph.SkillGraph` over *skills_dir*, validates
    it, and orders the discovered ``SKILL.md`` files so that every skill loads
    after the skills it ``requires``.

    Fallback / safety guarantees:

    * The returned list contains **exactly** the same files
      :func:`agent.skill_utils.iter_skill_index_files` would yield — never more,
      never fewer.  Skills the graph cannot place (no graph frontmatter, legacy
      manifest, name collision) are appended in their original flat order after
      the graph-ordered files, so nothing is ever dropped.
    * On a ``requires`` cycle the function raises
      :class:`SkillRequiresCycleError` (named cycle) — it does **not** silently
      pick an arbitrary order.  Callers fall back to the flat scan.

    Raises:
        SkillRequiresCycleError: the graph has at least one ``requires`` cycle.
    """
    # The authoritative file list — flat, directory order. This is the set the
    # legacy loader walks; we only ever reorder it, never add/remove.
    flat_files: List[Path] = list(iter_skill_index_files(skills_dir, "SKILL.md"))
    if not flat_files:
        return flat_files

    graph = SkillGraph.from_skills_dirs([skills_dir])

    # Hard error: a requires cycle has no valid load order. Surface it clearly
    # rather than emitting an arbitrary order.
    validation = graph.validate()
    if validation.requires_cycles:
        raise SkillRequiresCycleError(validation.requires_cycles)

    # Load-time governance warning (issue #299): a capability provided by 2+
    # skills is ambiguous to resolve. Advisory only — it never changes the load
    # order or the returned file set. Reuses the validation computed just above.
    _warn_capability_conflicts(validation, skills_dir)

    # Map each discovered file to its graph node name. A name may map to several
    # files if two skill dirs share a frontmatter name; the graph keeps only the
    # first (local-dir precedence), so later duplicates are treated as un-graphed
    # and preserved in flat order — never dropped.
    name_to_files: Dict[str, List[Path]] = {}
    file_names: Dict[Path, str] = {}
    for f in flat_files:
        name = _name_for_skill_file(f, skills_dir)
        file_names[f] = name
        name_to_files.setdefault(name, []).append(f)

    topo_names = graph.topological_order()

    ordered: List[Path] = []
    placed: set = set()

    # 1) Emit files in topological (dependency-first) order. For a name that the
    #    graph registered, emit its first matching file once.
    for name in topo_names:
        files = name_to_files.get(name)
        if not files:
            continue
        first = files[0]
        if first in placed:
            continue
        ordered.append(first)
        placed.add(first)

    # 2) Append every remaining file (un-graphed skills, duplicate-name files,
    #    legacy manifests) in their original flat order. This is the fallback
    #    that guarantees no skill is ever lost.
    for f in flat_files:
        if f not in placed:
            ordered.append(f)
            placed.add(f)

    return ordered


def ordered_skill_files_or_flat(skills_dir: Path) -> List[Path]:
    """Topological order when the graph allows it, else the flat order.

    Convenience wrapper used at the call site: returns
    :func:`ordered_skill_files` but, on a :class:`SkillRequiresCycleError`,
    logs the named cycle and returns the plain flat scan so skill loading still
    succeeds.  Any other unexpected failure also falls back to flat — the v2
    loader must never be able to break skill loading for a profile.
    """
    try:
        return ordered_skill_files(skills_dir)
    except SkillRequiresCycleError as exc:
        logger.error(
            "skills_loader_v2: %s — falling back to flat (directory-order) "
            "skill loading for %s",
            exc,
            skills_dir,
        )
    except Exception:  # noqa: BLE001 - defensive: never break loading
        logger.warning(
            "skills_loader_v2: unexpected error ordering skills in %s; "
            "falling back to flat loading",
            skills_dir,
            exc_info=True,
        )
    return list(iter_skill_index_files(skills_dir, "SKILL.md"))


def iter_skill_files_for_load(skills_dir: Path) -> List[Path]:
    """The single entry point the flat loader calls to get skill files.

    * Flag OFF (default): identical to ``list(iter_skill_index_files(skills_dir,
      "SKILL.md"))`` — byte-identical to the pre-#298 loader.
    * Flag ON: graph-ordered (topological) load order, with the flat fallback
      for un-graphed skills and on cycle errors.
    """
    if not v2_enabled():
        return list(iter_skill_index_files(skills_dir, "SKILL.md"))
    return ordered_skill_files_or_flat(skills_dir)
