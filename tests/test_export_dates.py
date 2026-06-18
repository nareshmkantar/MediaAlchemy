import pandas as pd

from sia.utils.export_dates import normalize_dataframe_dates_for_export


def test_datetime64_columns_format_as_iso_date_strings():
    df = pd.DataFrame(
        {
            "event_date": pd.to_datetime(["2026-01-05", "2026-02-01"]),
            "channel": ["a", "b"],
        }
    )
    out = normalize_dataframe_dates_for_export(df)
    assert list(out["event_date"]) == ["2026-01-05", "2026-02-01"]
    assert list(out["channel"]) == ["a", "b"]


def test_object_date_named_column_parses_when_majority_dates():
    df = pd.DataFrame(
        {
            "report_date": ["2026-03-10", "2026-03-11", "not-a-date"],
            "x": [1, 2, 3],
        }
    )
    out = normalize_dataframe_dates_for_export(df)
    assert out["report_date"].iloc[0] == "2026-03-10"
    assert out["report_date"].iloc[1] == "2026-03-11"
    assert out["report_date"].iloc[2] == "not-a-date"


def test_empty_frame_returns_copy():
    df = pd.DataFrame()
    out = normalize_dataframe_dates_for_export(df)
    assert out is not None
    assert len(out) == 0
