"""Multi-source grain tools must defer until after collation duplicate_check."""

from __future__ import annotations

import pandas as pd
import pytest

import web_server as ws
from sia.agent.base import ExtractionPlan
from sia.agent.nodes import (
    POST_COLLATE_DEFERRED_GRAIN_TOOLS,
    _inject_infer_daily_before_aggregate_weekly,
    _partition_deferred_post_collate_grain_tools,
    _should_defer_weekly_rollup_until_post_collate,
)
from sia.agent.planner import PlanGenerator
from sia.agent.post_collate_transforms import apply_deferred_post_collate_transforms
from sia.debug.llm_observer import init_observer, clear_observer
from sia.debug.state_snapshot import build_state_snapshot, log_state_snapshot


def test_defer_when_multi_source_without_union_relationship():
    state = {
        "multi_source_active_batch": True,
        "approved_relationships": [{"relationship_kind": "independent", "source_ids": ["a", "b"]}],
    }
    assert _should_defer_weekly_rollup_until_post_collate(state) is True


def test_no_defer_for_single_source_batch():
    state = {"multi_source_active_batch": False, "approved_relationships": []}
    assert _should_defer_weekly_rollup_until_post_collate(state) is False


def test_partition_defers_infer_and_aggregate():
    tools = [
        {"tool": "transform.rename", "params": {"mapping": {"x": "date"}}},
        {
            "tool": "transform.infer_granularity_expand_to_daily",
            "params": {"date_col": "date", "value_cols": ["spends"]},
        },
        {
            "tool": "transform.aggregate_weekly",
            "params": {"date_col": "calendar_date", "metric_rules": {"spends": "sum"}},
        },
        {"tool": "verify.schema", "params": {"schema": {"type": "object"}}},
    ]
    state = {"multi_source_active_batch": True}
    kept, deferred = _partition_deferred_post_collate_grain_tools(tools, state)
    kept_names = [t["tool"] for t in kept]
    deferred_names = [t["tool"] for t in deferred]
    assert "transform.rename" in kept_names
    assert "verify.schema" in kept_names
    assert "transform.infer_granularity_expand_to_daily" in deferred_names
    assert "transform.aggregate_weekly" in deferred_names


def test_inject_skipped_when_multi_source_defer():
    tools = [
        {
            "tool": "transform.aggregate_weekly",
            "params": {"date_col": "date", "metric_rules": {"spends": "sum"}},
        },
    ]
    state = {
        "multi_source_active_batch": True,
        "target_template": {"x_scope": {"date_granularity": "weekly"}},
    }
    # Simulate execute_tools: no inject when deferring
    if not _should_defer_weekly_rollup_until_post_collate(state):
        tools = _inject_infer_daily_before_aggregate_weekly(tools, state)
    kept, deferred = _partition_deferred_post_collate_grain_tools(tools, state)
    assert len(deferred) == 1
    assert deferred[0]["tool"] == "transform.aggregate_weekly"
    assert not any(t.get("tool") == "transform.infer_granularity_expand_to_daily" for t in kept)


def test_post_collate_runs_infer_then_aggregate():
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-15", "2024-02-15"]),
            "channel": ["tv", "tv"],
            "spends": [300.0, 600.0],
        }
    )
    deferred = [
        {
            "tool": "transform.infer_granularity_expand_to_daily",
            "params": {
                "date_col": "date",
                "value_cols": ["spends"],
                "date_column": "calendar_date",
            },
        },
        {
            "tool": "transform.aggregate_weekly",
            "params": {
                "date_col": "calendar_date",
                "group_by_cols": ["channel"],
                "metric_rules": {"spends": "sum"},
            },
        },
    ]
    out, trace = apply_deferred_post_collate_transforms(df, deferred)
    assert out is not None
    assert len(out) >= 1
    assert len(trace) == 2
    assert all(t.get("ok") for t in trace)
    for name in POST_COLLATE_DEFERRED_GRAIN_TOOLS:
        assert name  # constant referenced


def test_resumed_source_deferred_tools_are_recorded_for_post_collate():
    class FakeTrace:
        deferred_post_collate_tools = [
            {
                "tool": "transform.aggregate_weekly",
                "params": {"date_col": "calendar_date", "metric_rules": {"spends": "sum"}},
            }
        ]

    job = {}

    ws._record_resumed_source_deferred_tools(
        job,
        source_id="src-a",
        sheet_name="Sheet A",
        processing_sheet="Sheet A_clean",
        trace=FakeTrace(),
    )

    assert job["_deferred_post_collate_by_source"]["src-a"][0]["tool"] == "transform.aggregate_weekly"
    assert job["source_execution_registry"][0]["deferred_post_collate_tools_count"] == 1


