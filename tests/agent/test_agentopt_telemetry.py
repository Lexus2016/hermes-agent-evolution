# -*- coding: utf-8 -*-
"""Tests for AgentOpt Slice 1 telemetry hook (#2741)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.agentopt_telemetry import (  # noqa: E402
    agentopt_enabled,
    append_record,
    estimate_cost,
    record_llm_call,
)

ON = {"agentopt": {"telemetry": {"enabled": True}}}


class TestAgentoptEnabled:
    def test_disabled_by_default(self):
        assert agentopt_enabled({}) is False

    def test_enabled_when_flag_set(self):
        assert agentopt_enabled(ON) is True

    def test_false_on_malformed(self):
        assert agentopt_enabled({"agentopt": "oops"}) is False
        assert agentopt_enabled(None) is False


class TestEstimateCost:
    def test_dict_usage(self):
        assert estimate_cost({"prompt_tokens": 500, "completion_tokens": 500}) == 0.002

    def test_object_usage(self):
        assert (
            estimate_cost(SimpleNamespace(prompt_tokens=1000, completion_tokens=0))
            == 0.002
        )


class TestAppendRecord:
    def test_writes_jsonl(self, tmp_path):
        store = tmp_path / "calls.jsonl"
        assert append_record({"model": "m", "tool": "t", "step": "s"}, store=store)
        rec = json.loads(store.read_text().splitlines()[0])
        assert rec["model"] == "m" and rec["outcome"] == "unknown"


class TestRecordLlmCall:
    def test_noop_when_disabled(self, tmp_path):
        store = tmp_path / "c.jsonl"
        assert not record_llm_call(
            model="m",
            tool="t",
            step="s",
            latency_ms=1.0,
            outcome="ok",
            config={},
            store=store,
        )
        assert not store.exists()

    def test_records_when_enabled(self, tmp_path):
        store = tmp_path / "c.jsonl"
        ok = record_llm_call(
            model="m",
            tool="t",
            step="s",
            latency_ms=12.3,
            outcome="ok",
            usage={"prompt_tokens": 1000, "completion_tokens": 0},
            config=ON,
            store=store,
        )
        assert ok is True
        rec = json.loads(store.read_text())
        assert rec["model"] == "m" and rec["cost"] == 0.002 and rec["outcome"] == "ok"
