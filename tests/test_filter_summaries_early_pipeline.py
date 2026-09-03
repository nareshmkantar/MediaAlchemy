"""filter_summaries runs immediately after layout extract in plans and sort order."""

from __future__ import annotations

import pandas as pd

from sia.agent.base import ExtractionPlan
from sia.agent.planner import PlanGenerator
from sia.tools.tool_validator import sort_tool_calls_by_pipeline_stage
from sia.tools.transformation_tools import TransformationTools


def test_planner_injects_filter_summaries_after_extract():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "layout.extract",
                "params": {"start_row": 0, "end_row": 20, "start_col": 0, "end_col": 5, "header_row": 0},
            },
            {"tool": "transform.rename", "params": {"mapping": {"total_cost_paid_media": "spends"}}},
            {"tool": "transform.expand_grouped_block", "params": {"dimension_columns": ["channel_paid_media"]}},
        ],
        reasoning="",
    )
    structure_analysis = {
        "tables": [{"label": "Grand Total row at bottom", "coordinates": {}}],
    }
    out = gen._ensure_filter_summaries_after_extract(plan, {}, {"x_scope": {}}, structure_analysis)
    names = [t["tool"] for t in out.tool_calls]
    assert names.index("transform.filter_summaries") == names.index("layout.extract") + 1
    assert names.index("transform.rename") > names.index("transform.filter_summaries")


def test_planner_skips_filter_summaries_without_summary_signals():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "layout.extract",
                "params": {"start_row": 0, "end_row": 20, "start_col": 0, "end_col": 5, "header_row": 0},
            },
            {"tool": "transform.filter_summaries", "params": {"keywords": ["Total"]}},
            {"tool": "transform.rename", "params": {"mapping": {"a": "b"}}},
        ],
        reasoning="",
        requires_human_review=True,
        review_reason="Plan includes destructive tools: transform.filter_summaries",
        confidence=0.95,
    )
    out = gen.finalize_plan(plan, {}, {"x_scope": {}}, structure_analysis={"tables": []})
    names = [t["tool"] for t in out.tool_calls]
    assert "transform.filter_summaries" not in names
    assert out.requires_human_review is False
    assert out.review_reason == ""


def test_reconcile_keeps_review_for_approval_items_without_destructive_reason():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[{"tool": "layout.extract", "params": {}}],
        approval_items=[{"summary": "Confirm date column"}],
        requires_human_review=True,
        review_reason="Plan includes destructive tools: transform.filter_summaries",
        confidence=0.95,
    )
    out = gen._reconcile_destructive_plan_review(plan)
    assert out.requires_human_review is True
    assert "approval items" in out.review_reason


def test_sort_places_filter_summaries_after_extract_before_rename():
    calls = [
        {"tool": "layout.extract", "params": {"start_row": 0, "end_row": 10, "start_col": 0, "end_col": 5, "header_row": 0}},
        {"tool": "transform.rename", "params": {"mapping": {"a": "b"}}},
        {"tool": "transform.filter_summaries", "params": {"keywords": ["Subtotal"]}},
        {"tool": "transform.expand_grouped_block", "params": {"dimension_columns": ["channel"]}},
        {"tool": "transform.type_cast", "params": {"columns": {"b": "float"}}},
    ]
    out = sort_tool_calls_by_pipeline_stage(calls)
    names = [c["tool"] for c in out]
    assert names.index("layout.extract") < names.index("transform.filter_summaries")
    assert names.index("transform.filter_summaries") < names.index("transform.rename")
    assert names.index("transform.filter_summaries") < names.index("transform.expand_grouped_block")


def test_filter_removes_subtotal_publisher_row():
    df = pd.DataFrame(
        {
            "date_paid_media": ["2024-01-01", ""],
            "publisher_name_paid_media": ["TV Channel 1", "SUBTOTAL: TV Channel 1"],
            "total_cost_paid_media": [100.0, 328644.19],
            "impressions_paid_media": [1000, 17074049],
        }
    )
    result = TransformationTools.filter_summary_rows(
        df,
        keywords=["Subtotal", "SUBTOTAL", "Total"],
        use_structural_detection=True,
    )
    assert result.success
    assert len(result.data) == 1
    assert "SUBTOTAL" not in str(result.data["publisher_name_paid_media"].iloc[0])
