# -*- coding: utf-8 -*-
"""Unit tests for task-decoupled planning (#1138)."""

from __future__ import annotations

import pytest

from agent.task_decoupling import (
    SubGoal,
    SubGoalDAG,
    TaskDecouplingConfig,
    confined_replan,
    decompose_task,
    scoped_context,
    should_decouple,
)


# --- DAG model ----------------------------------------------------------------


def test_dag_topological_order():
    dag = SubGoalDAG([
        SubGoal("a", "first"),
        SubGoal("b", "second", dependencies=("a",)),
        SubGoal("c", "third", dependencies=("b",)),
    ])
    assert dag.topological_order() == ["a", "b", "c"]


def test_dag_rejects_unknown_dependency():
    with pytest.raises(ValueError):
        SubGoalDAG([SubGoal("a", "x", dependencies=("ghost",))])


def test_dag_rejects_cycle():
    with pytest.raises(ValueError):
        SubGoalDAG([
            SubGoal("a", "x", dependencies=("b",)),
            SubGoal("b", "y", dependencies=("a",)),
        ])


def test_dag_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        SubGoalDAG([SubGoal("a", "x"), SubGoal("a", "y")])


def test_dag_ancestors_transitive():
    dag = SubGoalDAG([
        SubGoal("a", "first"),
        SubGoal("b", "second", dependencies=("a",)),
        SubGoal("c", "third", dependencies=("b",)),
    ])
    assert dag.ancestors("c") == {"a", "b"}
    assert dag.ancestors("a") == set()


# --- decomposition ------------------------------------------------------------


def test_decompose_numbered_steps_into_linear_chain():
    task = "1. read the config\n2. transform the data\n3. write the output"
    dag = decompose_task(task)
    order = dag.topological_order()
    assert len(order) == 3
    # linear chain: each depends on the previous
    assert dag.get("g1").dependencies == ("g0",)
    assert dag.get("g2").dependencies == ("g1",)


def test_decompose_single_task_is_one_node():
    dag = decompose_task("just do one thing")
    assert len(dag.nodes) == 1


def test_decompose_with_injected_decomposer():
    def decomposer(task):
        return [SubGoal("x", "a"), SubGoal("y", "b", dependencies=("x",))]

    dag = decompose_task("whatever", decomposer=decomposer)
    assert set(dag.nodes) == {"x", "y"}


# --- scoped context -----------------------------------------------------------


def test_scoped_context_excludes_unrelated_subtask_outputs():
    dag = SubGoalDAG([
        SubGoal("a", "prep", inputs=("cfg",)),
        SubGoal("b", "unrelated"),
        SubGoal("c", "use a", dependencies=("a",)),
    ])
    outputs = {"a": "A_RESULT", "b": "B_RESULT_UNRELATED"}
    ctx = scoped_context(dag, "c", dependency_outputs=outputs, base_inputs={"cfg": "CFG"})
    # c depends on a -> a's output is included; b is unrelated -> excluded
    assert ctx["dependency_outputs"] == {"a": "A_RESULT"}
    assert "b" not in ctx["dependency_outputs"]
    assert ctx["goal"] == "use a"


def test_scoped_context_includes_only_declared_inputs():
    dag = SubGoalDAG([SubGoal("a", "prep", inputs=("cfg",))])
    ctx = scoped_context(dag, "a", base_inputs={"cfg": "CFG", "secret": "SHOULD_NOT_LEAK"})
    assert ctx["inputs"] == {"cfg": "CFG"}
    assert "secret" not in ctx["inputs"]


def test_scoped_context_is_smaller_than_full_trajectory():
    # 5 sub-goals; c depends only on a. The scoped context must be smaller than
    # dumping every sub-goal's output (the linear-loop baseline).
    dag = SubGoalDAG([
        SubGoal("a", "prep"),
        SubGoal("b", "noise1"),
        SubGoal("c", "use a", dependencies=("a",)),
        SubGoal("d", "noise2"),
        SubGoal("e", "noise3"),
    ])
    outputs = {k: "X" * 1000 for k in ("a", "b", "d", "e")}
    ctx = scoped_context(dag, "c", dependency_outputs=outputs)
    scoped_size = len(str(ctx["dependency_outputs"]))
    full_size = len(str(outputs))
    assert scoped_size < full_size


# --- confined replanning ------------------------------------------------------


def test_confined_replan_revises_only_target_node():
    dag = SubGoalDAG([
        SubGoal("a", "first"),
        SubGoal("b", "second", dependencies=("a",)),
        SubGoal("c", "third", dependencies=("b",)),
    ])
    new_dag = confined_replan(dag, "b", revised_description="second (revised)")
    assert new_dag.get("b").description == "second (revised)"
    # siblings/ancestors untouched
    assert new_dag.get("a").description == "first"
    assert new_dag.get("c").description == "third"
    # dependencies preserved
    assert new_dag.get("b").dependencies == ("a",)
    assert new_dag.get("c").dependencies == ("b",)
    # original dag is not mutated
    assert dag.get("b").description == "second"


def test_confined_replan_with_replanner_must_preserve_id_and_deps():
    dag = SubGoalDAG([SubGoal("a", "x"), SubGoal("b", "y", dependencies=("a",))])
    with pytest.raises(ValueError):
        confined_replan(dag, "b", replanner=lambda sg: SubGoal("DIFFERENT", "z"))


def test_confined_replan_with_valid_replanner():
    dag = SubGoalDAG([SubGoal("a", "x"), SubGoal("b", "y", dependencies=("a",))])
    new_dag = confined_replan(
        dag, "b", replanner=lambda sg: SubGoal(sg.id, "revised", dependencies=sg.dependencies)
    )
    assert new_dag.get("b").description == "revised"


# --- config + trigger ---------------------------------------------------------


def test_config_defaults_off():
    assert TaskDecouplingConfig().enabled is False


def test_should_decouple_off_by_default():
    assert should_decouple("x" * 10000, TaskDecouplingConfig()) is False


def test_should_decouple_only_long_tasks_when_enabled():
    cfg = TaskDecouplingConfig(enabled=True, min_task_chars=50)
    assert should_decouple("short task", cfg) is False
    assert should_decouple("x" * 100, cfg) is True


def test_config_from_mapping():
    cfg = TaskDecouplingConfig.from_mapping({"enabled": True, "min_task_chars": 42})
    assert cfg.enabled is True
    assert cfg.min_task_chars == 42
