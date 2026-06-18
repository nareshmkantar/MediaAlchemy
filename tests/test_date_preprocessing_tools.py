import json

import pandas as pd

from sia.agent.planner import PlanGenerator
from sia.tools.tool_validator import TOOL_SCHEMAS, normalize_tool_name, validate_tool_call
from sia.tools.transformation_tools import TransformationTools


def test_build_date_from_parts_month_name_and_day():
    df = pd.DataFrame(
        [
            {"Year": 2026, "Month": "Apr", "Day": 15, "channel": "search", "spend": 100.0},
            {"Year": 2026, "Month": "May", "Day": 1, "channel": "social", "spend": 200.0},
        ]
    )
    res = TransformationTools.build_date_from_parts(
        df,
        year_col="Year",
        month_col="Month",
        day_col="Day",
        target_date_col="event_date",
    )
    assert res.success, res.message
    out = res.data
    assert list(pd.to_datetime(out["event_date"]).dt.strftime("%Y-%m-%d")) == ["2026-04-15", "2026-05-01"]


def test_build_date_from_parts_quarter_defaults_to_first_day():
    df = pd.DataFrame([{"year": 2026, "quarter": "Q2", "spend": 900.0}])
    res = TransformationTools.build_date_from_parts(
        df,
        year_col="year",
        quarter_col="quarter",
        target_date_col="period_date",
    )
    assert res.success, res.message
    out = res.data
    assert pd.Timestamp(out.loc[0, "period_date"]) == pd.Timestamp("2026-04-01")


def test_expand_period_to_daily_monthly_preserves_total():
    df = pd.DataFrame([{"period_date": "2026-04-01", "channel": "search", "spend": 300.0}])
    expanded = TransformationTools.expand_period_to_daily(
        df,
        date_col="period_date",
        value_cols=["spend"],
        input_granularity="monthly",
        id_cols=["channel"],
        date_column="calendar_date",
    )
    assert expanded.success, expanded.message
    out = expanded.data
    assert len(out) == 30
    assert abs(out["spend"].sum() - 300.0) < 1e-6
    assert pd.Timestamp(out["calendar_date"].min()) == pd.Timestamp("2026-04-01")
    assert pd.Timestamp(out["calendar_date"].max()) == pd.Timestamp("2026-04-30")


def test_infer_granularity_expand_monthly_matches_row_count_of_explicit_expand():
    df = pd.DataFrame(
        {
            "period_date": pd.date_range("2026-01-01", periods=3, freq="MS"),
            "channel": "search",
            "spend": [100.0, 200.0, 300.0],
        }
    )
    inf = TransformationTools.infer_granularity_expand_to_daily(
        df, date_col="period_date", value_cols=["spend"], id_cols=["channel"]
    )
    assert inf.success, inf.message
    exp = TransformationTools.expand_period_to_daily(
        df,
        date_col="period_date",
        value_cols=["spend"],
        input_granularity="monthly",
        id_cols=["channel"],
    )
    assert exp.success
    assert len(inf.data) == len(exp.data)
    assert abs(float(inf.data["spend"].sum()) - 600.0) < 1e-5


def test_infer_granularity_expand_daily_noop():
    df = pd.DataFrame({"d": pd.date_range("2026-01-01", periods=12, freq="D"), "metric": range(12)})
    r = TransformationTools.infer_granularity_expand_to_daily(df, "d", ["metric"])
    assert r.success, r.message
    assert len(r.data) == len(df)
    assert r.changes_made.get("expanded") is False


def test_infer_granularity_irregular_sporadic_treated_as_daily():
    """Median gap ~2.5d → irregular; should succeed as sporadic daily (no calendar expand)."""
    df = pd.DataFrame(
        {
            "d": pd.to_datetime(["2026-01-01", "2026-01-04", "2026-01-07", "2026-01-10"]),
            "metric": [10.0, 20.0, 30.0, 40.0],
        }
    )
    r = TransformationTools.infer_granularity_expand_to_daily(df, "d", ["metric"])
    assert r.success, r.message
    assert len(r.data) == 4
    assert r.changes_made.get("treated_as") == "daily"
    assert "sporadic" in r.message.lower() or r.changes_made.get("inferred_cadence") == "irregular"


def test_infer_granularity_expand_range_strings_points_to_range_tool():
    df = pd.DataFrame(
        {
            "flight": ["2024-01-01 to 2024-01-07", "2024-01-08 to 2024-01-14"],
            "v": [10.0, 20.0],
        }
    )
    r = TransformationTools.infer_granularity_expand_to_daily(df, "flight", ["v"])
    assert not r.success
    assert "date_range_to_weekly" in r.message.lower()