def test_context_packet_defer_flag_defers_grain_tools_at_execution_boundary():
    state = {"context_packet": {"defer_weekly_rollups_to_post_union": True}}
    assert _should_defer_weekly_rollup_until_post_collate(state) is True

    tools = [
        {"tool": "transform.rename", "params": {"mapping": {"Date": "date"}}},
        {
            "tool": "transform.infer_granularity_expand_to_daily",
            "params": {"date_col": "date", "value_cols": ["spends"]},
        },
        {
            "tool": "transform.aggregate_weekly",
            "params": {"date_col": "calendar_date", "metric_rules": {"spends": "sum"}},
        },
        {"tool": "verify.schema", "params": {"schema": {"type": "object"}}},
    ]

    kept, deferred = _partition_deferred_post_collate_grain_tools(tools, state)

    assert [t["tool"] for t in kept] == ["transform.rename", "verify.schema"]
    assert [t["tool"] for t in deferred] == [
        "transform.infer_granularity_expand_to_daily",
        "transform.aggregate_weekly",
    ]


def test_planner_finalizer_preserves_grain_tools_as_post_collate_intent():
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {"start_row": 1, "end_row": 2, "start_col": 1, "end_col": 2, "header_row": 1}},
            {
                "tool": "transform.aggregate_weekly",
                "params": {"date_col": "calendar_date", "metric_rules": {"spends": "sum"}},
            },
            {"tool": "verify.schema", "params": {"schema": {"type": "object"}}},
        ],
        reasoning="",
    )

    out = PlanGenerator(llm_client=None).finalize_plan(
        plan,
        context_packet={"defer_weekly_rollups_to_post_union": True},
        target_template={},
    )

    names = [t["tool"] for t in out.tool_calls]
    assert "transform.aggregate_weekly" in names


def test_state_snapshot_exposes_deferral_flags_without_full_dataframe():
    df = pd.DataFrame({"calendar_date": ["2024-01-01"], "spends": [10]})
    snapshot = build_state_snapshot(
        label="state.execute_tools.deferral",
        phase="deferral_decision",
        state={
            "source_id": "src-a",
            "multi_source_active_batch": True,
            "current_df": df,
            "suggested_tools": [{"tool": "transform.aggregate_weekly"}],
            "deferred_post_collate_tools": [{"tool": "transform.aggregate_weekly"}],
            "context_packet": {"defer_weekly_rollups_to_post_union": True},
        },
        decision={"defer_grain": True},
    )

    assert snapshot["source"]["source_id"] == "src-a"
    assert snapshot["agent_state"]["run"]["multi_source_active_batch"] is True
    assert snapshot["context"]["deferrals"]["weekly_rollups_to_post_union"] is True
    assert snapshot["agent_state"]["frames"]["current"] == {"rows": 1, "cols": 2}
    assert snapshot["agent_state"]["plan"]["deferred_post_collate_tools"] == ["transform.aggregate_weekly"]
    assert snapshot["decision"]["grain_and_tools"]["defer_grain"] is True
    assert "calendar_date" not in str(snapshot)


def test_observer_state_snapshots_are_separate_from_trace_events():
    observer = init_observer("run-state-test", enabled=True)
    try:
        with observer.trace_span("execute_tools", type="node"):
            log_state_snapshot(
                "state.execute_tools.deferral",
                "deferral_decision",
                state={
                    "multi_source_active_batch": True,
                    "context_packet": {"defer_weekly_rollups_to_post_union": True},
                },
                decision={"defer_grain": True},
            )

        snapshots = observer.get_state_snapshots()
        events = observer.get_hierarchical_events()

        assert len(snapshots) == 1
        assert snapshots[0]["anchor_event_id"] == events[0]["event_id"]
        assert all(e.get("process_type") != "state_snapshot" for e in events)
    finally:
        clear_observer()


def test_job_state_snapshot_records_memory_ledger():
    job = {
        "processing_route": "multi_sheet",
        "_deferred_post_collate_by_source": {
            "src-a": [{"tool": "transform.aggregate_weekly", "params": {}}],
        },
        "source_registry": [{"source_id": "src-a"}],
    }

    ws._append_job_state_snapshot(
        job,
        label="job.multi_source.collation",
        phase="after_duplicate_check_before_deferred",
        source_id="__collation__",
        context_packet={"defer_weekly_rollups_to_post_union": True},
        decision={"frame_sources": ["src-a", "src-b"]},
    )

    assert job["state_snapshots"][0]["memory"]["source_registry_count"] == 1
    assert job["state_snapshots"][0]["memory"]["deferred_by_source"]["src-a"] == [
        "transform.aggregate_weekly"
    ]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
