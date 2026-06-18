"""Tests for DataFrame preview serialization column order."""

import pandas as pd

from sia.utils.df_preview import dataframe_to_preview_records, preview_column_order


def test_preview_records_follow_dataframe_column_order():
    df = pd.DataFrame(
        {
            "date": ["2023-05-08"],
            "channel": ["IG Reel 1"],
            "publisher": ["total"],
            "spends": [100.0],
            "impressions": [12240],
        }
    )
    df = df[["date", "channel", "publisher", "spends", "impressions"]]

    order, records = dataframe_to_preview_records(df, max_rows=1)

    assert order == ["date", "channel", "publisher", "spends", "impressions"]
    assert list(records[0].keys()) == order


def test_preview_column_order_matches_frame():
    df = pd.DataFrame({"z": [1], "a": [2]})
    assert preview_column_order(df) == ["z", "a"]


def test_preview_records_with_integer_column_labels():
    """Excel header=None loads as 0..N columns; values must not be dropped."""
    df = pd.DataFrame([[10, 20, 30], [40, 50, 60]])
    order, records = dataframe_to_preview_records(df, max_rows=2)

    assert order == ["0", "1", "2"]
    assert records[0] == {"0": 10, "1": 20, "2": 30}
    assert records[1] == {"0": 40, "1": 50, "2": 60}
