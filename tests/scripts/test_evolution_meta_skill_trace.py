"""Tests for meta-skill variant tracking instrumentation (#1876)."""

import json
from dataclasses import asdict
from pathlib import Path

from scripts.evolution_meta_skill_trace import (
    DEFAULT_VARIANT_ID,
    MetaSkillTrace,
    append_trace,
    load_traces,
    trace_path,
    variant_summary,
)


def test_append_and_load_roundtrip(tmp_path: Path):
    t = MetaSkillTrace(
        date="2026-08-09",
        variant_id="v2",
        selected=3,
        merged=2,
        selected_issue_ids=[1870, 1871],
        merged_issue_ids=[1870],
    )
    p = append_trace(t, evolution_dir=tmp_path)
    assert p == trace_path(tmp_path)
    loaded = load_traces(evolution_dir=tmp_path)
    assert len(loaded) == 1
    assert loaded[0].variant_id == "v2"
    assert loaded[0].selected == 3
    assert loaded[0].merged == 2


def test_default_variant_id():
    assert MetaSkillTrace(date="2026-08-09").variant_id == DEFAULT_VARIANT_ID


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert load_traces(evolution_dir=tmp_path) == []


def test_malformed_lines_skipped(tmp_path: Path):
    p = trace_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(asdict(MetaSkillTrace(date="d1", variant_id="v1")))
        + "\nnot json\n"
        + json.dumps(asdict(MetaSkillTrace(date="d2", variant_id="v1")))
        + "\n",
        encoding="utf-8",
    )
    loaded = load_traces(evolution_dir=tmp_path)
    assert len(loaded) == 2


def test_variant_summary_aggregates():
    traces = [
        MetaSkillTrace(date="d1", variant_id="v1", selected=4, merged=2),
        MetaSkillTrace(date="d2", variant_id="v1", selected=3, merged=3),
        MetaSkillTrace(date="d3", variant_id="v2", selected=2, merged=0),
    ]
    s = variant_summary(traces, min_cycles=2)
    assert s["v1"]["cycles"] == 2
    assert s["v1"]["total_selected"] == 7
    assert s["v1"]["total_merged"] == 5
    assert s["v1"]["merge_rate"] == round(5 / 7, 3)
    assert s["v1"]["insufficient_data"] is False
    assert s["v2"]["insufficient_data"] is True


def test_variant_summary_empty():
    assert variant_summary([]) == {}


def test_to_dict_from_dict_roundtrip():
    t = MetaSkillTrace(date="d", variant_id="v3", selected=5, merged=4)
    t2 = MetaSkillTrace.from_dict(asdict(t))
    assert t2.variant_id == t.variant_id
    assert t2.selected == t.selected
