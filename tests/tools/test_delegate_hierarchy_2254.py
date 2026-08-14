"""Workflow-hierarchy guidance in the delegate_task schema (#2254).

The tool description must teach the cheapest-first hierarchy
(select > generate > edit) with its ordering stated explicitly, so the
model prefers reusing existing structures before spawning new workers.
"""

import tools.delegate_tool as dt


def test_description_states_select_generate_edit_order():
    desc = dt._build_top_level_description()
    assert "WORKFLOW HIERARCHY" in desc
    # Ordering must be explicit and cheapest-first.
    i_select = desc.index("SELECT")
    i_generate = desc.index("GENERATE")
    i_edit = desc.index("EDIT")
    assert i_select < i_generate < i_edit
    assert "select > generate > edit" in desc


def test_description_keeps_reuse_before_spawn_guidance():
    desc = dt._build_top_level_description()
    # SELECT guidance must point at existing assets, GENERATE at new children.
    assert "existing skill" in desc
    assert "only when nothing existing covers the need" in desc
    # EDIT must be bounded to genuine uncertainty.
    assert "genuine uncertainty" in desc
