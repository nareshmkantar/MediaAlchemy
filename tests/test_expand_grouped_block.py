"""Tests for transform.expand_grouped_block (dimension ffill + metric allocate)."""

import pandas as pd

from sia.agent.planner import PlanGenerator
from sia.agent.base import ExtractionPlan
from sia.tools.transformation_tools import TransformationTools


def _influencer_block_df() -> pd.DataFrame:
  """Sparse Name/Market on header row; spends block total; impressions per child row."""
  return pd.DataFrame(
      {
          "Name": ["Elita Fidiya Nugrahani", None, None, None, None],
          "Market": ["ID", None, None, None, None],
          "channel": ["IG Reel 1", "IG Reel 2", "IG Reel 3", "IG Reel 4", "IG Reel 5"],
          "date": pd.date_range("2024-01-12", periods=5, freq="D"),
          "impressions": [1615, 2160, 1680, 3375, 35990],
          "spends": [4285.54, None, None, None, None],
      }
  )


def test_expand_grouped_block_ffills_dims_and_splits_spends_by_weight():
    df = _influencer_block_df()
    res = TransformationTools.expand_grouped_block(
        df,
        dimension_columns=["Name", "Market"],
        block_start_columns=["Name"],
        allocations=[
            {
                "metric_col": "spends",
                "method": "by_weight",
                "weight_col": "impressions",
            }
        ],
        auto_detect_block_metrics=False,
    )
    assert res.success
    out = res.data
    assert out["Name"].notna().all()
    assert out["Market"].notna().all()
    assert out["spends"].notna().all()
    total = float(pd.to_numeric(out["spends"], errors="coerce").sum())
    assert abs(total - 4285.54) < 0.02
    assert len(out) == 5


def test_expand_preserves_row_level_impressions_when_llm_allocates_both():
    """LLM often lists impressions in metric_columns; must not overwrite row metrics."""
    df = _influencer_block_df()
    before = list(df["impressions"])
    res = TransformationTools.expand_grouped_block(
        df,
        dimension_columns=["Name", "Market"],
        block_start_columns=["Name"],
        allocations=[
            {"metric_col": "spends", "method": "by_weight", "weight_col": "impressions"},
            {"metric_col": "impressions", "method": "equal"},
        ],
        auto_detect_block_metrics=False,
    )
    assert res.success
    assert list(res.data["impressions"]) == before
    assert "impressions" in (res.changes_made.get("skipped_row_level_metrics") or [])
    assert res.data["spends"].notna().all()


def test_expand_ffills_channel_when_publisher_is_dense_on_every_row():
    """Publisher on all rows must not be a block-start key or each day becomes a 1-row block."""
    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=5, freq="D"),
            "publisher": ["TV Channel 1"] * 5,
            "channel": ["TV", None, None, None, None],
            "spends": [6006.15, None, None, None, None],
            "impressions": [289661, 180968, 988018, 3580579, 4524833],
        }
    )
    res = TransformationTools.expand_grouped_block(
        df,
        dimension_columns=["publisher", "channel"],
        block_start_columns=["channel"],
        allocations=[
            {"metric_col": "spends", "method": "by_weight", "weight_col": "impressions"},
        ],
        auto_detect_block_metrics=False,
    )
    assert res.success, res.message
    out = res.data
    assert out["channel"].notna().all()
    assert out["spends"].notna().all()
    assert abs(float(pd.to_numeric(out["spends"], errors="coerce").sum()) - 6006.15) < 0.02


def test_expand_ffills_date_inside_excel_merge_when_channel_is_dense_block_start():
    """
    Date merged across two channel rows; channel filled on every row (dense block_start).
    Segment ffill uses 1-row segments — Excel merge span must still fill the date column.
    """
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-14", None, "2024-01-14", None, "2024-01-01"]),
            "publisher": ["TV Channel 1", "TV Channel 2", "TV Channel 1", "TV Channel 2", "TV Channel 2"],
            "channel": ["Social", "Social", "TV", "TV", "TV"],
            "spends": [1699.48, 3310.51, 1699.48, 3310.51, 52.91],
            "impressions": [3000, 5844, 3000, 5844, 1156],
        }
    )
    merged_ranges = [
        {"column_name": "date", "start_row": 0, "end_row": 2, "excel_range": "A1:A2"},
        {"column_name": "date", "start_row": 2, "end_row": 4, "excel_range": "A3:A4"},
    ]
    res = TransformationTools.expand_grouped_block(
        df,
        dimension_columns=["date", "publisher", "channel"],
        block_start_columns=["channel"],
        allocations=[{"metric_col": "spends", "method": "by_weight", "weight_col": "impressions"}],
        auto_detect_block_metrics=False,
        merged_metric_ranges=merged_ranges,
    )
    assert res.success, res.message
    assert "date" in (res.changes_made.get("merged_dimension_ffill_columns") or [])
    dates = res.data["date"]
    assert pd.notna(dates.iloc[1])
    assert str(dates.iloc[1].date()) == "2024-01-14"
    assert pd.notna(dates.iloc[3])
    assert str(dates.iloc[3].date()) == "2024-01-14"


