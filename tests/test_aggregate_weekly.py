"""Focused tests for the generic transform.aggregate_weekly tool.

Covers:
- rollup of daily rows into Monday-aligned weekly buckets,
- explicit metric_rules taking precedence over value_cols,
- validator registration and alias normalisation.
"""
import pandas as pd

from sia.tools.transformation_tools import TransformationTools
from sia.tools.tool_validator import (
    TOOL_SCHEMAS,
    normalize_tool_name,
    validate_tool_call,
)


def _daily_df():
    return pd.DataFrame(
        [
            {"date": "2026-04-13", "channel": "search", "spend": 100.0, "impressions": 10},
            {"date": "2026-04-14", "channel": "search", "spend": 150.0, "impressions": 15},
            {"date": "2026-04-15", "channel": "search", "spend": 200.0, "impressions": 25},
            {"date": "2026-04-20", "channel": "search", "spend": 50.0,  "impressions": 5},
            {"date": "2026-04-14", "channel": "social", "spend": 300.0, "impressions": 40},
            {"date": "2026-04-21", "channel": "social", "spend": 120.0, "impressions": 18},
        ]
    )


def test_aggregate_weekly_sums_daily_rows_by_monday_week():
    df = _daily_df()
    res = TransformationTools.aggregate_weekly(
        df,
        date_col="date",
        group_by_cols=["channel"],
        metric_rules={"spend": "sum", "impressions": "sum"},
    )
    assert res.success, res.message
    out = res.data

    assert set(["channel", "date", "spend", "impressions"]).issubset(out.columns)
    assert "week_start" not in out.columns
    assert "week_end" not in out.columns

    week_starts = {pd.Timestamp(ws).normalize() for ws in out["date"].unique()}
    assert pd.Timestamp("2026-04-13") in week_starts
    assert pd.Timestamp("2026-04-20") in week_starts

    week_starts_series = pd.to_datetime(out["date"]).dt.normalize()
    search_w1 = out[
        (out["channel"] == "search")
        & (week_starts_series == pd.Timestamp("2026-04-13"))
    ].iloc[0]
    assert search_w1["spend"] == 450.0
    assert search_w1["impressions"] == 50

    social_w2 = out[
        (out["channel"] == "social")
        & (week_starts_series == pd.Timestamp("2026-04-20"))
    ].iloc[0]
    assert social_w2["spend"] == 120.0


def test_aggregate_weekly_metric_rules_honour_mean():
    df = _daily_df()
    res = TransformationTools.aggregate_weekly(
        df,
        date_col="date",
        group_by_cols=["channel"],
        metric_rules={"spend": "mean"},
    )
    assert res.success, res.message
    out = res.data
    week_starts_series = pd.to_datetime(out["date"]).dt.normalize()
    search_w1 = out[
        (out["channel"] == "search")
        & (week_starts_series == pd.Timestamp("2026-04-13"))
    ].iloc[0]
    assert abs(search_w1["spend"] - 150.0) < 1e-6


def test_aggregate_weekly_explicit_default_week_start_reuses_input_date_name():
    """Plans often pass week_start_col='week_start' even when the only date column is ``date``."""
    df = _daily_df()
    res = TransformationTools.aggregate_weekly(
        df,
        date_col="date",
        group_by_cols=["channel"],
        metric_rules={"spend": "sum"},
        week_start_col="week_start",
        drop_original_date=True,
    )
    assert res.success, res.message
    out = res.data
    assert "week_start" not in out.columns
    assert "date" in out.columns


def test_aggregate_weekly_custom_output_bucket_name():
    df = _daily_df()
    res = TransformationTools.aggregate_weekly(
        df,
        date_col="date",
        group_by_cols=["channel"],
        metric_rules={"spend": "sum"},
        week_start_col="bucket_mon",
        drop_original_date=True,
    )
    assert res.success, res.message
    out = res.data
    assert "bucket_mon" in out.columns
    assert "date" not in out.columns


def test_aggregate_weekly_when_date_col_is_already_named_week_start():
    df = pd.DataFrame(
        [
            {"week_start": "2026-04-13", "channel": "search", "spend": 10.0},
            {"week_start": "2026-04-14", "channel": "search", "spend": 20.0},
        ]
    )
    res = TransformationTools.aggregate_weekly(
        df,
        date_col="week_start",
        group_by_cols=["channel"],
        metric_rules={"spend": "sum"},
        week_start_col="week_start",
        drop_original_date=True,
    )
    assert res.success, res.message
    assert "week_start" in res.data.columns


def test_aggregate_weekly_registered_in_validator():
    assert "transform.aggregate_weekly" in TOOL_SCHEMAS

    canonical, corrected = normalize_tool_name("aggregate_weekly")
    assert canonical == "transform.aggregate_weekly"
    assert corrected is True

    result = validate_tool_call({
        "tool": "transform.aggregate_weekly",
        "params": {
            "date_col": "date",
            "group_by_cols": ["channel"],
            "metric_rules": {"spend": "sum"},
        },
    })
    assert result.valid, result.errors
    assert result.normalized_tool_name == "transform.aggregate_weekly"
