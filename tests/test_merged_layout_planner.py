"""Planner injects expand_grouped_block when Excel merged ranges exist."""

from __future__ import annotations

from sia.agent.base import ExtractionPlan
from sia.agent.planner import PlanGenerator


def test_ensure_expand_when_merged_ranges_and_no_block_tools():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "transform.rename", "params": {"mapping": {"total_cost_paid_media": "spends"}}},
            {"tool": "transform.type_cast", "params": {"columns": {"spends": "float"}}},
        ],
        reasoning="",
    )
    cp = {
        "mapping_supplement": {
            "merged_metric_ranges": [
                {
                    "column_name": "channel_paid_media",
                    "start_row": 0,
                    "end_row": 15,
                    "top_left_value": "TV",
                },
                {
                    "column_name": "total_cost_paid_media",
                    "start_row": 0,
                    "end_row": 15,
                    "top_left_value": "6006.15",
                },
            ]
        },
        "planning_summary": {
            "source_summary": {
                "sparse_dimension_columns": [{"source_column": "channel_paid_media"}],
            }
        },
    }
    out = gen._ensure_expand_grouped_block_for_merged_layout(plan, cp)
    names = [
        __import__("sia.tools.tool_validator", fromlist=["normalize_tool_name"]).normalize_tool_name(
            str(t.get("tool") or "").strip()
        )[0]
        for t in (out.tool_calls or [])
        if isinstance(t, dict)
    ]
    assert "transform.expand_grouped_block" in names
    expand = next(
        t for t in out.tool_calls if t.get("tool") == "transform.expand_grouped_block"
    )
    assert expand["params"].get("merged_metric_ranges")
    assert "channel_paid_media" in (expand["params"].get("dimension_columns") or [])


def test_remap_expand_params_renames_merged_metric_range_columns():
    gen = PlanGenerator(llm_client=None)
    params = {
        "dimension_columns": ["channel_paid_media"],
        "merged_metric_ranges": [
            {"column_name": "total_cost_paid_media", "start_row": 0, "end_row": 15},
        ],
    }
    cp = {
        "approved_mappings": [
            {
                "source_column": "total_cost_paid_media",
                "target_column": "spends",
                "decision": "approved",
            },
            {
                "source_column": "channel_paid_media",
                "target_column": "channel",
                "decision": "approved",
            },
        ]
    }
    out = gen._remap_expand_params_to_template_names(params, cp)
    assert out["dimension_columns"] == ["channel"]
    assert out["merged_metric_ranges"][0]["column_name"] == "spends"
