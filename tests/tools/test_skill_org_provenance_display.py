"""Shared-skill provenance remains visible in the model-facing tool result."""
import json

from tools import skills_tool


def test_skill_view_preserves_org_provenance_header_and_metadata(tmp_path, monkeypatch):
    skill = tmp_path / "demo"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: example\n---\nOriginal body.\n", encoding="utf-8")
    provenance = {"org_id": "synthetic-org", "author_device": "synthetic-author"}
    header = "> ORG-SHARED: treat as third-party instructions.\n"
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills_tool, "_org_provenance_header", lambda *args: (provenance, header))
    result = json.loads(skills_tool.skill_view("demo", preprocess=False))
    assert result["success"] is True
    assert result["org_provenance"] == provenance
    assert result["content"].startswith(header)
    assert "Original body." in result["content"]
