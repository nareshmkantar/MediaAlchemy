"""Planner param coercion for expand_grouped_block and aggregate_weekly group_by."""
from sia.agent.planner import PlanGenerator
from sia.agent.base import ExtractionPlan
from sia.tools.tool_validator import normalize_planner_tool_params, validate_tool_call


def test_normalize_expand_grouped_block_legacy_allocate_params():
    raw = {
        "dimension_columns": ["channel"],
        "metric_columns": ["spends"],
        "method": "equal",
        "weight_col": "impressions",
    }
    out = normalize_planner_tool_params("transform.expand_grouped_block", raw)
    assert "metric_columns" not in out
    assert out["allocations"] == [
        {"metric_col": "spends", "method": "equal", "weight_col": "impressions"}
    ]
    result = validate_tool_call({"tool": "transform.expand_grouped_block", "params": out})
    assert not any("Unknown parameter" in w for w in result.warnings)


def test_planner_merges_aggregate_weekly_group_by_from_template():
    tpl = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": ["publisher"],
        },
        "properties": {
            "date": {"format": "date"},
            "channel": {},
            "publisher": {},
            "spends": {},
            "impressions": {},
        },
    }
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.aggregate_weekly",
                "params": {
                    "date_col": "calendar_date",
                    "group_by_cols": ["channel"],
                    "metric_rules": {"spends": "sum", "impressions": "sum"},
                },
            }
        ],
        reasoning="",
    )
    out = planner._ensure_aggregate_weekly_group_by(plan, tpl)
    gb = out.tool_calls[0]["params"]["group_by_cols"]
    assert "channel" in gb
    assert "publisher" in gb
    assert "date" not in gb
