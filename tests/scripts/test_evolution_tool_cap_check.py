"""Checked lowering — resource-bound tool verification (#77, SkillEffect).

S1 (scripts/evolution_tool_cap_check.py): pure per-tool input-cap checker.
S2 (scripts/evolution_skill_lint.py): the real call site — declared_inputs
blocks in SKILL.md frontmatter are run through the cap checker before a skill
is trusted for reuse. The real-repo enforcement test (no cap_* violations on
the current corpus) is the merge blocker, mirroring the #101/#188 pattern.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_skill_lint import (
    extract_declared_inputs,
    find_cap_violations,
    lint_repo,
)
from evolution_tool_cap_check import (
    check_input_caps,
    declared_input_size,
    load_caps,
)


TINY_CAPS = {
    "read_file": {"max_bytes": 100, "max_args": 2},
    "default": {"max_bytes": 1000, "max_args": 8},
}


class TestRealRepoEnforcement:
    def test_real_repo_has_no_cap_violations(self):
        # No skill currently declares oversized inputs; if one ever does, CI
        # blocks the merge — the #77 acceptance criterion, enforced.
        violations = [v for v in lint_repo() if v["kind"].startswith("cap_")]
        assert violations == [], f"cap violations: {violations}"


class TestDeclaredInputSize:
    def test_counts_strings_and_keys(self):
        assert declared_input_size({"path": "abc"}) == 4 + 3  # key + value

    def test_counts_other_types_via_str(self):
        assert declared_input_size({"limit": 50}) == 5 + 2

    def test_empty_and_none(self):
        assert declared_input_size({}) == 0
        assert declared_input_size(None) == 0


class TestCheckInputCaps:
    def test_within_caps_ok(self):
        assert check_input_caps("read_file", {"path": "a.txt"}, caps=TINY_CAPS) == []

    def test_oversized_input_rejected(self):
        v = check_input_caps("read_file", {"path": "x" * 200}, caps=TINY_CAPS)
        assert len(v) == 1
        assert v[0]["kind"] == "input_too_large"
        assert v[0]["tool"] == "read_file"
        assert v[0]["declared"] == "204"  # len("path") + len("x"*200)

    def test_too_many_args_rejected(self):
        v = check_input_caps(
            "read_file", {"path": "a", "limit": 1, "extra": 2}, caps=TINY_CAPS
        )
        assert len(v) == 1 and v[0]["kind"] == "too_many_args"

    def test_unknown_tool_checked_against_default(self):
        assert check_input_caps("mystery_tool", {"x": "y"}, caps=TINY_CAPS) == []
        # default cap is 1000 bytes: 1200 chars exceeds it, 500 chars does not.
        assert check_input_caps("mystery_tool", {"x": "y" * 500}, caps=TINY_CAPS) == []
        v = check_input_caps("mystery_tool", {"x": "y" * 1200}, caps=TINY_CAPS)
        assert len(v) == 1 and v[0]["kind"] == "input_too_large"

    def test_no_default_no_caps_is_ok(self):
        assert check_input_caps("anything", {"a": 1}, caps={}) == []

    def test_load_caps_returns_table(self):
        caps = load_caps()
        assert "default" in caps
        assert caps["default"]["max_args"] > 0


class TestExtractDeclaredInputs:
    def _skill(self, block):
        return f"---\nname: evolution-demo\n{block}---\n# body\n"

    def test_absent_block_yields_empty(self):
        assert extract_declared_inputs("---\nname: x\n---\n") == []
        assert extract_declared_inputs("no frontmatter at all") == []

    def test_list_form(self):
        text = self._skill(
            "declared_inputs:\n"
            "  - tool: read_file\n"
            "    args: {path: 'data/big.txt'}\n"
            "  - {tool: search_files, args: {pattern: '*.py'}}\n"
        )
        out = extract_declared_inputs(text)
        assert out == [
            {"tool": "read_file", "args": {"path": "data/big.txt"}},
            {"tool": "search_files", "args": {"pattern": "*.py"}},
        ]

    def test_map_form(self):
        text = self._skill("declared_inputs:\n  read_file: {path: 'a.txt'}\n")
        assert extract_declared_inputs(text) == [
            {"tool": "read_file", "args": {"path": "a.txt"}}
        ]

    def test_malformed_yaml_is_empty_not_crash(self):
        assert (
            extract_declared_inputs("---\nname: x\ndeclared_inputs: [unclosed\n---\n")
            == []
        )


class TestFindCapViolations:
    def test_oversized_declaration_flagged(self):
        decl = [{"tool": "read_file", "args": {"path": "x" * 200}}]
        v = find_cap_violations(decl, caps=TINY_CAPS)
        assert len(v) == 1
        assert v[0]["kind"] == "cap_input_too_large"
        assert v[0]["tool"] == "read_file"

    def test_within_caps_ok(self):
        decl = [{"tool": "read_file", "args": {"path": "a.txt"}}]
        assert find_cap_violations(decl, caps=TINY_CAPS) == []

    def test_empty_and_malformed_ok(self):
        assert find_cap_violations([], caps=TINY_CAPS) == []
        assert find_cap_violations([{"no_tool": 1}], caps=TINY_CAPS) == []
