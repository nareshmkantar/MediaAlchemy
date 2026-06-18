"""Final layout: column reorder and uid-hierarchy row sort."""
import pandas as pd

from sia.agent.planner import PlanGenerator
from sia.agent.base import ExtractionPlan
from sia.agent.target_template_utils import (
    final_output_column_order,
    final_output_sort_columns,
    prune_dataframe_to_template,
    reorder_dataframe_columns,
    sort_dataframe_by_template,
)
from sia.agent.nodes import _repair_final_layout_tool_params
from sia.tools.transformation_tools import TransformationTools

TEMPLATE = {
    "x_scope": {
        "uid_hierarchy": ["date", "channel"],
        "metrics": ["spends", "impressions"],
        "supporting_columns": ["region"],
    },
    "properties": {
        "date": {"format": "date"},
        "channel": {},
        "spends": {},
        "impressions": {},
        "region": {},
    },
    "business_logic": {"column_rules": []},
    "aggregation_logic": {},
}


def test_final_output_column_order_matches_collation_merge():
    assert final_output_column_order(TEMPLATE) == [
        "date",
        "channel",
        "region",
        "spends",
        "impressions",
    ]


def test_final_output_sort_columns_date_then_uid():
    assert final_output_sort_columns(TEMPLATE) == ["date", "channel"]


def test_reorder_dataframe_columns_puts_metrics_last():
    df = pd.DataFrame(
        [{"impressions": 2, "date": "2026-01-02", "channel": "b", "spends": 1.0, "region": "EU"}]
    )
    out, notes = reorder_dataframe_columns(df, TEMPLATE)
    assert list(out.columns) == ["date", "channel", "region", "spends", "impressions"]
    assert notes


def test_sort_dataframe_by_template_orders_rows():
    df = pd.DataFrame(
        [
            {"date": "2026-01-03", "channel": "z", "spends": 1.0, "impressions": 1},
            {"date": "2026-01-01", "channel": "a", "spends": 2.0, "impressions": 2},
            {"date": "2026-01-02", "channel": "a", "spends": 3.0, "impressions": 3},
        ]
    )
    out, notes = sort_dataframe_by_template(df, TEMPLATE)
    assert list(out["date"].astype(str)) == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert list(out["channel"]) == ["a", "a", "z"]
    assert notes


def test_prune_always_reorders_even_without_drops():
    df = pd.DataFrame(
        [{"impressions": 2, "date": "2026-01-01", "channel": "x", "spends": 1.0, "region": "NA"}]
    )
    out, notes = prune_dataframe_to_template(df, TEMPLATE)
    assert list(out.columns) == ["date", "channel", "region", "spends", "impressions"]
    assert any("reordered" in n for n in notes)


def test_transform_reorder_and_sort_tools():
    df = pd.DataFrame(
        [
            {"impressions": 2, "spends": 1.0, "channel": "b", "date": "2026-01-02"},
            {"impressions": 4, "spends": 3.0, "channel": "a", "date": "2026-01-01"},
        ]
    )
    r1 = TransformationTools.reorder_columns_layout(
        df, column_order=["date", "channel", "spends", "impressions"]
    )
    assert r1.success
    r2 = TransformationTools.sort_rows(r1.data, sort_columns=["date", "channel"])
    assert r2.success
    assert list(r2.data["channel"]) == ["a", "b"]


def test_transform_reorder_skips_missing_week_start_when_date_exists():
    df = pd.DataFrame(
        [{"date": "2026-01-02", "publisher": "x", "channel": "tv", "spends": 1.0}]
    )
    result = TransformationTools.reorder_columns_layout(
        df, column_order=["week_start", "publisher", "channel", "spends"]
    )
    assert result.success
    assert list(result.data.columns) == ["publisher", "channel", "spends", "date"]
    assert result.changes_made["skipped_missing"] == ["week_start"]


def test_final_layout_repair_maps_week_start_to_date_column():
    df = pd.DataFrame(
        [{"date": "2026-01-02", "publisher": "x", "channel": "tv", "spends": 1.0}]
    )
    state = {
        "target_template": {
            "x_scope": {
                "uid_hierarchy": ["week_start", "publisher", "channel"],
                "metrics": ["spends"],
                "supporting_columns": [],
            },
            "properties": {
                "week_start": {"format": "date"},
                "publisher": {},
                "channel": {},
                "spends": {},
            },
        }
    }
    params = _repair_final_layout_tool_params(
        state,
        "transform.reorder_columns",
        {"column_order": ["week_start", "publisher", "channel", "spends"]},
        df,
    )
    assert params["column_order"] == ["date", "publisher", "channel", "spends"]


