# -*- coding: utf-8 -*-
"""Unit tests for the architecture router (#1139)."""

from __future__ import annotations

from agent.architecture_router import (
    Architecture,
    ArchitectureRouterConfig,
    RouterTelemetry,
    TaskClassification,
    classify_task,
    maybe_route,
    route,
)


# --- classifier ---------------------------------------------------------------


def test_classify_empty_task():
    c = classify_task("")
    assert c.decomposability == 0.0
    assert c.tool_density == 0
    assert c.confidence == 0.0


def test_classify_parallelizable_task_high_decomposability():
    task = (
        "For each of the following companies compare their revenue: "
        "1. Apple\n2. Microsoft\n3. Google\n4. Amazon — analyze all of them respectively"
    )
    c = classify_task(task)
    assert c.decomposability >= 0.6


def test_classify_sequential_task_high_sequentiality():
    task = "First read the config, then transform the data, after that write the output, and finally verify it"
    c = classify_task(task)
    assert c.sequentiality >= 0.6


def test_classify_tool_hint_count_overrides_estimate():
    c = classify_task("do something", tool_hint_count=20)
    assert c.tool_density == 20


# --- routing decision table ---------------------------------------------------


def test_route_low_confidence_defaults_to_single_agent():
    c = TaskClassification(decomposability=0.9, tool_density=2, sequentiality=0.1, confidence=0.2)
    d = route(c, ArchitectureRouterConfig(confidence_threshold=0.5))
    assert d.architecture is Architecture.single_agent
    assert d.max_workers == 1


def test_route_sequential_to_plan_and_execute():
    c = TaskClassification(decomposability=0.2, tool_density=3, sequentiality=0.8, confidence=0.9)
    d = route(c)
    assert d.architecture is Architecture.plan_and_execute
    assert d.max_workers == 1


def test_route_parallelizable_to_centralized_orchestrator():
    c = TaskClassification(decomposability=0.8, tool_density=3, sequentiality=0.1, confidence=0.9)
    d = route(c, ArchitectureRouterConfig(max_workers_cap=4))
    assert d.architecture is Architecture.centralized_orchestrator
    assert 1 < d.max_workers <= 4


def test_route_high_tool_density_caps_workers():
    c = TaskClassification(decomposability=0.9, tool_density=20, sequentiality=0.1, confidence=0.9)
    d = route(c, ArchitectureRouterConfig(max_workers_cap=8, high_tool_density=16))
    # high tool density -> capped at 3 to contain the coordination tax
    assert d.architecture is Architecture.centralized_orchestrator
    assert d.max_workers == 3


def test_route_low_decomposability_single_agent():
    c = TaskClassification(decomposability=0.1, tool_density=2, sequentiality=0.1, confidence=0.9)
    d = route(c)
    assert d.architecture is Architecture.single_agent


def test_route_three_task_types_distinct_architectures():
    parallel = route(TaskClassification(0.8, 3, 0.1, 0.9))
    sequential = route(TaskClassification(0.2, 3, 0.8, 0.9))
    simple = route(TaskClassification(0.1, 1, 0.1, 0.9))
    assert parallel.architecture is Architecture.centralized_orchestrator
    assert sequential.architecture is Architecture.plan_and_execute
    assert simple.architecture is Architecture.single_agent


# --- telemetry ----------------------------------------------------------------


def test_telemetry_records_decision():
    tel = RouterTelemetry()
    d = route(TaskClassification(0.8, 3, 0.1, 0.9))
    tel.record(d, outcome="success")
    assert len(tel) == 1
    e = tel.entries()[0]
    assert e["architecture"] == "centralized_orchestrator"
    assert e["outcome"] == "success"
    assert "classification" in e


def test_telemetry_bounded():
    tel = RouterTelemetry(capacity=3)
    d = route(TaskClassification(0.1, 1, 0.1, 0.9))
    for _ in range(10):
        tel.record(d)
    assert len(tel) == 3


# --- config + seam ------------------------------------------------------------


def test_config_defaults_off():
    assert ArchitectureRouterConfig().enabled is False


def test_maybe_route_disabled_returns_none():
    assert maybe_route("anything", config=ArchitectureRouterConfig(enabled=False)) is None


def test_maybe_route_enabled_returns_and_logs():
    tel = RouterTelemetry()
    task = "First do A, then do B, after that do C, and finally verify"
    d = maybe_route(task, config=ArchitectureRouterConfig(enabled=True), telemetry=tel)
    assert d is not None
    assert len(tel) == 1


def test_maybe_route_never_raises(monkeypatch):
    # Force classify_task to blow up; maybe_route must swallow it.
    import agent.architecture_router as ar

    monkeypatch.setattr(ar, "classify_task", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert maybe_route("t", config=ArchitectureRouterConfig(enabled=True)) is None