def test_expand_auto_detects_block_level_spends():
    df = _influencer_block_df()
    res = TransformationTools.expand_grouped_block(
        df,
        dimension_columns=["Name", "Market"],
        block_start_columns=["Name"],
        auto_detect_block_metrics=True,
    )
    assert res.success
    report = res.changes_made.get("metric_layout_report") or []
    spend_entry = next((r for r in report if r.get("column") == "spends"), None)
    assert spend_entry is not None
    assert spend_entry.get("grain") == "block_header_total"
    assert res.data["spends"].notna().all()


def test_ensure_expand_runs_after_rename():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": ["Name", "Market"],
                    "block_start_columns": ["Name"],
                    "allocations": [{"metric_col": "spends", "weight_col": "impressions"}],
                },
            },
            {
                "tool": "transform.rename",
                "params": {
                    "mapping": {
                        "Post": "channel",
                        "Performance_Views": "impressions",
                        "Budgets_Total": "spends",
                    }
                },
            },
            {"tool": "transform.type_cast", "params": {}},
        ],
        reasoning="",
    )
    context_packet = {
        "approved_mappings": [
            {"source_column": "Post", "target_column": "channel", "decision": "keep"},
            {"source_column": "Performance_Views", "target_column": "impressions", "decision": "keep"},
            {"source_column": "Budgets_Total", "target_column": "spends", "decision": "keep"},
        ],
    }
    out = planner._ensure_expand_after_column_typing(plan, context_packet)
    names = [t.get("tool") for t in out.tool_calls]
    assert names.index("transform.expand_grouped_block") > names.index("transform.rename")
    assert names.index("transform.expand_grouped_block") > names.index("transform.type_cast")
    expand = next(t for t in out.tool_calls if t.get("tool") == "transform.expand_grouped_block")
    assert expand["params"]["allocations"][0]["weight_col"] == "impressions"


def test_prefer_expand_replaces_fill_merged_when_no_allocate():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {
                "tool": "transform.fill_merged",
                "params": {"columns": ["Name", "Market"], "direction": "down"},
            },
            {"tool": "transform.rename", "params": {"mapping": {"Post": "channel"}}},
            {"tool": "transform.type_cast", "params": {}},
        ],
        reasoning="",
    )
    out = planner._prefer_expand_grouped_block(
        plan,
        structure_analysis={
            "metric_layout_signals": {
                "block_sparse_metrics": [
                    {"column_label": "spends", "spend_like": True},
                ],
            },
        },
    )
    names = [t.get("tool") for t in out.tool_calls]
    assert "transform.expand_grouped_block" in names
    assert "transform.fill_merged" not in names


def test_prefer_expand_grouped_block_merges_fill_and_allocate():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {
                "tool": "transform.fill_merged",
                "params": {"columns": ["Name", "Market"], "direction": "down"},
            },
            {"tool": "transform.type_cast", "params": {}},
            {
                "tool": "transform.allocate_block_metric",
                "params": {
                    "metric_col": "spends",
                    "block_start_columns": ["Name"],
                    "method": "by_weight",
                    "weight_col": "impressions",
                },
            },
        ],
        reasoning="",
    )
    out = planner._prefer_expand_grouped_block(plan)
    names = [t.get("tool") for t in out.tool_calls]
    assert "transform.expand_grouped_block" in names
    assert "transform.fill_merged" not in names
    assert "transform.allocate_block_metric" not in names
    expand = next(t for t in out.tool_calls if t.get("tool") == "transform.expand_grouped_block")
    assert "Name" in expand["params"]["dimension_columns"]
    assert expand["params"]["allocations"][0]["metric_col"] == "spends"