def test_resolve_sort_columns_calendar_date_maps_to_date_column():
    df = pd.DataFrame(
        [{"DATE": "2026-01-02", "PUBLISHER": "x", "CHANNEL": "tv", "SPENDS": 1.0}]
    )
    template = {
        "x_scope": {
            "uid_hierarchy": ["calendar_date", "publisher", "channel"],
            "metrics": ["spends"],
            "supporting_columns": [],
        },
        "properties": {
            "calendar_date": {"format": "date"},
            "publisher": {},
            "channel": {},
            "spends": {},
        },
    }
    approved = [
        {
            "source_column": "DATE",
            "target_column": "calendar_date",
            "decision": "keep",
        }
    ]
    from sia.agent.target_template_utils import final_output_sort_columns

    keys = final_output_sort_columns(
        template, present_columns=list(df.columns), approved_mappings=approved
    )
    assert keys == ["DATE", "PUBLISHER", "CHANNEL"]


def test_final_layout_repair_ignores_llm_metric_first_order():
    df = pd.DataFrame(
        [
            {
                "SPENDS": 1.0,
                "DATE": "2026-01-02",
                "PUBLISHER": "x",
                "CHANNEL": "tv",
                "IMPRESSIONS": 2,
            }
        ]
    )
    state = {
        "target_template": {
            "x_scope": {
                "uid_hierarchy": ["date", "publisher", "channel"],
                "metrics": ["spends", "impressions"],
                "supporting_columns": [],
            },
            "properties": {
                "date": {"format": "date"},
                "publisher": {},
                "channel": {},
                "spends": {},
                "impressions": {},
            },
        },
        "approved_mappings": [
            {"source_column": "DATE", "target_column": "date", "decision": "keep"},
            {"source_column": "cost", "target_column": "spends", "decision": "keep"},
        ],
    }
    params = _repair_final_layout_tool_params(
        state,
        "transform.reorder_columns",
        {"column_order": ["spends", "date", "publisher", "channel", "impressions"]},
        df,
    )
    assert params["column_order"] == ["DATE", "PUBLISHER", "CHANNEL", "SPENDS", "IMPRESSIONS"]


def test_final_layout_repair_fills_empty_sort_columns():
    df = pd.DataFrame(
        [{"DATE": "2026-01-03", "CHANNEL": "z", "SPENDS": 1.0, "IMPRESSIONS": 1}]
    )
    state = {"target_template": TEMPLATE}
    params = _repair_final_layout_tool_params(
        state,
        "transform.sort_rows",
        {"sort_columns": [], "ascending": True},
        df,
    )
    assert params["sort_columns"] == ["DATE", "CHANNEL"]
    result = TransformationTools.sort_rows(df, sort_columns=params["sort_columns"])
    assert result.success


def test_planner_collapses_duplicate_rename_steps():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {
                "tool": "transform.rename",
                "params": {"mapping": {"DATE": "date", "cost": "spends"}},
            },
            {"tool": "transform.type_cast", "params": {}},
            {"tool": "transform.rename", "params": {"mapping": {"cost": "spends"}}},
        ],
        reasoning="",
    )
    out = planner._collapse_plan_rename_tools(plan)
    renames = [t for t in out.tool_calls if t.get("tool") == "transform.rename"]
    assert len(renames) == 1
    assert renames[0]["params"]["mapping"]["DATE"] == "date"
    assert renames[0]["params"]["mapping"]["cost"] == "spends"
    names = [t.get("tool") for t in out.tool_calls]
    assert names.index("transform.rename") == names.index("layout.extract") + 1


def test_planner_inserts_final_layout_before_verify():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {"tool": "transform.format", "params": {"format_map": {}}},
            {"tool": "verify.schema", "params": {}},
        ],
        reasoning="",
    )
    out = planner._ensure_final_layout_tools(plan, TEMPLATE)
    names = [t.get("tool") for t in out.tool_calls]
    assert names.index("transform.reorder_columns") < names.index("verify.schema")
    assert names.index("transform.sort_rows") < names.index("verify.schema")
    reorder = next(t for t in out.tool_calls if t.get("tool") == "transform.reorder_columns")
    assert "date" in reorder["params"]["column_order"][0]


def test_planner_patches_existing_sort_when_llm_left_empty():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {
                "tool": "transform.sort_rows",
                "params": {"sort_columns": [], "ascending": True},
            },
            {"tool": "verify.schema", "params": {}},
        ],
        reasoning="",
    )
    out = planner._ensure_final_layout_tools(plan, TEMPLATE)
    sort_step = next(t for t in out.tool_calls if t.get("tool") == "transform.sort_rows")
    assert sort_step["params"]["sort_columns"] == ["date", "channel"]
