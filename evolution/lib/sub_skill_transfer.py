"""Cross-task sub-skill schema, extraction, retrieval, and transfer (issues #3067, #3070, #3071).

Defines reusable execution primitives extracted from completed tasks, enabling
cross-task skill transfer with strict provenance tracking and a human-in-the-loop
approval gate before promotion to active policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import uuid

from hermes_constants import get_hermes_home
from evolution.lib.skill_reuse_gate import scan_skill_for_misevolution

logger = logging.getLogger(__name__)

_DEFAULT_SUB_SKILLS_RELPATH = "evolution/sub_skills.jsonl"


@dataclass
class SubSkillProvenance:
    session_id: str
    task_id: str
    extracted_at: int
    extractor_version: str = "1.0.0"
    source_tools: list[str] = field(default_factory=list)
    commit_sha: Optional[str] = None


@dataclass
class SubSkill:
    sub_skill_id: str
    name: str
    description: str
    preconditions: list[str]
    action_template: str
    provenance: SubSkillProvenance
    status: str = "candidate"
    human_reviewed: bool = False
    confidence_score: float = 0.8
    usage_count: int = 0
    success_count: int = 0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubSkill:
        prov_data = data.get("provenance", {})
        prov = SubSkillProvenance(**prov_data) if isinstance(prov_data, dict) else prov_data
        return cls(
            sub_skill_id=data["sub_skill_id"],
            name=data["name"],
            description=data["description"],
            preconditions=list(data.get("preconditions", [])),
            action_template=data["action_template"],
            provenance=prov,
            status=data.get("status", "candidate"),
            human_reviewed=bool(data.get("human_reviewed", False)),
            confidence_score=float(data.get("confidence_score", 0.8)),
            usage_count=int(data.get("usage_count", 0)),
            success_count=int(data.get("success_count", 0)),
            tags=list(data.get("tags", [])),
        )

    def validate_safety(self) -> tuple[bool, Optional[str]]:
        content_to_scan = f"{self.name}\n{self.description}\n{self.action_template}"
        is_safe, reasons = scan_skill_for_misevolution(content_to_scan)
        if not is_safe:
            reason = ", ".join(reasons) if reasons else "failed safety scan"
            return False, reason
        return True, None


class SubSkillExtractor:
    def __init__(self, min_trace_length: int = 2):
        self.min_trace_length = min_trace_length

    def extract_from_trace(
        self,
        session_id: str,
        task_id: str,
        actions: Sequence[dict[str, Any]],
        task_description: str = "",
        success: bool = True,
    ) -> list[SubSkill]:
        if not success or len(actions) < self.min_trace_length:
            return []

        candidates: list[SubSkill] = []
        now = int(time.time())
        tool_names = [a.get("tool", "") for a in actions if isinstance(a, dict)]

        if "terminal" in tool_names or "read_file" in tool_names:
            sub_id = f"subskill-{uuid.uuid4().hex[:8]}"
            preconds = [f"domain: {task_description[:30].strip() or 'general'}"]
            
            error_keywords = ["error", "fail", "flak", "timeout", "lock", "concurren"]
            for a in actions:
                output_str = str(a.get("output", "") or a.get("inputs", ""))
                for kw in error_keywords:
                    if kw in output_str.lower():
                        preconds.append(f"condition: {kw}")
                        break

            steps = []
            for i, a in enumerate(actions[:5]):
                t = a.get("tool", "action")
                steps.append(f"Step {i+1}: invoke {t}")
            template = "\n".join(steps)

            skill = SubSkill(
                sub_skill_id=sub_id,
                name=f"resolve_{task_id[:16]}",
                description=f"Extracted workflow for: {task_description[:60]}",
                preconditions=list(set(preconds)),
                action_template=template,
                provenance=SubSkillProvenance(
                    session_id=session_id,
                    task_id=task_id,
                    extracted_at=now,
                    source_tools=list(set(tool_names)),
                ),
                status="candidate",
                human_reviewed=False,
                confidence_score=0.85,
            )

            is_safe, reason = skill.validate_safety()
            if is_safe:
                candidates.append(skill)
            else:
                skill.status = "quarantined"
                logger.warning("Extracted sub-skill %s quarantined: %s", sub_id, reason)

        return candidates


class SubSkillStore:
    def __init__(self, store_path: Optional[Path] = None):
        self.store_path = store_path or (get_hermes_home() / _DEFAULT_SUB_SKILLS_RELPATH)
        self._skills: dict[str, SubSkill] = {}
        self._load()

    def _load(self) -> None:
        self._skills.clear()
        if not self.store_path.exists():
            return
        try:
            for line in self.store_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    skill = SubSkill.from_dict(data)
                    self._skills[skill.sub_skill_id] = skill
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Failed to load sub-skills from %s: %s", self.store_path, exc)

    def _persist(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(s.to_dict(), sort_keys=True) for s in self._skills.values()]
        self.store_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def add(self, skill: SubSkill) -> None:
        is_safe, _reason = skill.validate_safety()
        if not is_safe:
            skill.status = "quarantined"
        self._skills[skill.sub_skill_id] = skill
        self._persist()

    def get(self, sub_skill_id: str) -> Optional[SubSkill]:
        return self._skills.get(sub_skill_id)

    def list_all(self, status: Optional[str] = None) -> list[SubSkill]:
        if status:
            return [s for s in self._skills.values() if s.status == status]
        return list(self._skills.values())

    def promote(self, sub_skill_id: str, approver: str = "human") -> bool:
        skill = self._skills.get(sub_skill_id)
        if not skill or skill.status == "quarantined":
            return False

        skill.status = "approved"
        skill.human_reviewed = True
        self._persist()
        logger.info("Sub-skill %s approved by %s", sub_skill_id, approver)
        return True

    def quarantine(self, sub_skill_id: str, reason: str = "") -> bool:
        skill = self._skills.get(sub_skill_id)
        if not skill:
            return False
        skill.status = "quarantined"
        self._persist()
        return True

    def retrieve_matching_subskills(
        self,
        query: str,
        preconditions: Optional[Sequence[str]] = None,
        top_k: int = 3,
        approved_only: bool = True,
    ) -> list[SubSkill]:
        candidates = [
            s for s in self._skills.values()
            if (not approved_only or s.status in ("approved", "promoted"))
        ]

        scored: list[tuple[float, SubSkill]] = []
        query_terms = set(re.findall(r"\w+", query.lower()))
        filter_preconds = {p.lower() for p in (preconditions or [])}

        for skill in candidates:
            match_score = 0.0
            skill_preconds = [p.lower() for p in skill.preconditions]
            for sp in skill_preconds:
                if sp in filter_preconds:
                    match_score += 2.0
                for term in query_terms:
                    if term in sp:
                        match_score += 0.5

            desc_terms = set(re.findall(r"\w+", skill.description.lower()))
            overlap = query_terms.intersection(desc_terms)
            match_score += len(overlap) * 0.2

            if match_score > 0.0 or not filter_preconds:
                scored.append((match_score, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:top_k]]
