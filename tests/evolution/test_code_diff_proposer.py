# -*- coding: utf-8 -*-
"""Unit tests for the code-diff proposal schema + generator (#2613)."""

from evolution.lib.code_diff_proposer import (
    RETRY_POLICY_DIFF_SCHEMA,
    BackoffSpec,
    CodeDiffProposal,
    RetryPolicyChange,
    diff_lines,
    generate_code_diff,
    validate_proposal,
)

# A representative harness retry-policy block (mirrors the shape of
# agent/retry_utils.py's adaptive backoff functions).
_BEFORE = """\
def adaptive_overload_backoff(attempt, *, default_wait, short_attempts=2):
    max_retries = 3
    if attempt <= short_attempts:
        return default_wait, "overload_short"
    base_delay = 20.0
    max_delay = 120.0
    return jittered_backoff(1, base_delay=base_delay, max_delay=max_delay), "overload_long"
"""


def _change(
    *,
    retry_count: int | None = None,
    backoff: BackoffSpec | None = None,
    guard_condition: str | None = None,
) -> RetryPolicyChange:
    return RetryPolicyChange(
        target_file="agent/retry_utils.py",
        symbol="adaptive_overload_backoff",
        before=_BEFORE,
        retry_count=retry_count,
        backoff=backoff,
        guard_condition=guard_condition,
    )


class TestSchema:
    def test_schema_is_valid_json_shape(self):
        assert RETRY_POLICY_DIFF_SCHEMA["type"] == "object"
        assert RETRY_POLICY_DIFF_SCHEMA["title"] == "RetryPolicyCodeDiffProposal"
        # Required fields cover the retry-policy surface + diff delta.
        required = set(RETRY_POLICY_DIFF_SCHEMA["required"])
        for field in (
            "target_file",
            "symbol",
            "before",
            "after",
            "retry_policy",
            "diff_delta",
            "status",
            "requires_human_review",
            "auto_apply",
        ):
            assert field in required
        # Human-gating invariants are hard-coded in the schema.
        assert RETRY_POLICY_DIFF_SCHEMA["properties"]["status"]["const"] == "proposed"
        assert (
            RETRY_POLICY_DIFF_SCHEMA["properties"]["requires_human_review"]["const"]
            is True
        )
        assert RETRY_POLICY_DIFF_SCHEMA["properties"]["auto_apply"]["const"] is False

    def test_schema_retry_policy_surface_fields(self):
        rp = RETRY_POLICY_DIFF_SCHEMA["properties"]["retry_policy"]["properties"]
        assert "retry_count" in rp
        assert "backoff" in rp
        assert "guard_condition" in rp
        backoff = rp["backoff"]["properties"]
        assert "base_delay" in backoff and "max_delay" in backoff


class TestGenerateCodeDiff:
    def test_retry_count_change(self):
        prop = generate_code_diff(_change(retry_count=5))
        assert prop.after != prop.before
        assert "max_retries = 5" in prop.after
        assert "max_retries = 3" not in prop.after
        assert prop.warnings == []
        assert prop.retry_policy["retry_count"] == 5

    def test_backoff_change(self):
        prop = generate_code_diff(
            _change(backoff=BackoffSpec(base_delay=40.0, max_delay=240.0))
        )
        assert "base_delay = 40" in prop.after
        assert "max_delay = 240" in prop.after
        assert prop.retry_policy["backoff"]["base_delay"] == 40.0
        assert prop.retry_policy["backoff"]["max_delay"] == 240.0
        assert prop.warnings == []

    def test_guard_condition_change(self):
        prop = generate_code_diff(
            _change(guard_condition="attempt <= short_attempts + 1")
        )
        assert "if attempt <= short_attempts + 1:" in prop.after
        assert prop.retry_policy["guard_condition"] == "attempt <= short_attempts + 1"

    def test_combined_change(self):
        prop = generate_code_diff(
            _change(
                retry_count=4,
                backoff=BackoffSpec(base_delay=30.0, max_delay=180.0),
                guard_condition="attempt <= 3",
            )
        )
        assert "base_delay = 30" in prop.after
        assert "max_delay = 180" in prop.after
        assert "if attempt <= 3:" in prop.after
        assert prop.retry_policy["retry_count"] == 4

    def test_no_change_when_all_none(self):
        prop = generate_code_diff(_change())
        assert prop.after == prop.before
        assert prop.diff_delta == []
        assert prop.retry_policy == {
            "retry_count": None,
            "backoff": None,
            "guard_condition": None,
        }

    def test_unknown_surface_is_warned_not_guessed(self):
        # A block with no retry-policy surface must not be invented.
        prop = generate_code_diff(
            RetryPolicyChange(
                target_file="x.py",
                symbol="f",
                before="def f():\n    return 1\n",
                retry_count=3,
            )
        )
        assert prop.after == prop.before
        assert any("retry-count surface" in w for w in prop.warnings)

    def test_human_gating_invariants(self):
        prop = generate_code_diff(_change(retry_count=5))
        assert prop.status == "proposed"
        assert prop.requires_human_review is True
        assert prop.auto_apply is False


class TestDiffLines:
    def test_identical_blocks_empty(self):
        assert diff_lines("a\nb\n", "a\nb\n") == []

    def test_single_line_change(self):
        hunks = diff_lines("x = 1\n", "x = 2\n")
        assert len(hunks) == 1
        assert hunks[0]["old_count"] == 1
        assert hunks[0]["new_count"] == 1
        assert hunks[0]["lines"][0]["type"] == "remove"
        assert hunks[0]["lines"][1]["type"] == "add"

    def test_insertion(self):
        hunks = diff_lines("a\nc\n", "a\nb\nc\n")
        assert any(
            line["type"] == "add" and line["text"] == "b"
            for hunk in hunks
            for line in hunk["lines"]
        )


class TestValidateProposal:
    def test_valid_proposal_passes(self):
        prop = generate_code_diff(
            _change(backoff=BackoffSpec(base_delay=40.0, max_delay=240.0))
        )
        assert validate_proposal(prop.to_dict()) == []

    def test_missing_required_field(self):
        d = generate_code_diff(_change(retry_count=5)).to_dict()
        del d["target_file"]
        errors = validate_proposal(d)
        assert any("target_file" in e for e in errors)

    def test_wrong_type_discriminator(self):
        d = generate_code_diff(_change(retry_count=5)).to_dict()
        d["type"] = "system_prompt_delta"
        errors = validate_proposal(d)
        assert any("type" in e for e in errors)

    def test_auto_apply_must_be_false(self):
        d = generate_code_diff(_change(retry_count=5)).to_dict()
        d["auto_apply"] = True
        errors = validate_proposal(d)
        assert any("auto_apply" in e for e in errors)

    def test_negative_retry_count_rejected(self):
        d = generate_code_diff(_change(retry_count=5)).to_dict()
        d["retry_policy"]["retry_count"] = -1
        errors = validate_proposal(d)
        assert any("retry_count" in e for e in errors)

    def test_non_dict_rejected(self):
        assert validate_proposal("not a dict") != []


class TestSerialization:
    def test_proposal_roundtrip(self):
        prop = generate_code_diff(
            _change(
                retry_count=4,
                backoff=BackoffSpec(base_delay=30.0, max_delay=180.0),
            )
        )
        restored = CodeDiffProposal.from_dict(prop.to_dict())
        assert restored.target_file == prop.target_file
        assert restored.after == prop.after
        assert restored.retry_policy == prop.retry_policy
        assert restored.diff_delta == prop.diff_delta
        assert restored.auto_apply is False
