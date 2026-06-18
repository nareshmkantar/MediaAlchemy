"""Tests for execute_tools weekly prep injection."""

import pandas as pd

from sia.agent.nodes import _inject_infer_daily_before_aggregate_weekly


def test_inject_infer_before_aggregate_when_no_daily_prep():
    state = {"target_template": {"x_scope": {"date_granularity": "weekly"}}}
    tools = [
        {"tool": "transform.format", "params": {}},
        {
            "tool": "transform.aggregate_weekly",
            "params": {
                "date_col": "event_date",
                "metric_rules": {"spend": "sum", "imps": "sum"},
                "group_by_cols": ["channel"],
            },
        },
    ]
    out = _inject_infer_daily_before_aggregate_weekly(tools, state)
    names = [t.get("tool") for t in out if isinstance(t, dict)]
    assert names == [
        "transform.format",
        "transform.infer_granularity_expand_to_daily",
        "transform.aggregate_weekly",
    ]
    infer = [t for t in out if isinstance(t, dict) and t["tool"] == "transform.infer_granularity_expand_to_daily"][0]
    assert infer["params"]["date_col"] == "event_date"
    assert infer["params"]["value_cols"] == ["spend", "imps"]
    assert infer["params"]["id_cols"] == ["channel"]
    ag = [t for t in out if isinstance(t, dict) and t["tool"] == "transform.aggregate_weekly"][0]
    assert ag["params"]["date_col"] == "calendar_date"


def test_no_inject_when_expand_already_in_plan():
    state = {"target_template": {"x_scope": {"date_granularity": "weekly"}}}
    tools = [
        {
            "tool": "transform.expand_period_to_daily",
            "params": {"date_col": "d", "value_cols": ["x"], "input_granularity": "monthly"},
        },
        {"tool": "transform.aggregate_weekly", "params": {"date_col": "calendar_date", "metric_rules": {"x": "sum"}}},
    ]
    out = _inject_infer_daily_before_aggregate_weekly(tools, state)
    assert len(out) == 2


def test_no_inject_when_template_target_daily():
    state = {"target_template": {"x_scope": {"date_granularity": "daily"}}}
    tools = [
        {"tool": "transform.aggregate_weekly", "params": {"date_col": "d", "metric_rules": {"x": "sum"}}},
    ]
    out = _inject_infer_daily_before_aggregate_weekly(tools, state)
    assert len(out) == 1


def test_inject_date_range_daily_when_two_column_range_inferred():
    tpl = {
        "x_scope": {"date_granularity": "weekly", "date_columns": ["ps", "pe"], "metrics": ["m"]},
        "properties": {
            "ps": {"type": "date"},
            "pe": {"type": "date"},
            "m": {"type": "number"},
        },
    }
    df = pd.DataFrame(
        {
            "s": pd.date_range("2024-01-01", periods=5, freq="7D"),
            "e": pd.date_range("2024-01-07", periods=5, freq="7D"),
            "v": [70.0, 70.0, 70.0, 70.0, 70.0],
        }
    )
    state = {
        "target_template": tpl,
        "context_packet": {
            "target_template": tpl,
            "approved_mappings": [
                {"source_column": "s", "target_column": "ps", "decision": "keep"},
                {"source_column": "e", "target_column": "pe", "decision": "keep"},
                {"source_column": "v", "target_column": "m", "decision": "keep"},
            ],
            "planning_summary": {"source_summary": {}},
        },
        "current_df": df,
    }
    tools = [
        {
            "tool": "transform.aggregate_weekly",
            "params": {"date_col": "s", "metric_rules": {"v": "sum"}},
        },
    ]
    out = _inject_infer_daily_before_aggregate_weekly(tools, state)
    names = [t.get("tool") for t in out if isinstance(t, dict)]
    assert names == ["transform.date_range_to_weekly", "transform.aggregate_weekly"]
    dr = [t for t in out if isinstance(t, dict) and t["tool"] == "transform.date_range_to_weekly"][0]
    assert dr["params"]["granularity"] == "daily"
    assert dr["params"]["start_date_col"] == "s"
    assert dr["params"]["end_date_col"] == "e"
    assert dr["params"]["value_cols"] == ["v"]
    assert dr["params"]["date_column"] == "calendar_date"
    ag = [t for t in out if isinstance(t, dict) and t["tool"] == "transform.aggregate_weekly"][0]
    assert ag["params"]["date_col"] == "calendar_date"


def test_inject_expand_date_range_to_daily_when_period_span_metadata():
    tpl = {
        "x_scope": {"date_granularity": "weekly", "date_columns": ["ps", "pe"], "metrics": ["m"]},
        "properties": {
            "ps": {"type": "date"},
            "pe": {"type": "date"},
            "m": {"type": "number"},
        },
    }
    df = pd.DataFrame(
        {
            "s": pd.date_range("2024-01-01", periods=5, freq="7D"),
            "e": pd.date_range("2024-01-07", periods=5, freq="7D"),
            "v": [70.0, 70.0, 70.0, 70.0, 70.0],
        }
    )
    state = {
        "target_template": tpl,
        "source_metadata": {"date_shape": "period_span"},
        "context_packet": {
            "target_template": tpl,
            "approved_mappings": [
                {"source_column": "s", "target_column": "ps", "decision": "keep"},
                {"source_column": "e", "target_column": "pe", "decision": "keep"},
                {"source_column": "v", "target_column": "m", "decision": "keep"},
            ],
            "planning_summary": {"source_summary": {}},
        },
        "current_df": df,
    }
    tools = [
        {
            "tool": "transform.aggregate_weekly",
            "params": {"date_col": "s", "metric_rules": {"v": "sum"}},
        },
    ]
    out = _inject_infer_daily_before_aggregate_weekly(tools, state)
    names = [t.get("tool") for t in out if isinstance(t, dict)]
    assert names == ["transform.expand_date_range_to_daily", "transform.aggregate_weekly"]
    dr = [t for t in out if isinstance(t, dict) and t["tool"] == "transform.expand_date_range_to_daily"][0]
    assert "granularity" not in dr["params"]
    assert dr["params"]["start_date_col"] == "s"
    assert dr["params"]["end_date_col"] == "e"
