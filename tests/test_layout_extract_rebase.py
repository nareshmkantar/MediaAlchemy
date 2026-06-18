"""layout.extract / layout.stack params must be relative to the scoped (cropped) grid."""

import pandas as pd

from sia.agent.scoped_source import (
    build_scoped_source,
    layout_crop_origin,
    rebase_layout_stack_params,
    resolve_layout_extract_params,
)
from sia.tools.transformation_tools import TransformationTools


def test_layout_crop_origin_when_demarcated():
    scoped = build_scoped_source(
        "Sheet1",
        blocks=[
            {
                "category": "Main Data",
                "decision": "Keep",
                "coordinates": {
                    "start_row": 1,
                    "end_row": 5,
                    "start_col": 0,
                    "end_col": 4,
                    "header_row": 1,
                },
            }
        ],
    )
    assert layout_crop_origin(scoped) == (1, 0)


def test_rebase_layout_extract_grand_total_offset():
    """Absolute header row 1 with crop origin 1 -> header index 0 on scoped grid."""
    scoped = {
        "requires_extraction": True,
        "analysis_bounds": {"start_row": 1, "end_row": 79, "start_col": 0, "end_col": 5},
        "absolute_bounds": {"start_row": 1, "end_row": 79, "start_col": 0, "end_col": 5},
        "header_row": 1,
    }
    abs_params = {
        "start_row": 1,
        "end_row": 79,
        "start_col": 0,
        "end_col": 5,
        "header_row": 1,
    }
    rel = resolve_layout_extract_params(abs_params, scoped)
    assert rel == {
        "start_row": 0,
        "end_row": 78,
        "start_col": 0,
        "end_col": 5,
        "header_row": 0,
    }


def test_resolve_overrides_wrong_plan_header_row():
    """Structure/plan may pass header_row=2 (TikTok); demarcation header_row=1 must win."""
    scoped = {
        "requires_extraction": True,
        "header_row": 1,
        "analysis_bounds": {"start_row": 0, "end_row": 79, "start_col": 0, "end_col": 5},
        "absolute_bounds": {"start_row": 0, "end_row": 79, "start_col": 0, "end_col": 5},
    }
    wrong_plan = {
        "start_row": 0,
        "end_row": 79,
        "start_col": 0,
        "end_col": 5,
        "header_row": 2,
    }
    rel = resolve_layout_extract_params(wrong_plan, scoped)
    assert rel["header_row"] == 1


def test_extract_data_block_uses_rebased_header_on_cropped_grid():
    rows = [
        ["GRAND TOTAL", None, None, 2442963.84, 113474789],
        ["date_paid_media", "publisher_name_paid_media", "channel_paid_media", "total_cost_paid_media", "impressions_paid_media"],
        ["2024-01-01", "TikTok", "Social", 8924.45, 68905],
        ["2024-01-02", "Network 10", "TV", 33641.83, 1858778],
    ]
    full = pd.DataFrame(rows)
    cropped = full.iloc[1:].reset_index(drop=True)
    scoped = {
        "requires_extraction": True,
        "header_row": 1,
        "analysis_bounds": {"start_row": 1, "end_row": 3, "start_col": 0, "end_col": 4},
        "absolute_bounds": {"start_row": 1, "end_row": 3, "start_col": 0, "end_col": 4},
    }
    params = resolve_layout_extract_params(
        {
            "start_row": 1,
            "end_row": 3,
            "start_col": 0,
            "end_col": 4,
            "header_row": 2,
        },
        scoped,
    )
    result = TransformationTools.extract_data_block(
        cropped,
        params["start_row"],
        params["end_row"],
        params["start_col"],
        params["end_col"],
        params["header_row"],
    )
    assert result.success
    assert list(result.data.columns) == [
        "date_paid_media",
        "publisher_name_paid_media",
        "channel_paid_media",
        "total_cost_paid_media",
        "impressions_paid_media",
    ]
    assert result.data.iloc[0]["publisher_name_paid_media"] == "TikTok"


def test_rebase_layout_stack_block_cols():
    scoped = {
        "requires_extraction": True,
        "analysis_bounds": {"start_row": 2, "start_col": 1, "end_row": 10, "end_col": 8},
    }
    out = rebase_layout_stack_params(
        {
            "header_row": 2,
            "blocks": [{"col_start": 1, "col_end": 4, "row_start": 3, "row_end": 10}],
        },
        scoped,
    )
    assert out["header_row"] == 0
    assert out["blocks"][0]["col_start"] == 0
    assert out["blocks"][0]["col_end"] == 3
    assert out["blocks"][0]["row_start"] == 1
