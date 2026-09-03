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


def test_promote_numeric_header_row_for_preview():
    from sia.utils.df_preview import promote_numeric_header_row_for_preview

    df = pd.DataFrame(
        [
            ["Campaign End Date", "Advertiser", "impressions"],
            ["2024-12-15", "GERMANY_ALPRO_ALPRO", "100"],
            ["2024-12-16", "GERMANY_ALPRO_ALPRO", "200"],
        ]
    )
    out = promote_numeric_header_row_for_preview(df)
    assert list(out.columns) == ["Campaign End Date", "Advertiser", "impressions"]
    assert len(out) == 2
    assert out.iloc[0]["Advertiser"] == "GERMANY_ALPRO_ALPRO"


def test_mapped_preview_column_names_keeps_selected_only():
    from sia.utils.df_preview import mapped_preview_column_names

    columns = ["orderCurrency", "advertiserName", "impressions", "noise"]
    mappings = [
        {"source_column": "orderCurrency", "target_column": "currency"},
        {"source_column": "impressions", "target_column": "impressions"},
        {"source_column": "noise", "target_column": "no match"},
    ]
    assert mapped_preview_column_names(columns, mappings) == ["orderCurrency", "impressions"]


def test_promote_numeric_header_row_keeps_true_numeric_grid():
    from sia.utils.df_preview import promote_numeric_header_row_for_preview

    df = pd.DataFrame([[10, 20, 30], [40, 50, 60], [70, 80, 90]])
    out = promote_numeric_header_row_for_preview(df)
    assert list(out.columns) == [0, 1, 2]
    assert len(out) == 3
