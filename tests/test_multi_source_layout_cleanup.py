"""Planner injects per-source layout cleanup when sparse dimensions exist without weekly rollup."""

from __future__ import annotations

from sia.agent.base import ExtractionPlan
from sia.agent.planner import PlanGenerator
from sia.tools.tool_validator import normalize_tool_name


def test_sparse_dimension_layout_cleanup_without_aggregate_weekly():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {"tool": "transform.rename", "params": {"mapping": {"channel_paid_media": "channel"}}},
            {"tool": "transform.type_cast", "params": {"columns": {"spends": "float"}}},
        ],
        reasoning="",
    )
    cp = {
        "planning_summary": {
            "source_summary": {
                "sparse_dimension_columns": [
                    {"source_column": "channel_paid_media", "blank_ratio": 0.4},
                ],
            }
        },
    }
    out = gen._ensure_sparse_dimension_layout_cleanup(plan, cp)
    names = [
        normalize_tool_name(str(t.get("tool") or "").strip())[0]
        for t in (out.tool_calls or [])
        if isinstance(t, dict)
    ]
    assert "transform.fill_merged" in names
    assert "transform.aggregate_weekly" not in names
    fill = next(
        t for t in out.tool_calls if t.get("tool") == "transform.fill_merged"
    )
    assert "channel_paid_media" in (fill["params"].get("columns") or [])
    rename_idx = names.index("transform.rename")
    fill_idx = names.index("transform.fill_merged")
    assert fill_idx > rename_idx


def test_sparse_cleanup_skipped_when_merged_ranges_handled_elsewhere():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(tool_calls=[], reasoning="")
    cp = {
        "mapping_supplement": {
            "merged_metric_ranges": [
                {"column_name": "channel_paid_media", "start_row": 0, "end_row": 5},
            ]
        },
        "planning_summary": {
            "source_summary": {
                "sparse_dimension_columns": [
                    {"source_column": "channel_paid_media", "blank_ratio": 0.4},
                ],
            }
        },
    }
    out = gen._ensure_sparse_dimension_layout_cleanup(plan, cp)
    assert not (out.tool_calls or [])


def test_sparse_cleanup_uses_expand_when_block_metrics_detected():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(tool_calls=[{"tool": "layout.extract", "params": {}}], reasoning="")
    cp = {
        "planning_summary": {
            "source_summary": {
                "sparse_dimension_columns": [
                    {"source_column": "channel_paid_media", "blank_ratio": 0.4},
                ],
            }
        },
    }
    out = gen._ensure_sparse_dimension_layout_cleanup(
        plan,
        cp,
        structure_analysis={
            "metric_layout_signals": {
                "block_sparse_metrics": [
                    {"column_label": "spends", "spend_like": True},
                ]
            }
        },
    )
    names = [
        normalize_tool_name(str(t.get("tool") or "").strip())[0]
        for t in (out.tool_calls or [])
        if isinstance(t, dict)
    ]
    assert "transform.expand_grouped_block" in names
