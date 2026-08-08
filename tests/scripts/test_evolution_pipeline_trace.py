"""Tests for pipeline state trace recorder (#1269)."""

import json
from pathlib import Path
from scripts.evolution_pipeline_trace import PipelineTraceRecorder


def test_pipeline_trace_recorder(tmp_path: Path):
    trace_file = tmp_path / "pipeline-traces.jsonl"
    recorder = PipelineTraceRecorder(trace_file=trace_file)

    trace = recorder.record_stage(
        stage_name="analysis",
        sub_goal="Prioritize issues for iteration",
        tool_calls=["read_file", "grep_search"],
        result_summary="Selected issue #1269 for implementation",
        verification_status="sufficient",
        turn_index=1,
    )

    assert trace.stage_name == "analysis"
    assert trace.verification_status == "sufficient"
    assert trace_file.exists()

    with open(trace_file) as f:
        data = json.loads(f.readline())
        assert data["stage_name"] == "analysis"
        assert data["turn_index"] == 1
