"""Tests for tools/llm_router.py — five-component LLM routing + xRouteBench (issue #2317)."""

import pytest

from tools.llm_router import (
    BenchTask,
    ContextEncoder,
    DecisionRule,
    LLMRouter,
    LearningSignalCollector,
    ModelEncoder,
    ModelInfo,
    RoutingContext,
    RoutingOutcome,
    ScoringFunction,
    xRouteBench,
)


def _sample_models():
    return [
        {
            "name": "fast-cheap",
            "provider": "p1",
            "context_window": 8000,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": False,
            "cost_per_1k": 0.1,
            "latency_ms": 200,
        },
        {
            "name": "big-reasoner",
            "provider": "p2",
            "context_window": 128000,
            "supports_tools": True,
            "supports_vision": True,
            "supports_reasoning": True,
            "cost_per_1k": 2.0,
            "latency_ms": 1500,
        },
    ]


def test_context_encoder_extracts_request_features():
    enc = ContextEncoder()
    ctx = enc.encode({"prompt": "x" * 100, "tools": [{"name": "calc"}], "images": ["i"]})
    assert isinstance(ctx, RoutingContext)
    assert ctx.prompt_length == 100
    assert ctx.has_tools is True
    assert ctx.has_vision is True
    assert ctx.estimated_tokens == 25  # len(prompt) // 4


def test_model_encoder_maps_metadata():
    enc = ModelEncoder()
    m = enc.encode({"name": "m", "provider": "p", "max_tokens": 4096, "tool_use": True})
    assert isinstance(m, ModelInfo)
    assert m.name == "m"
    assert m.context_window == 4096
    assert m.supports_tools is True


def test_scorer_penalizes_missing_required_capability():
    scorer = ScoringFunction()
    ctx = RoutingContext(has_tools=True, estimated_tokens=10)
    model = ModelInfo(name="m", provider="p", supports_tools=False)
    assert scorer.score(ctx, model) < 0


def test_decision_rule_argmax_and_min_score():
    rule = DecisionRule()
    assert rule.decide({"a": 1.0, "b": 5.0}) == "b"
    assert rule.decide({}) is None
    strict = DecisionRule(min_score=10.0)
    assert strict.decide({"a": 5.0}) is None


def test_router_routes_to_capable_model():
    router = LLMRouter(models=_sample_models())
    chosen = router.route({"prompt": "describe this image", "images": ["data:..."]})
    assert chosen == "big-reasoner"  # only it supports vision


def test_router_returns_none_when_no_model_fits():
    router = LLMRouter(models=_sample_models())
    # A request needing reasoning + vision + huge context still fits big-reasoner,
    # so instead force a hard miss by requiring a capability nobody has.
    chosen = router.route({"prompt": "x", "estimated_tokens": 10_000_000})
    assert chosen is None


def test_learning_signal_collector_accuracy():
    col = LearningSignalCollector()
    col.record(RoutingOutcome(request={}, selected_model="a", success=True))
    col.record(RoutingOutcome(request={}, selected_model="b", success=False))
    assert col.accuracy() == 0.5
    assert len(col.all()) == 2


def test_xroutebench_evaluates_attribute_accuracy():
    router = LLMRouter(models=_sample_models())
    tasks = [
        BenchTask(
            request={"prompt": "x" * 200, "tools": [{"name": "calc"}]},
            expected_attrs={"supports_tools": True},
        ),
        BenchTask(
            request={"prompt": "describe this image", "images": ["data:..."]},
            expected_attrs={"supports_vision": True},
        ),
        BenchTask(
            request={"prompt": "think step by step", "reasoning": True},
            expected_attrs={"supports_reasoning": True},
        ),
    ]
    result = xRouteBench(router, tasks).evaluate()
    assert result["total"] == 3
    assert result["correct"] == 3
    assert result["accuracy"] == 1.0


def test_xroutebench_default_tasks_smoke():
    router = LLMRouter(models=_sample_models())
    result = xRouteBench(router).evaluate()
    assert result["total"] == 4
    assert 0.0 <= result["accuracy"] <= 1.0
