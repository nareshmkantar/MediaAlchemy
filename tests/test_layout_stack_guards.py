"""Guards for layout.stack — block boundaries and schema alignment."""

from __future__ import annotations

import pandas as pd

from sia.agent.layout_stack_utils import (
    assess_stack_readiness,
    build_blocks_from_main_blocks,
    looks_like_horizontal_block_failure,
    sanitize_layout_stack_params,
    should_prefer_stack_over_single_extract,
)
from sia.agent.planner import PlanGenerator
from sia.agent.base import ExtractionPlan
from sia.tools.transformation_tools import TransformationTools


def test_looks_like_horizontal_block_failure_detects_suffix_columns():
    df = pd.DataFrame(
        {
            "Date": ["2023-01-07"],
            "Spend": [1.0],
            "Date.1": ["2023-01-07"],
            "Spend.1": [2.0],
        }
    )
    assert looks_like_horizontal_block_failure(df) is True


def test_assess_stack_readiness_requires_two_disjoint_blocks():
    # Two side-by-side blocks with shared headers
    raw = pd.DataFrame(
        [
            ["Date", "Spend", "", "Date", "Spend"],
            ["2023-01-07", 10, "", "2023-01-07", 20],
            ["2023-01-14", 11, "", "2023-01-14", 21],
        ]
    )
    blocks = [
        {"col_start": 0, "col_end": 1, "row_start": 1, "row_end": 3},
        {"col_start": 3, "col_end": 4, "row_start": 1, "row_end": 3},
    ]
    ok, msg = assess_stack_readiness(raw, 0, blocks)
    assert ok, msg

    merged = TransformationTools.merge_blocks(raw, header_row=0, blocks=blocks)
    assert merged.success
    assert not looks_like_horizontal_block_failure(merged.data)
    assert "Date" in merged.data.columns
    assert "Date.1" not in merged.data.columns
    assert len(merged.data) == 4


def test_merge_blocks_refuses_single_full_width_block():
    raw = pd.DataFrame(
        [
            ["Date", "Spend", "Date", "Spend"],
            ["2023-01-07", 10, "2023-01-07", 20],
        ]
    )
    blocks = [{"col_start": 0, "col_end": 3, "row_start": 1, "row_end": 1}]
    result = TransformationTools.merge_blocks(raw, header_row=0, blocks=blocks)
    assert not result.success
    assert "at least two" in (result.message or "").lower()


def test_planner_replaces_stack_without_blocks():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[{"tool": "layout.stack", "params": {"header_row": 0, "blocks": []}}],
        reasoning="",
    )
    scoped = {
        "header_row": 0,
        "analysis_bounds": {
            "start_row": 0,
            "end_row": 2,
            "start_col": 0,
            "end_col": 3,
        },
        "main_blocks": [],
    }
    out = gen._ensure_layout_stack_in_plan(plan, {"scoped_source": scoped}, {})
    assert out.tool_calls[0]["tool"] == "layout.extract"
    assert "start_row" in out.tool_calls[0]["params"]


def test_sanitize_fills_blocks_from_main_blocks():
    main_blocks = [
        {
            "coordinates": {
                "col_start": 0,
                "col_end": 2,
                "start_row": 0,
                "end_row": 3,
                "header_row": 0,
            }
        },
        {
            "coordinates": {
                "col_start": 4,
                "col_end": 6,
                "start_row": 0,
                "end_row": 3,
                "header_row": 0,
            }
        },
    ]
    built = build_blocks_from_main_blocks(main_blocks, header_row=0, data_end_row=3)
    assert len(built) == 2
    params, warns = sanitize_layout_stack_params(
        {"header_row": 0},
        scoped_source={"main_blocks": main_blocks, "header_row": 0},
    )
    assert len(params.get("blocks") or []) == 2
    assert any("main_blocks" in w for w in warns)


def test_should_prefer_stack_for_disjoint_main_blocks():
    scoped = {
        "main_blocks": [
            {"coordinates": {"col_start": 0, "col_end": 5, "header_row": 0}},
            {"coordinates": {"col_start": 7, "col_end": 12, "header_row": 0}},
        ]
    }
    assert should_prefer_stack_over_single_extract({}, scoped) is True