def test_expand_period_to_daily_then_aggregate_weekly():
    df = pd.DataFrame([{"period_date": "2026-04-01", "channel": "search", "spend": 300.0}])
    expanded = TransformationTools.expand_period_to_daily(
        df,
        date_col="period_date",
        value_cols=["spend"],
        input_granularity="monthly",
        id_cols=["channel"],
    )
    assert expanded.success, expanded.message

    weekly = TransformationTools.aggregate_weekly(
        expanded.data,
        date_col="calendar_date",
        group_by_cols=["channel"],
        metric_rules={"spend": "sum"},
    )
    assert weekly.success, weekly.message
    out = weekly.data
    assert abs(out["spend"].sum() - 300.0) < 1e-6
    assert pd.Timestamp(out["calendar_date"].min()) == pd.Timestamp("2026-03-30")
    assert "week_end" not in out.columns


def test_new_date_tools_registered_in_validator():
    assert "transform.build_date_from_parts" in TOOL_SCHEMAS
    assert "transform.expand_period_to_daily" in TOOL_SCHEMAS
    assert "transform.infer_granularity_expand_to_daily" in TOOL_SCHEMAS

    canonical, corrected = normalize_tool_name("build_date_from_parts")
    assert canonical == "transform.build_date_from_parts"
    assert corrected is True

    build_result = validate_tool_call(
        {
            "tool": "transform.build_date_from_parts",
            "params": {
                "year_col": "Year",
                "month_col": "Month",
                "target_date_col": "calendar_date",
            },
        }
    )
    assert build_result.valid, build_result.errors

    expand_result = validate_tool_call(
        {
            "tool": "transform.expand_period_to_daily",
            "params": {
                "date_col": "period_date",
                "value_cols": ["spend"],
                "input_granularity": "monthly",
            },
        }
    )
    assert expand_result.valid, expand_result.errors

    infer_result = validate_tool_call(
        {
            "tool": "transform.infer_granularity_expand_to_daily",
            "params": {"date_col": "period_date", "value_cols": ["spend"]},
        }
    )
    assert infer_result.valid, infer_result.errors


class _FakeLLMClient:
    def __init__(self):
        self.last_prompt = None

    def generate_content(self, prompt: str, **kwargs):
        self.last_prompt = prompt
        return type(
            "FakeResponse",
            (),
            {
                "text": json.dumps(
                    {
                        "confidence": 0.9,
                        "tool_calls": [],
                        "business_rule_actions": [],
                        "approval_items": [],
                        "expected_schema": ["week_start", "spend"],
                    }
                )
            },
        )()


def test_plan_generator_mentions_new_weekly_preprocessing_tools():
    client = _FakeLLMClient()
    planner = PlanGenerator(client)

    target_template = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel"],
            "metrics": ["spend"],
            "supporting_columns": [],
            "date_granularity": "weekly",
        },
        "aggregation_logic": {"metric_rules": {"spend": "sum"}},
        "business_logic": {"column_rules": []},
        "properties": {"date": {}, "channel": {}, "spend": {}},
    }
    structure_analysis = {
        "overall_structure": "flat",
        "confidence": 0.95,
        "tables": [],
        "visual_patterns": {},
        "column_analysis": [
            {"col": 0, "name": "Year", "type": "numeric"},
            {"col": 1, "name": "Month", "type": "temporal"},
            {"col": 2, "name": "Spend", "type": "numeric"},
        ],
        "reasoning": "",
    }
    context_packet = {
        "planning_summary": {
            "mapping_summary": {"mapped_columns": [], "excluded_columns": []},
            "rules_summary": [],
            "source_summary": {
                "file_name": "sample.xlsx",
                "sheet_name": "Sheet1",
                "date_granularity": "monthly",
                "uid": ["date", "channel"],
            },
            "user_notes": [],
        }
    }

    planner.generate(structure_analysis, target_template=target_template, context_packet=context_packet)

    assert client.last_prompt is not None
    assert "transform.build_date_from_parts" in client.last_prompt
    assert "transform.expand_period_to_daily" in client.last_prompt
    assert "transform.aggregate_weekly" in client.last_prompt
    assert "single Monday-aligned weekly date column" in client.last_prompt


class _FakeRenamePlanLLMClient:
    def generate_content(self, prompt: str, **kwargs):
        return type(
            "FakeResponse",
            (),
            {
                "text": json.dumps(
                    {
                        "confidence": 0.9,
                        "tool_calls": [
                            {
                                "tool": "transform.rename",
                                "params": {
                                    "mapping": {
                                        "Views": "impressions",
                                        "Market": "region",
                                    }
                                },
                                "description": "Rename mapped columns",
                            }
                        ],
                        "business_rule_actions": [],
                        "approval_items": [],
                        "expected_schema": ["impressions"],
                    }
                )
            },
        )()


