"""Tests for scripts/introspection_codebook.py — step-level failure codebook (#3508)."""

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from introspection_codebook import (
    CODEBOOK,
    DEFAULT_CONFIDENCE_THRESHOLD,
    STEP_TYPES,
    StepAnnotation,
    aggregate_step_annotations,
    classify_step_heuristics,
    compare_cycle_counts,
    get_step_failure_classes,
    infer_step_type,
    is_class_suppressed,
    is_valid_failure_class,
    load_suppression_registry,
    prelabel_step,
    prelabel_steps,
    record_filed_class,
    sanitize_evidence,
    save_suppression_registry,
    suppress_filed_classes,
)
from introspection_extract import build_digest, scan_session


class TestCodebookDefinition:
    def test_step_types_exist_and_partitioned(self):
        assert set(STEP_TYPES) == {"delegation", "tool_call", "gate_decision"}
        for st in STEP_TYPES:
            assert st in CODEBOOK
            meta = CODEBOOK[st]
            assert "description" in meta
            assert "failure_classes" in meta
            classes = get_step_failure_classes(st)
            assert len(classes) >= 5
            # Each step type has an explicit catch-all
            expected_catchall = {
                "delegation": "other_delegation_failure",
                "tool_call": "other_tool_failure",
                "gate_decision": "other_gate_failure",
            }[st]
            assert expected_catchall in classes
            # Classes are unique
            assert len(classes) == len(set(classes))

    def test_class_validity_check(self):
        assert is_valid_failure_class("tool_call", "precondition_failed")
        assert is_valid_failure_class("delegation", "recursion_limit")
        assert is_valid_failure_class("gate_decision", "evidence_mismatch")
        assert not is_valid_failure_class("tool_call", "non_existent_class")
        assert not is_valid_failure_class("unknown_type", "precondition_failed")


class TestPrelabelingHeuristics:
    def test_delegation_step_classification(self):
        step_rec = {
            "step_id": "del-1",
            "step_type": "delegation",
            "error": "recursion limit reached in subagent call hierarchy",
        }
        ann = prelabel_step(step_rec)
        assert ann.step_type == "delegation"
        assert ann.failure_class == "recursion_limit"
        assert ann.canonical_tag == "delegation.recursion_limit"
        assert ann.confidence >= DEFAULT_CONFIDENCE_THRESHOLD
        assert ann.escalated is False

    def test_tool_call_step_classification(self):
        step_rec = {
            "step_id": "tool-1",
            "step_type": "tool_call",
            "tool": "read_file",
            "error": "no such file or directory: /path/to/missing.txt",
        }
        ann = prelabel_step(step_rec)
        assert ann.step_type == "tool_call"
        assert ann.failure_class == "precondition_failed"
        assert ann.canonical_tag == "tool_call.precondition_failed"
        assert ann.confidence >= DEFAULT_CONFIDENCE_THRESHOLD
        assert ann.escalated is False

    def test_gate_decision_step_classification(self):
        step_rec = {
            "step_id": "gate-1",
            "step_type": "gate_decision",
            "gate_name": "evidence_gate",
            "reason": "evidence_type_mismatch: unbacked claim detected",
        }
        ann = prelabel_step(step_rec)
        assert ann.step_type == "gate_decision"
        assert ann.failure_class == "evidence_mismatch"
        assert ann.canonical_tag == "gate_decision.evidence_mismatch"
        assert ann.confidence >= DEFAULT_CONFIDENCE_THRESHOLD
        assert ann.escalated is False

    def test_unclassified_failure_escalation(self):
        step_rec = {
            "step_id": "unknown-1",
            "step_type": "tool_call",
            "tool": "custom_tool",
            "error": "some completely bizarre unexplained glitch xyz",
        }
        ann = prelabel_step(step_rec)
        assert ann.failure_class == "other_tool_failure"
        assert ann.confidence < DEFAULT_CONFIDENCE_THRESHOLD
        assert ann.escalated is True


