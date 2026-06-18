"""Tests for HITL graph resume entry routing."""

from sia.agent.graph_resume import (
    can_skip_pipeline_after_load,
    prepare_plan_review_resume_state,
    resolve_resume_graph_entry,
    route_after_load_file,
)


class _FakeGrid:
    pass


def _plan_review_artifacts_state(**extra):
    base = {
        "hitl_resume_from": "plan_review",
        "resume_mode": "use_existing_plan",
        "structure_analysis": {"tables": [{"label": "Main"}]},
        "approved_mappings": [{"source_column": "Spend", "target_column": "spends"}],
        "suggested_tools": [{"tool": "layout.extract"}],
    }
    base.update(extra)
    return base


def test_fresh_run_defaults_to_load_file():
    assert resolve_resume_graph_entry({}) == "load_file"


def test_plan_review_approve_with_artifacts_enters_load_file():
    state = _plan_review_artifacts_state(grid=_FakeGrid())
    assert resolve_resume_graph_entry(state) == "load_file"


def test_prepare_plan_review_clears_grid_and_sets_fast_path():
    state = _plan_review_artifacts_state(grid=_FakeGrid(), current_df="stale")
    out = prepare_plan_review_resume_state(state, materialized_clean_active=True)
    assert out.get("grid") is None
    assert out.get("current_df") is None
    assert out.get("resume_skip_pipeline_after_load") is True
    assert out.get("materialized_clean_active") is True


def test_route_after_load_file_skips_to_execute_tools():
    state = prepare_plan_review_resume_state(_plan_review_artifacts_state())
    assert route_after_load_file(state) == "execute_tools"
    assert can_skip_pipeline_after_load(state) is True


def test_route_after_load_file_normal_when_no_flag():
    state = {
        "structure_analysis": {"tables": []},
        "approved_mappings": [{"target_column": "spends"}],
        "suggested_tools": [{"tool": "layout.extract"}],
    }
    assert route_after_load_file(state) == "analyze_structure"


def test_plan_review_approve_without_artifacts_falls_back_to_load_only():
    state = {
        "hitl_resume_from": "plan_review",
        "resume_mode": "use_existing_plan",
        "suggested_tools": [{"tool": "layout.extract"}],
    }
    assert resolve_resume_graph_entry(state) == "load_file"
    out = prepare_plan_review_resume_state(state)
    assert not out.get("resume_skip_pipeline_after_load")


def test_plan_review_replan_skips_to_generate_plan():
    state = {
        "hitl_resume_from": "plan_review",
        "resume_mode": "replan",
        "grid": _FakeGrid(),
        "structure_analysis": {"confidence": 0.9},
    }
    assert resolve_resume_graph_entry(state) == "generate_plan"


def test_destructive_resume_skips_to_execute_tools():
    state = {
        "hitl_resume_from": "execute_pause",
        "destructive_approved": True,
        "grid": _FakeGrid(),
    }
    assert resolve_resume_graph_entry(state) == "execute_tools"
