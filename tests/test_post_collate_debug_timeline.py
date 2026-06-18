"""Deferred post-collate tools appear under Post-merge collation in debug events."""

from __future__ import annotations

import pandas as pd

from sia.agent.post_collate_transforms import apply_deferred_post_collate_transforms
from sia.debug.llm_observer import LLMObserver


def test_apply_deferred_logs_aggregate_weekly_on_observer():
    df = pd.DataFrame(
        {
            "calendar_date": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-08", "2024-01-09"]
            ),
            "channel": ["A", "A", "B", "B"],
            "spends": [10.0, 10.0, 20.0, 20.0],
        }
    )
    deferred = [
        {
            "tool": "transform.aggregate_weekly",
            "params": {
                "date_col": "calendar_date",
                "group_by_cols": ["channel"],
                "metric_rules": {"spends": "sum"},
            },
        }
    ]
    obs = LLMObserver(run_id="test-collation", enabled=True)
    with obs.trace_span("collation.post_collate_transforms", type="node"):
        out, trace = apply_deferred_post_collate_transforms(df, deferred, observer=obs)
    assert out is not None
    assert len(trace) == 1
    events = obs.get_hierarchical_events()
    tool_rows = [
        e
        for e in events
        if e.get("process_type") == "tool_execution"
        and e.get("module") == "transform.aggregate_weekly"
    ]
    assert len(tool_rows) == 1
    assert tool_rows[0].get("snapshot_path") or tool_rows[0].get("output_preview")


def test_post_collate_final_layout_uses_template_column_order():
    """Combined frame reorder must not use LLM partial column_order like ['spends']."""
    from sia.agent.post_collate_transforms import apply_combined_frame_final_layout

    df = pd.DataFrame(
        [
            {
                "SPENDS": 1.0,
                "DATE": "2026-01-02",
                "PUBLISHER": "x",
                "CHANNEL": "tv",
                "IMPRESSIONS": 2,
            }
        ]
    )
    template = {
        "x_scope": {
            "uid_hierarchy": ["date", "publisher", "channel"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": [],
        },
        "properties": {
            "date": {"format": "date"},
            "publisher": {},
            "channel": {},
            "spends": {},
            "impressions": {},
        },
    }
    out, trace = apply_combined_frame_final_layout(
        df,
        target_template=template,
        approved_mappings=[
            {"source_column": "DATE", "target_column": "date", "decision": "keep"},
        ],
    )
    assert list(out.columns) == ["DATE", "PUBLISHER", "CHANNEL", "SPENDS", "IMPRESSIONS"]
    assert len(trace) == 2
    assert all(t.get("ok") for t in trace)


def test_link_deferred_under_collation_root():
    from web_server import _link_deferred_collation_debug_events

    col_dbg = [
        {
            "event_id": "root-1",
            "parent_event_id": None,
            "module": "collation.merge_sources",
            "process_type": "node",
            "source_id": "__collation__",
        },
        {
            "event_id": "union-1",
            "parent_event_id": "root-1",
            "module": "collation.union_stack",
            "process_type": "tool_execution",
            "source_id": "__collation__",
        },
    ]
    deferred_dbg = [
        {
            "event_id": "pc-node",
            "parent_event_id": None,
            "module": "collation.post_collate_transforms",
            "process_type": "node",
        },
        {
            "event_id": "agg-1",
            "parent_event_id": "pc-node",
            "module": "transform.aggregate_weekly",
            "process_type": "tool_execution",
        },
    ]
    linked = _link_deferred_collation_debug_events(col_dbg, deferred_dbg)
    by_id = {e["event_id"]: e for e in linked}
    assert by_id["pc-node"]["parent_event_id"] == "root-1"
    assert by_id["agg-1"]["parent_event_id"] == "pc-node"
    assert by_id["agg-1"]["source_id"] == "__collation__"
