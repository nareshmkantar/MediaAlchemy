"""Merged-cell metric allocation and planner-review allocation choices."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from sia.agent.base import ExtractionPlan
from sia.agent.planner import PlanGenerator
from sia.agent.planner_decisions import (
    apply_resolved_approvals_to_plan,
    infer_block_metric_allocation,
)
from sia.agent.scoped_source import _load_workbook_merged_ranges, map_merged_ranges_to_prepared_df
from sia.tools.transformation_tools import TransformationTools


def _paid_media_style_df() -> pd.DataFrame:
    """Simulates merged total_cost repeated on every row in a 3-row block."""
    total = 6006.15
    return pd.DataFrame(
        {
            "date_paid_media": pd.date_range("2024-01-01", periods=3, freq="D"),
            "channel_paid_media": ["TV", "TV", "TV"],
            "total_cost_paid_media": [total, total, total],
            "impressions_paid_media": [289661.0, 180968.0, 150000.0],
        }
    )


def test_load_workbook_merged_ranges_uses_non_read_only_mode():
    """ReadOnlyWorksheet has no merged_cells — loader must not use read_only=True."""

    class _FakeRange:
        def __str__(self):
            return "D2:D4"

    mock_ws = MagicMock()
    mock_ws.sheet_state = "visible"
    mock_ws.merged_cells.ranges = [_FakeRange()]
    mock_ws.cell.return_value = SimpleNamespace(value=100.0)

    mock_wb = MagicMock()
    mock_wb.sheetnames = ["Data"]
    mock_wb.__getitem__.return_value = mock_ws

    with patch("openpyxl.load_workbook", return_value=mock_wb) as load_wb:
        out = _load_workbook_merged_ranges("book.xlsx", "Data")
        load_wb.assert_called_once()
        assert load_wb.call_args.kwargs.get("read_only") is False
    assert len(out) == 1
    assert out[0]["rows_spanned"] == 3


def test_load_workbook_merged_ranges_returns_empty_when_no_merged_cells_attr():
    mock_ws = MagicMock(spec=[])  # no merged_cells
    mock_wb = MagicMock()
    mock_wb.sheetnames = ["Data"]
    mock_wb.__getitem__.return_value = mock_ws

    with patch("openpyxl.load_workbook", return_value=mock_wb):
        assert _load_workbook_merged_ranges("book.xlsx", "Data") == []


def test_map_merged_ranges_to_prepared_df():
    merged = [
        {
            "range": "D2:D4",
            "min_row": 1,
            "max_row": 3,
            "min_col": 3,
            "max_col": 3,
            "rows_spanned": 3,
            "cols_spanned": 1,
            "top_left_value": 6006.15,
        }
    ]
    bounds = {"start_row": 0, "end_row": 10, "start_col": 0, "end_col": 5}
    mapped = map_merged_ranges_to_prepared_df(
        merged,
        bounds=bounds,
        data_first_row_abs=1,
        column_names=["a", "b", "c", "total_cost_paid_media"],
    )
    assert len(mapped) == 1
    assert mapped[0]["column_name"] == "total_cost_paid_media"
    assert mapped[0]["start_row"] == 0
    assert mapped[0]["end_row"] == 3


def test_map_merged_ranges_to_prepared_df_returns_all_vertical_merges():
    """Regression: mapper must not return after the first matching merge."""
    merged = [
        {
            "range": "D2:D4",
            "min_row": 1,
            "max_row": 3,
            "min_col": 3,
            "max_col": 3,
            "rows_spanned": 3,
            "cols_spanned": 1,
            "top_left_value": 6006.15,
        },
        {
            "range": "D10:D24",
            "min_row": 9,
            "max_row": 23,
            "min_col": 3,
            "max_col": 3,
            "rows_spanned": 15,
            "cols_spanned": 1,
            "top_left_value": 10614.78,
        },
        {
            "range": "D36:D50",
            "min_row": 35,
            "max_row": 49,
            "min_col": 3,
            "max_col": 3,
            "rows_spanned": 15,
            "cols_spanned": 1,
            "top_left_value": 70166.43,
        },
    ]
    bounds = {"start_row": 0, "end_row": 60, "start_col": 0, "end_col": 5}
    mapped = map_merged_ranges_to_prepared_df(
        merged,
        bounds=bounds,
        data_first_row_abs=1,
        column_names=["a", "b", "c", "total_cost_paid_media"],
    )
    assert len(mapped) == 3
    assert [m["excel_range"] for m in mapped] == ["D2:D4", "D10:D24", "D36:D50"]


def test_merged_range_allocates_across_span_and_preserves_total():
    df = _paid_media_style_df()
    merged_ranges = [
        {
            "column_name": "total_cost_paid_media",
            "start_row": 0,
            "end_row": 3,
            "excel_range": "D2:D4",
        }
    ]
    res = TransformationTools.expand_grouped_block(
        df,
        dimension_columns=["channel_paid_media"],
        block_start_columns=["channel_paid_media"],
        allocations=[
            {
                "metric_col": "total_cost_paid_media",
                "method": "equal",
            }
        ],
        auto_detect_block_metrics=False,
        merged_metric_ranges=merged_ranges,
    )
    assert res.success, res.message
    vals = pd.to_numeric(res.data["total_cost_paid_media"], errors="coerce")
    assert abs(float(vals.sum()) - 6006.15) < 0.02
    assert all(abs(v - 6006.15 / 3.0) < 0.02 for v in vals)


def test_merged_range_by_weight_uses_helper_column():
    df = _paid_media_style_df()
    merged_ranges = [
        {
            "column_name": "total_cost_paid_media",
            "start_row": 0,
            "end_row": 3,
        }
    ]
    res = TransformationTools.expand_grouped_block(
        df,
        dimension_columns=["channel_paid_media"],
        block_start_columns=["channel_paid_media"],
        allocations=[
            {
                "metric_col": "total_cost_paid_media",
                "method": "by_weight",
                "weight_col": "impressions_paid_media",
            }
        ],
        auto_detect_block_metrics=False,
        merged_metric_ranges=merged_ranges,
    )
    assert res.success
    weights = df["impressions_paid_media"].astype(float)
    expected = (weights / weights.sum()) * 6006.15
    got = pd.to_numeric(res.data["total_cost_paid_media"], errors="coerce")
    for e, g in zip(expected, got):
        assert abs(float(e) - float(g)) < 0.05


def test_allocate_block_metric_uses_merged_ranges():
    df = _paid_media_style_df()
    res = TransformationTools.allocate_block_metric(
        df,
        metric_col="total_cost_paid_media",
        block_start_columns=["channel_paid_media"],
        method="equal",
        merged_metric_ranges=[
            {"column_name": "total_cost_paid_media", "start_row": 0, "end_row": 3}
        ],
    )
    assert res.success
    assert abs(float(res.data["total_cost_paid_media"].sum()) - 6006.15) < 0.02


def test_planner_does_not_auto_upgrade_to_by_weight():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": ["Name"],
                    "allocations": [{"metric_col": "spends", "method": "equal"}],
                },
            }
        ],
        reasoning="",
    )
    context_packet = {
        "approved_mappings": [
            {"source_column": "Performance_Views", "target_column": "impressions", "decision": "keep"},
        ],
        "mapping_supplement": {"prepared_columns": ["impressions", "spends"]},
    }
    tpl = {
        "x_scope": {"metrics": ["impressions", "spends"]},
        "properties": {"impressions": {}, "spends": {}},
    }
    out = planner.finalize_plan(plan, context_packet, tpl)
    expand = next(
        t for t in out.tool_calls if t.get("tool") == "transform.expand_grouped_block"
    )
    alloc = expand["params"]["allocations"][0]
    assert alloc["method"] == "equal"
    assert alloc.get("weight_col") is None


def test_resolution_note_does_not_set_weight_col_to_choice():
    item = {
        "decision_type": "block_metric_allocation",
        "target_column": "spends",
        "analyst_confirm": "by_weight__impressions",
        "resolution_note": (
            "Choice: Split spends by weight using 'impressions' "
            "(row_metric = block_total × row_weight / sum_weights)."
        ),
        "options": [
            {
                "id": "by_weight__impressions",
                "label": "Split spends by weight using 'impressions'",
            },
        ],
    }
    alloc = infer_block_metric_allocation(item)
    assert alloc is not None
    assert alloc["method"] == "by_weight"
    assert alloc["weight_col"] == "impressions"


def test_approval_item_and_resolved_by_weight_patch():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(tool_calls=[], reasoning="")
    context_packet = {
        "mapping_supplement": {
            "merged_metric_ranges": [
                {
                    "column_name": "total_cost_paid_media",
                    "start_row": 0,
                    "end_row": 3,
                }
            ],
            "prepared_columns": ["impressions_paid_media", "total_cost_paid_media"],
        },
        "approved_mappings": [],
    }
    tpl = {
        "x_scope": {"metrics": ["spends", "impressions"]},
        "properties": {"spends": {}, "impressions": {}},
    }
    out = planner._ensure_block_metric_allocation_approval_items(plan, context_packet, tpl)
    assert out.approval_items
    item = out.approval_items[0]
    assert item.get("decision_type") == "block_metric_allocation"
    option_ids = [o["id"] for o in item.get("options") or []]
    assert "equal" in option_ids
    assert any(str(oid).startswith("by_weight__") for oid in option_ids)

    resolved_item = dict(item)
    resolved_item["analyst_confirm"] = "by_weight__impressions_paid_media"
    alloc = infer_block_metric_allocation(resolved_item)
    assert alloc is not None
    assert alloc["method"] == "by_weight"
    assert alloc["weight_col"] == "impressions_paid_media"

    plan2 = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": ["channel"],
                    "auto_detect_block_metrics": True,
                },
            }
        ],
        reasoning="",
    )
    cp2 = {**context_packet, "resolved_planner_decisions": [resolved_item]}
    patched = apply_resolved_approvals_to_plan(plan2, cp2)
    expand2 = next(
        t for t in patched.tool_calls if t.get("tool") == "transform.expand_grouped_block"
    )
    specs = expand2["params"]["allocations"]
    assert specs
    assert specs[0]["method"] == "by_weight"
    assert specs[0]["weight_col"] == "impressions_paid_media"
    assert expand2["params"]["auto_detect_block_metrics"] is False


def test_skip_block_metric_hitl_for_expand_without_block_evidence():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": ["channel"],
                    "block_start_columns": ["channel"],
                    "allocations": None,
                    "auto_detect_block_metrics": True,
                },
            }
        ],
        reasoning="",
    )
    tpl = {
        "x_scope": {"metrics": ["spends", "impressions"]},
        "properties": {"spends": {}, "impressions": {}},
    }
    out = planner._ensure_block_metric_allocation_approval_items(
        plan,
        context_packet={},
        target_template=tpl,
        structure_analysis={"metric_layout_signals": {"block_sparse_metrics": []}},
    )
    assert not (out.approval_items or [])


def test_dedupe_weight_options_prefers_target_column_name():
    planner = PlanGenerator(llm_client=None)
    cp = {
        "approved_mappings": [
            {
                "source_column": "impressions_paid_media",
                "target_column": "impressions",
                "decision": "approved",
            },
        ],
        "mapping_supplement": {"prepared_columns": ["impressions_paid_media", "spends"]},
    }
    deduped = planner._dedupe_weight_column_options(
        ["impressions", "impressions_paid_media"],
        cp,
    )
    assert deduped == ["impressions"]
