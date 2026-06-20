"""Tests for optional-skills/quality/self-critique/scripts/self_critique.py (#372)"""

import json
import sys
from pathlib import Path

import pytest

# Add the scripts dir so we can import the module directly.
SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "quality"
    / "self-critique"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import self_critique  # noqa: E402


def _fn(payload: dict):
    """Build a critique_fn that returns a fixed JSON string, ignoring kwargs."""
    text = json.dumps(payload)

    def _inner(messages, **kwargs):
        return text

    return _inner


# ── Verdict scenarios ─────────────────────────────────────────────────────────


class TestVerdicts:
    def test_satisfied(self):
        result = self_critique.critique(
            "Add a logout button to the navbar.",
            "Added a logout button to the navbar; it clears the session.",
            critique_fn=_fn({"verdict": "satisfied", "missing_items": [],
                             "suggested_follow_up": ""}),
        )
        assert result["verdict"] == "satisfied"
        assert result["missing_items"] == []

    def test_partial_scope_omission(self):
        result = self_critique.critique(
            "Export the report as CSV and email it to the team.",
            "Exported the report as report.csv.",
            critique_fn=_fn({
                "verdict": "partial",
                "missing_items": ["did not email the report to the team"],
                "suggested_follow_up": "Send report.csv to the team mailing list.",
            }),
        )
        assert result["verdict"] == "partial"
        assert any("email" in m for m in result["missing_items"])
        assert result["suggested_follow_up"]

    def test_missing_hallucinated_completion(self):
        result = self_critique.critique(
            "Fix the failing CI build.",
            "All set, the build is green now!",
            tool_trace_json='[{"tool": "terminal", "result": "1 test failed"}]',
            critique_fn=_fn({
                "verdict": "missing",
                "missing_items": ["build still failing: 1 test failed in trace"],
                "suggested_follow_up": "Investigate the failing test before claiming success.",
            }),
        )
        assert result["verdict"] == "missing"
        assert result["missing_items"]


# ── Parsing / coercion robustness ─────────────────────────────────────────────


class TestParsing:
    def test_handles_json_code_fence(self):
        fenced = "```json\n{\"verdict\": \"partial\", \"missing_items\": [\"x\"]}\n```"

        def fn(messages, **kwargs):
            return fenced

        result = self_critique.critique("ask", "answer", critique_fn=fn)
        assert result["verdict"] == "partial"
        assert result["missing_items"] == ["x"]

    def test_extracts_object_from_chatty_text(self):
        chatty = 'Here is my audit:\n{"verdict": "satisfied", "missing_items": []}\nDone.'

        def fn(messages, **kwargs):
            return chatty

        result = self_critique.critique("ask", "answer", critique_fn=fn)
        assert result["verdict"] == "satisfied"

    def test_extracts_object_after_stray_brace_literal(self):
        # A stray '{...}' before the real object must not break extraction.
        noisy = 'note: {tbd}\n{"verdict": "partial", "missing_items": ["x"]}'

        def fn(messages, **kwargs):
            return noisy

        result = self_critique.critique("ask", "answer", critique_fn=fn)
        assert result["verdict"] == "partial"
        assert result["missing_items"] == ["x"]

    def test_unrecognized_verdict_becomes_unknown(self):
        result = self_critique.critique(
            "ask", "answer",
            critique_fn=_fn({"verdict": "maybe-ish", "missing_items": []}),
        )
        assert result["verdict"] == "unknown"

    def test_unparseable_response_becomes_unknown(self):
        def fn(messages, **kwargs):
            return "this is not json at all"

        result = self_critique.critique("ask", "answer", critique_fn=fn)
        assert result["verdict"] == "unknown"

    def test_satisfied_with_missing_items_downgraded_to_partial(self):
        # Consistency guard: contradictory verdict is corrected, not trusted.
        result = self_critique.critique(
            "ask", "answer",
            critique_fn=_fn({"verdict": "satisfied",
                             "missing_items": ["actually one thing is unmet"]}),
        )
        assert result["verdict"] == "partial"


# ── Safe degradation ──────────────────────────────────────────────────────────


class TestDegradation:
    def test_auditor_exception_returns_unknown(self):
        def fn(messages, **kwargs):
            raise RuntimeError("no provider configured")

        result = self_critique.critique("ask", "answer", critique_fn=fn)
        assert result["verdict"] == "unknown"
        assert "no provider configured" in result["suggested_follow_up"]

    def test_empty_request_returns_unknown(self):
        result = self_critique.critique("", "answer", critique_fn=_fn({}))
        assert result["verdict"] == "unknown"

    def test_empty_response_returns_unknown(self):
        result = self_critique.critique("ask", "", critique_fn=_fn({}))
        assert result["verdict"] == "unknown"

    def test_critique_fn_without_kwargs_is_supported(self):
        # An injected fn that only accepts (messages) must still work.
        def fn(messages):
            return json.dumps({"verdict": "satisfied", "missing_items": []})

        result = self_critique.critique("ask", "answer", critique_fn=fn)
        assert result["verdict"] == "satisfied"


# ── CLI ───────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_main_reads_file_and_prints_json(self, tmp_path, capsys, monkeypatch):
        payload = {
            "original_request": "Add a logout button.",
            "final_response": "Added a logout button.",
        }
        infile = tmp_path / "audit.json"
        infile.write_text(json.dumps(payload), encoding="utf-8")

        monkeypatch.setattr(
            self_critique, "_default_critique_fn",
            lambda messages, **kw: json.dumps(
                {"verdict": "satisfied", "missing_items": []}
            ),
        )
        rc = self_critique.main(["--input", str(infile)])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "satisfied"

    def test_main_bad_input_returns_2(self, tmp_path, capsys):
        rc = self_critique.main(["--input", str(tmp_path / "missing.json")])
        assert rc == 2
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "unknown"


# ── Message construction ──────────────────────────────────────────────────────


class TestMessages:
    def test_build_messages_includes_all_sections(self):
        msgs = self_critique.build_messages(
            "the ask", "the answer", '{"tool": "x"}'
        )
        assert msgs[0]["role"] == "system"
        user = msgs[1]["content"]
        assert "ORIGINAL REQUEST" in user and "the ask" in user
        assert "FINAL RESPONSE" in user and "the answer" in user
        assert "TOOL TRACE" in user

    def test_build_messages_omits_empty_trace(self):
        msgs = self_critique.build_messages("the ask", "the answer", None)
        assert "TOOL TRACE" not in msgs[1]["content"]

    def test_long_fields_are_truncated(self):
        big = "x" * 50000
        msgs = self_critique.build_messages("ask", big, None)
        assert "truncated" in msgs[1]["content"]
