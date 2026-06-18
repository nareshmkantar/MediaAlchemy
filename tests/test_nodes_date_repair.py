"""Tests for execute-time repair of start/end columns on range expansion tools."""

import pandas as pd

from sia.agent.nodes import _repair_date_range_tool_params


def test_repair_uses_guided_range_columns_when_params_invalid():
    df = pd.DataFrame(
        {
            "Flight Start": ["2026-01-01", "2026-01-08"],
            "Flight End": ["2026-01-07", "2026-01-14"],
            "Spend": [70.0, 70.0],
        }
    )
    state = {
        "source_metadata": {
            "range_start_column": "Flight Start",
            "range_end_column": "Flight End",
        },
        "approved_mappings": [],
        "context_packet": {},
        "target_template": {},
    }
    out = _repair_date_range_tool_params(
        df,
        state,
        {"start_date_col": "WrongStart", "end_date_col": "WrongEnd", "value_cols": ["Spend"]},
    )
    assert out["start_date_col"] == "Flight Start"
    assert out["end_date_col"] == "Flight End"


def test_repair_keeps_params_when_already_valid():
    df = pd.DataFrame({"a": ["2026-01-01"], "b": ["2026-01-02"], "v": [10.0]})
    state = {"source_metadata": {}, "approved_mappings": [], "context_packet": {}, "target_template": {}}
    out = _repair_date_range_tool_params(
        df,
        state,
        {"start_date_col": "a", "end_date_col": "b", "value_cols": ["v"]},
    )
    assert out["start_date_col"] == "a"
    assert out["end_date_col"] == "b"
