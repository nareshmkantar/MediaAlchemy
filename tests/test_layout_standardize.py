"""Standardize messy planning-matrix sheets into Date + Dimensions + Metrics."""

import pandas as pd

from sia.agent.layout_standardize import (
    DATE_COLUMN,
    needs_standardize_step,
    standardize_messy_layout,
)
from sia.agent.sheet_layout_class import detect_layout_complexity
from tests.test_sheet_layout_class import _planning_matrix_df


def test_planning_matrix_needs_standardize_step():
    report = detect_layout_complexity(_planning_matrix_df(), {"merged_ranges": ["A1:B4", "D2:E2"]})
    assert needs_standardize_step(report) is True


def test_flat_table_skips_standardize_step():
    df = pd.DataFrame(
        [
            ["Date", "Channel", "Spend"],
            ["2025-01-01", "TV", 10],
            ["2025-01-02", "TV", 12],
        ]
    )
    report = detect_layout_complexity(df, {"merged_ranges": []})
    assert needs_standardize_step(report) is False


def test_planning_matrix_becomes_date_dims_metrics():
    df = _planning_matrix_df()
    report = detect_layout_complexity(df, {"merged_ranges": ["A1:B4", "D2:E2"]})
    result = standardize_messy_layout(df, report)
    assert result["ok"] is True
    out = result["dataframe"]
    roles = result["column_roles"]
    assert DATE_COLUMN in out.columns
    assert roles[DATE_COLUMN] == "date"
    assert any(role == "dimension" for role in roles.values())
    metric_cols = [c for c, role in roles.items() if role == "metric"]
    assert "GRPs" in metric_cols
    assert "Weekly costs" in metric_cols
    # Far-right noise island must not become a metric column
    assert not any("4807274" in str(c) for c in out.columns)
    # One row per period × dimension combo (4 weeks × 1 media)
    assert len(out) == 4
    jan_w1 = out[out[DATE_COLUMN].astype(str).str.contains("January") & out[DATE_COLUMN].astype(str).str.contains("W1")]
    assert not jan_w1.empty
    assert float(jan_w1.iloc[0]["GRPs"]) == 75.7
    assert float(jan_w1.iloc[0]["Weekly costs"]) == 100


def test_noise_and_title_excluded():
    df = _planning_matrix_df()
    report = detect_layout_complexity(df, {"merged_ranges": ["A1:B4"]})
    result = standardize_messy_layout(df, report)
    preview_text = " ".join(
        str(v) for row in result["preview"]["rows"] for v in row.values()
    )
    assert "FOR INTERNAL USE ONLY" not in preview_text
    assert "4807274" not in preview_text


def test_user_can_rename_dimensions():
    df = _planning_matrix_df()
    report = detect_layout_complexity(df, {"merged_ranges": []})
    axes = report.get("axes") or {}
    result = standardize_messy_layout(
        df,
        report,
        overrides={
            "stub_columns": list(axes.get("stub_columns") or []),
            "dimension_names": ["PO", "Media"][: len(axes.get("stub_columns") or [])],
        },
    )
    assert result["ok"] is True
    for name in result["assignment"]["dimension_names"]:
        assert name in result["dataframe"].columns