class TestModelPrelabelingHook:
    def test_model_fn_override(self):
        def mock_model(step_dict, codebook):
            return {
                "failure_class": "context_overflow",
                "confidence": 0.94,
                "rationale": "Evaluated prompt tokens exceeded model context boundary",
            }

        step_rec = {
            "step_id": "step-sub",
            "step_type": "delegation",
            "error": "token limit exceeded",
        }
        ann = prelabel_step(step_rec, model_fn=mock_model)
        assert ann.failure_class == "context_overflow"
        assert ann.confidence == 0.94
        assert ann.escalated is False
        assert "Evaluated prompt tokens" in ann.rationale

    def test_model_fn_low_confidence_triggers_escalation(self):
        def low_conf_model(step_dict, codebook):
            return {
                "failure_class": "subagent_crash",
                "confidence": 0.65,
                "rationale": "Ambiguous error trace",
            }

        step_rec = {
            "step_id": "step-sub-2",
            "step_type": "delegation",
            "error": "opaque worker exit",
        }
        ann = prelabel_step(step_rec, model_fn=low_conf_model)
        assert ann.failure_class == "subagent_crash"
        assert ann.confidence == 0.65
        assert ann.escalated is True


class TestCycleComparisonAndAggregation:
    def test_aggregation(self):
        steps = [
            {"step_id": "1", "step_type": "tool_call", "error": "file not found"},
            {"step_id": "2", "step_type": "tool_call", "error": "file not found"},
            {"step_id": "3", "step_type": "tool_call", "error": "timed out"},
            {"step_id": "4", "step_type": "gate_decision", "error": "evidence_type_mismatch"},
        ]
        annotations = prelabel_steps(steps)
        counts = aggregate_step_annotations(annotations)
        assert counts["tool_call.precondition_failed"] == 2
        assert counts["tool_call.timeout"] == 1
        assert counts["gate_decision.evidence_mismatch"] == 1

    def test_compare_cycle_counts(self):
        prev = {
            "tool_call.precondition_failed": 10,
            "tool_call.timeout": 5,
            "delegation.timeout": 3,
        }
        curr = {
            "tool_call.precondition_failed": 12,
            "tool_call.timeout": 2,
            "gate_decision.safety_violation": 4,
        }
        res = compare_cycle_counts(prev, curr)
        assert "gate_decision.safety_violation" in res["new_classes"]
        assert "delegation.timeout" in res["resolved_classes"]
        assert res["deltas"]["tool_call.precondition_failed"]["status"] == "increased"
        assert res["deltas"]["tool_call.timeout"]["status"] == "decreased"
        assert res["total_failures_previous"] == 18
        assert res["total_failures_current"] == 18


class TestSuppressionRegistry:
    def test_suppression_workflow(self, tmp_path):
        reg_file = tmp_path / "suppression_registry.json"
        registry = load_suppression_registry(reg_file)
        assert registry == {}

        record_filed_class(registry, "tool_call.precondition_failed", issue_number=3508)
        save_suppression_registry(reg_file, registry)

        loaded = load_suppression_registry(reg_file)
        assert is_class_suppressed("tool_call.precondition_failed", loaded)
        assert not is_class_suppressed("tool_call.timeout", loaded)

        counts = {
            "tool_call.precondition_failed": 15,
            "tool_call.timeout": 4,
        }
        filtered = suppress_filed_classes(counts, loaded)
        assert "tool_call.precondition_failed" not in filtered
        assert filtered == {"tool_call.timeout": 4}


class TestSanitizeEvidencePrivacy:
    def test_sanitize_evidence_drops_secrets(self):
        raw = {
            "step_id": "s-123",
            "tool": "terminal",
            "exit_code": 1,
            "error": "SECRET USER TOKEN abcdef12345",
            "output": "PRIVATE USER SENSITIVE CONTENT",
            "reason": "non-zero-exit",
        }
        cleaned = sanitize_evidence(raw)
        assert "error" not in cleaned
        assert "output" not in cleaned
        assert "SECRET USER TOKEN" not in json.dumps(cleaned)
        assert cleaned["tool"] == "terminal"
        assert cleaned["exit_code"] == 1
        assert cleaned["reason"] == "non-zero-exit"


class TestIntrospectionExtractIntegration:
    def test_build_digest_emits_step_annotations(self, tmp_path):
        sess_file = tmp_path / "session_1.jsonl"
        lines = [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "read_file"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"error": "No such file: config.yaml"})},
            {"role": "assistant", "tool_calls": [{"id": "c2", "function": {"name": "terminal"}}]},
            {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"output": "", "exit_code": 1, "error": "command not found"})},
        ]
        sess_file.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

        digest = build_digest(tmp_path, window_days=7)
        assert "step_failure_classes" in digest["signals"]
        classes = digest["signals"]["step_failure_classes"]
        assert "tool_call.precondition_failed" in classes
        assert "step_annotations_summary" in digest
        summary = digest["step_annotations_summary"]
        assert summary["total_steps_annotated"] >= 2
        assert "failure_class_counts" in summary
