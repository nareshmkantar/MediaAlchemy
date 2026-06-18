"""Merged metric ranges survive materialized clean workbooks and plan finalize."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from sia.agent.base import ExtractionPlan
from sia.agent.materialized_clean_sheet import resolve_processing_workbook
from sia.agent.planner import PlanGenerator
from sia.agent.scoped_source import (
    detect_merged_metric_ranges_for_prepared_df,
    reconcile_merged_metric_ranges_to_columns,
)


def test_reconcile_merged_metric_ranges_drops_missing_columns():
    prior = [
        {"column_name": "total_cost_paid_media", "start_row": 5, "end_row": 20},
        {"column_name": "gone_column", "start_row": 0, "end_row": 3},
    ]
    out = reconcile_merged_metric_ranges_to_columns(
        prior, ["date", "channel", "total_cost_paid_media"]
    )
    assert len(out) == 1
    assert out[0]["column_name"] == "total_cost_paid_media"
    assert out[0]["start_row"] == 5


def test_detect_merged_reuses_prior_when_workbook_has_no_merges():
    prior = [
        {
            "column_name": "spends",
            "start_row": 10,
            "end_row": 25,
            "excel_range": "D11:D25",
        }
    ]
    with patch(
        "sia.agent.scoped_source._load_workbook_merged_ranges",
        return_value=[],
    ):
        out = detect_merged_metric_ranges_for_prepared_df(
            "clean.xlsx",
            "Clean",
            bounds={"start_row": 0, "end_row": 100, "start_col": 0, "end_col": 10},
            data_first_row_abs=1,
            column_names=["date", "channel", "spends"],
            prior_ranges=prior,
        )
    assert len(out) == 1
    assert out[0]["start_row"] == 10


def test_resolve_processing_workbook_preserves_merged_from_registry():
    job = {
        "materialized_clean_templates": {
            "src1": {
                "path": "/tmp/src1_clean.xlsx",
                "sheet_name": "Clean",
                "rows": 50,
                "cols": 8,
                "merged_metric_ranges": [
                    {"column_name": "total_cost_paid_media", "start_row": 10, "end_row": 25},
                ],
            }
        }
    }
    orig_scoped = {
        "sheet_name": "Publisher",
        "merged_metric_ranges": [
            {"column_name": "total_cost_paid_media", "start_row": 10, "end_row": 25},
        ],
    }
    with patch("pathlib.Path.is_file", return_value=True):
        _path, sheet, scoped, used = resolve_processing_workbook(
            job,
            "src1",
            "/tmp/original.xlsx",
            "Publisher",
            orig_scoped,
        )
    assert used is True
    assert sheet == "Clean"
    assert scoped.get("source_workbook_path") == "/tmp/original.xlsx"
    assert scoped.get("source_sheet_name") == "Publisher"
    assert len(scoped.get("merged_metric_ranges") or []) == 1


def test_finalize_merged_layout_expand_params_injects_template_names():
    gen = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.rename",
                "params": {"mapping": {"total_cost_paid_media": "spends"}},
            },
            {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": ["channel_paid_media"],
                    "block_start_columns": ["channel_paid_media"],
                    "merged_metric_ranges": [],
                },
            },
        ],
        reasoning="",
    )
    cp = {
        "mapping_supplement": {
            "merged_metric_ranges": [
                {
                    "column_name": "total_cost_paid_media",
                    "start_row": 10,
                    "end_row": 25,
                }
            ],
            "sparse_dimension_columns": [
                {"source_column": "publisher_paid_media", "blank_ratio": 0.4},
            ],
        },
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
            {
                "source_column": "publisher_paid_media",
                "target_column": "publisher",
                "decision": "keep",
            },
        ],
        "planning_summary": {
            "source_summary": {
                "sparse_dimension_columns": [
                    {"source_column": "publisher_paid_media", "blank_ratio": 0.4},
                ],
            }
        },
    }
    out = gen._finalize_merged_layout_expand_params(plan, cp)
    expand = next(
        t for t in out.tool_calls if t.get("tool") == "transform.expand_grouped_block"
    )
    mmr = expand["params"].get("merged_metric_ranges") or []
    assert mmr
    assert mmr[0]["column_name"] == "spends"
    assert expand["params"].get("block_start_columns") == ["publisher"]