def test_plan_generator_rewrites_rename_step_to_approved_mapping_columns():
    planner = PlanGenerator(_FakeRenamePlanLLMClient())
    structure_analysis = {
        "overall_structure": "flat",
        "confidence": 0.95,
        "tables": [],
        "visual_patterns": {},
        "column_analysis": [
            {"col": 0, "name": "performance_views", "type": "numeric"},
            {"col": 1, "name": "Market", "type": "text"},
        ],
        "reasoning": "",
    }
    context_packet = {
        "approved_mappings": [
            {
                "source_column": "performance_views",
                "target_column": "impressions",
                "decision": "keep",
            },
            {
                "source_column": "Market",
                "target_column": "No match",
                "decision": "keep",
            },
        ],
        "planning_summary": {
            "mapping_summary": {
                "mapped_columns": ["performance_views -> impressions"],
                "excluded_columns": [],
            },
            "rules_summary": [],
            "source_summary": {
                "file_name": "sample.xlsx",
                "sheet_name": "Sheet1",
                "date_granularity": "daily",
                "uid": ["date"],
                "prepared_columns": ["performance_views", "Market"],
                "header_derivation": {
                    "derived": True,
                    "message": "Merged Excel rows 1 and 2 into column names where applicable.",
                },
            },
            "user_notes": [],
        },
    }

    plan = planner.generate(structure_analysis, target_template={"properties": {"impressions": {}}}, context_packet=context_packet)

    assert len(plan.tool_calls) == 1
    rename_mapping = plan.tool_calls[0]["params"]["mapping"]
    assert rename_mapping == {"performance_views": "impressions"}


class _FakeWeeklyPlanLLMClient:
    def generate_content(self, prompt: str, **kwargs):
        return type(
            "FakeResponse",
            (),
            {
                "text": json.dumps(
                    {
                        "confidence": 0.9,
                        "tool_calls": [
                            {
                                "tool": "transform.aggregate_weekly",
                                "params": {
                                    "date_col": "date",
                                    "group_by_cols": ["channel", "Market"],
                                    "metric_rules": {"spend": "sum"},
                                },
                                "description": "Weekly rollup",
                            }
                        ],
                        "business_rule_actions": [],
                        "approval_items": [],
                        "expected_schema": ["week_start", "channel", "Market", "spend"],
                    }
                )
            },
        )()


def test_plan_generator_inserts_fill_forward_before_weekly_rollup_for_sparse_dimensions():
    planner = PlanGenerator(_FakeWeeklyPlanLLMClient())
    structure_analysis = {
        "overall_structure": "flat",
        "confidence": 0.95,
        "tables": [],
        "visual_patterns": {},
        "column_analysis": [
            {"col": 0, "name": "date", "type": "temporal"},
            {"col": 1, "name": "channel", "type": "text"},
            {"col": 2, "name": "Market", "type": "text"},
            {"col": 3, "name": "spend", "type": "numeric"},
        ],
        "reasoning": "",
    }
    target_template = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel"],
            "metrics": ["spend"],
            "supporting_columns": ["market"],
            "date_granularity": "weekly",
        },
        "aggregation_logic": {"metric_rules": {"spend": "sum"}},
        "properties": {"week_start": {}, "channel": {}, "market": {}, "spend": {}},
    }
    context_packet = {
        "planning_summary": {
            "mapping_summary": {"mapped_columns": ["Market -> market"], "excluded_columns": []},
            "rules_summary": [],
            "source_summary": {
                "file_name": "sample.xlsx",
                "sheet_name": "Sheet1",
                "date_granularity": "daily",
                "uid": ["date", "channel"],
                "sparse_dimension_columns": [
                    {"source_column": "Market", "target_column": "market", "blank_ratio": 0.5}
                ],
            },
            "user_notes": [],
        },
    }

    plan = planner.generate(structure_analysis, target_template=target_template, context_packet=context_packet)

    tool_names = [t["tool"] for t in plan.tool_calls if isinstance(t, dict)]
    assert "transform.aggregate_weekly" in tool_names
    layout_tools = {"transform.fill_merged", "transform.expand_grouped_block"}
    layout_idx = next(i for i, n in enumerate(tool_names) if n in layout_tools)
    weekly_idx = tool_names.index("transform.aggregate_weekly")
    assert layout_idx < weekly_idx
    layout_step = plan.tool_calls[layout_idx]
    if layout_step["tool"] == "transform.fill_merged":
        assert layout_step["params"]["direction"] == "down"
        assert layout_step["params"]["columns"] == ["Market"]
    else:
        assert layout_step["tool"] == "transform.expand_grouped_block"
        assert "Market" in (layout_step["params"].get("dimension_columns") or [])
