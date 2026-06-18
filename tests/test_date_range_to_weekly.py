import pandas as pd

from sia.tools.transformation_tools import TransformationTools


def test_date_range_to_weekly_monday_bucket():
    df = pd.DataFrame(
        [
            {
                "campaign": "X",
                "Start Date": "2026-04-15",
                "End Date": "2026-04-19",
                "Spend": 500.0,
            }
        ]
    )
    res = TransformationTools.date_range_to_weekly(
        df,
        start_date_col="Start Date",
        end_date_col="End Date",
        value_cols=["Spend"],
        id_cols=["campaign"],
        granularity="weekly",
    )
    assert res.success
    out = res.data
    assert len(out) == 1
    row = out.iloc[0]
    assert row["campaign"] == "X"
    assert row["days_in_week"] == 5
    assert abs(row["Spend"] - 500.0) < 1e-6
    assert pd.Timestamp(row["week_start"]).weekday() == 0
    assert pd.Timestamp(row["week_start"]) == pd.Timestamp("2026-04-13")
    assert "week_end" not in out.columns


def test_date_range_to_daily_row_count():
    df = pd.DataFrame([{"s": "2026-04-15", "e": "2026-04-19", "v": 100.0}])
    res = TransformationTools.date_range_to_weekly(
        df,
        start_date_col="s",
        end_date_col="e",
        value_cols=["v"],
        id_cols=[],
        granularity="daily",
        date_column="d",
    )
    assert res.success
    assert len(res.data) == 5
    assert abs(res.data["v"].sum() - 100.0) < 1e-6


def test_row_filters_eq():
    df = pd.DataFrame(
        [
            {"status": "active", "s": "2026-04-15", "e": "2026-04-16", "v": 200.0},
            {"status": "paused", "s": "2026-04-15", "e": "2026-04-16", "v": 999.0},
        ]
    )
    res = TransformationTools.date_range_to_weekly(
        df,
        start_date_col="s",
        end_date_col="e",
        value_cols=["v"],
        id_cols=["status"],
        granularity="weekly",
        row_filters=[{"column": "status", "operator": "eq", "value": "active"}],
    )
    assert res.success
    assert len(res.data) == 1
    assert res.data.iloc[0]["status"] == "active"
    assert abs(res.data.iloc[0]["v"] - 200.0) < 1e-6


def test_rejects_when_start_and_end_resolve_to_same_column():
    df = pd.DataFrame([{"d": "2026-04-15", "v": 100.0}])
    res = TransformationTools.date_range_to_weekly(
        df,
        start_date_col="d",
        end_date_col="d",
        value_cols=["v"],
        id_cols=[],
        granularity="weekly",
    )
    assert not res.success
    assert "same column" in (res.message or "").lower()


def test_spans_two_calendar_weeks():
    df = pd.DataFrame([{"s": "2026-04-15", "e": "2026-04-21", "v": 700.0}])
    res = TransformationTools.date_range_to_weekly(
        df,
        "s",
        "e",
        ["v"],
        id_cols=[],
    )
    assert res.success
    out = res.data
    assert len(out) == 2
    assert abs(out["v"].sum() - 700.0) < 1e-6


def test_thin_wrappers_match_date_range_to_weekly():
    df = pd.DataFrame([{"s": "2026-04-15", "e": "2026-04-17", "v": 30.0}])
    daily_wrap = TransformationTools.expand_date_range_to_daily(df, "s", "e", ["v"])
    daily_full = TransformationTools.date_range_to_weekly(
        df, "s", "e", ["v"], granularity="daily", date_column="calendar_date"
    )
    assert daily_wrap.success and daily_full.success
    pd.testing.assert_frame_equal(
        daily_wrap.data.sort_index(axis=1),
        daily_full.data.sort_index(axis=1),
        check_dtype=False,
    )
    week_wrap = TransformationTools.expand_date_range_to_weekly(df, "s", "e", ["v"])
    week_full = TransformationTools.date_range_to_weekly(df, "s", "e", ["v"], granularity="weekly")
    assert week_wrap.success and week_full.success
    pd.testing.assert_frame_equal(
        week_wrap.data.sort_index(axis=1),
        week_full.data.sort_index(axis=1),
        check_dtype=False,
    )
