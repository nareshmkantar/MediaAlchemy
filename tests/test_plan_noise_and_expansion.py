"""Replan noise dedupe, daily-expansion column retention, verify.schema template injection."""
import pandas as pd

from sia.agent.nodes import (
    _enrich_tool_params_from_template,
    _last_column_typing_tool_index,
)
from sia.tools.tool_validator import dedupe_redundant_tool_calls
from sia.tools.transformation_tools import TransformationTools


# ===== Redundant tool call dedupe (replanner restates the whole plan) =====


def test_runtime_gate_is_after_last_column_typing_tool():
    calls = [
        {"tool": "layout.extract", "params": {}},
        {"tool": "transform.rename", "params": {}},
        {"tool": "transform.type_cast", "params": {}},
        {"tool": "transform.add_column", "params": {}},
        {"tool": "transform.infer_granularity_expand_to_daily", "params": {}},
        {"tool": "verify.schema", "params": {}},
    ]
    assert _last_column_typing_tool_index(calls) == 3


def test_runtime_gate_absent_without_column_typing_stage():
    calls = [
        {"tool": "layout.extract", "params": {}},
        {"tool": "verify.schema", "params": {}},
    ]
    assert _last_column_typing_tool_index(calls) is None


def test_dedupe_drops_repeated_add_column_with_same_params():
    calls = [
        {"tool": "transform.add_column", "params": {"target_column": "channel", "value": "Pinterest", "when": "missing"}},
        {"tool": "transform.add_column", "params": {"target_column": "market", "value": "US", "when": "missing"}},
        {"tool": "transform.add_column", "params": {"target_column": "channel", "value": "Pinterest", "when": "missing"}},
    ]
    kept, notes = dedupe_redundant_tool_calls(calls)
    assert len(kept) == 2
    assert [c["params"]["target_column"] for c in kept] == ["channel", "market"]
    assert len(notes) == 1


def test_dedupe_keeps_add_column_for_different_values():
    calls = [
        {"tool": "transform.add_column", "params": {"target_column": "channel", "value": "Pinterest"}},
        {"tool": "transform.add_column", "params": {"target_column": "channel", "value": "Meta"}},
    ]
    kept, notes = dedupe_redundant_tool_calls(calls)
    assert len(kept) == 2
    assert not notes


def test_dedupe_collapses_duplicated_plan_from_replanner():
    plan = [
        {"tool": "layout.extract", "params": {"row_start": 1}},
        {"tool": "transform.rename", "params": {"mapping": {"Spend": "spends"}}},
        {"tool": "transform.type_cast", "params": {"type_map": {"spends": "float"}}},
    ]
    kept, notes = dedupe_redundant_tool_calls(plan + plan)
    assert len(kept) == 3
    assert len(notes) == 3


def test_dedupe_preserves_non_idempotent_tools():
    calls = [
        {"tool": "transform.calculate", "params": {"target_column": "x", "expression": "a+b"}},
        {"tool": "transform.calculate", "params": {"target_column": "x", "expression": "a+b"}},
    ]
    kept, _ = dedupe_redundant_tool_calls(calls)
    assert len(kept) == 2


# ===== Daily expansion must not drop columns added upstream =====

def _monthly_frame():
    return pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-02-01"],
            "advertiser": ["Acme", "Acme"],
            "channel": ["Pinterest", "Pinterest"],
            "market": ["US", "US"],
            "spends": [310.0, 280.0],
            "impressions": [3100, 2800],
        }
    )


def test_expand_period_to_daily_keeps_columns_missing_from_id_cols():
    df = _monthly_frame()
    result = TransformationTools.expand_period_to_daily(
        df,
        date_col="date",
        value_cols=["spends", "impressions"],
        input_granularity="monthly",
        id_cols=["advertiser"],
    )
    assert result.success
    out = result.data
    for col in ("advertiser", "channel", "market"):
        assert col in out.columns, f"{col} was dropped by daily expansion"
    assert set(out["channel"].unique()) == {"Pinterest"}
    assert "calendar_date" in out.columns
    assert len(out) == 31 + 28


def test_infer_granularity_expand_keeps_added_columns():
    df = _monthly_frame()
    result = TransformationTools.infer_granularity_expand_to_daily(
        df,
        date_col="date",
        value_cols=["spends", "impressions"],
        id_cols=["advertiser"],
    )
    assert result.success, result.message
    out = result.data
    assert "channel" in out.columns
    assert "market" in out.columns


def test_expand_period_to_daily_default_id_cols_unchanged():
    df = _monthly_frame()
    result = TransformationTools.expand_period_to_daily(
        df, date_col="date", value_cols=["spends"], input_granularity="monthly"
    )
    assert result.success
    out = result.data
    # impressions is not a value_col, so it is carried through as an id column
    for col in ("advertiser", "channel", "market", "impressions"):
        assert col in out.columns
    assert "date" not in out.columns


# ===== verify.schema must validate against the uploaded template =====

def test_verify_schema_params_replaced_with_target_template():
    template = {
        "properties": {
            "spends": {"type": "number", "minimum": 0},
            "impressions": {"type": "number", "minimum": 0},
        }
    }
    planner_params = {"schema": {"type": "object", "properties": {"spends": {"type": "number"}}}}
    out = _enrich_tool_params_from_template(
        {"target_template": template}, "verify.schema", planner_params
    )
    assert out["schema"]["properties"]["spends"]["minimum"] == 0
    assert "impressions" in out["schema"]["properties"]


def test_verify_schema_params_untouched_without_template():
    planner_params = {"schema": {"properties": {"spends": {"type": "number"}}}}
    out = _enrich_tool_params_from_template({}, "verify.schema", planner_params)
    assert out["schema"] == planner_params["schema"]


def test_injected_template_makes_negative_spends_fail_validation():
    template = {"properties": {"spends": {"type": "number", "minimum": 0}}}
    params = _enrich_tool_params_from_template(
        {"target_template": template},
        "verify.schema",
        {"schema": {"properties": {"spends": {"type": "number"}}}},
    )
    df = pd.DataFrame({"spends": [5.0, -331.898]})
    result = TransformationTools.validate_against_schema(df, params["schema"])
    report = result.changes_made["validation_report"]
    assert report["valid"] is False
    assert report["constraint_violation_count"] == 1
