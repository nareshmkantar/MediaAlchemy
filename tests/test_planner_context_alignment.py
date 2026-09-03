import pandas as pd

from sia.agent.base import ExtractionPlan
from sia.agent.planner import PlanGenerator
from sia.mcp_server import _result_to_dict
from sia.tools.transformation_tools import ToolResult


def test_adjust_drop_columns_defers_and_protects_name():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {"tool": "transform.drop_columns", "params": {"columns": ["Name", "Market"]}},
            {
                "tool": "transform.classify_metric_level",
                "params": {"block_start_columns": ["Name"]},
            },
            {
                "tool": "transform.allocate_block_metric",
                "params": {"metric_col": "spends", "block_start_columns": ["Name"]},
            },
        ],
        reasoning="",
    )
    out = planner._adjust_drop_columns_for_block_metrics(plan)
    names = [t.get("tool") for t in out.tool_calls]
    assert names.index("transform.drop_columns") > names.index("transform.allocate_block_metric")
    drop_params = next(t for t in out.tool_calls if t.get("tool") == "transform.drop_columns")["params"]
    assert "Name" not in drop_params["columns"]
    assert "Market" in drop_params["columns"]


def test_sanitize_rename_mapping_drops_no_match_targets():
    from sia.agent.target_template_utils import sanitize_rename_mapping

    raw = {
        "Market": "No match",
        "Name": "no match",
        "Performance_Views": "impressions",
    }
    assert sanitize_rename_mapping(raw) == {"Performance_Views": "impressions"}


def test_ensure_rename_from_mappings_strips_llm_no_match_on_merge():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {
                "tool": "transform.rename",
                "params": {
                    "mapping": {
                        "Market": "No match",
                        "Performance_Views": "impressions",
                    }
                },
            },
        ],
        reasoning="",
    )
    context_packet = {
        "approved_mappings": [
            {"source_column": "Performance_Views", "target_column": "impressions", "decision": "keep"},
        ],
    }
    out = planner._ensure_rename_from_mappings(plan, context_packet)
    mapping = out.tool_calls[1]["params"]["mapping"]
    assert "Market" not in mapping
    assert "No match" not in mapping.values()
    assert mapping["Performance_Views"] == "impressions"


def test_ensure_rename_from_mappings_inserts_after_layout_extract():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {"start_row": 1, "end_row": 10}},
            {"tool": "transform.type_cast", "params": {"columns": ["impressions"]}},
        ],
        reasoning="Extract then cast.",
    )
    context_packet = {
        "approved_mappings": [
            {
                "source_column": "Performance_Views",
                "target_column": "impressions",
                "decision": "keep",
            },
            {
                "source_column": "Budgets_Total",
                "target_column": "spends",
                "decision": "keep",
            },
        ],
        "column_gaps": {
            "rename_needed": [
                {"from": "Performance_Views", "to": "impressions"},
                {"from": "Budgets_Total", "to": "spends"},
            ],
        },
    }

    out = planner._ensure_rename_from_mappings(plan, context_packet)
    assert [t.get("tool") for t in out.tool_calls[:3]] == [
        "layout.extract",
        "transform.rename",
        "transform.type_cast",
    ]
    mapping = out.tool_calls[1]["params"]["mapping"]
    assert mapping["Performance_Views"] == "impressions"
    assert mapping["Budgets_Total"] == "spends"
    assert "Inserted transform.rename after layout" in out.reasoning


def test_ensure_rename_from_mappings_merges_existing_rename_at_slot():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {
                "tool": "transform.rename",
                "params": {"mapping": {"Posting date": "date"}},
            },
        ],
        reasoning="",
    )
    context_packet = {
        "approved_mappings": [
            {"source_column": "Performance_Views", "target_column": "impressions", "decision": "keep"},
        ],
    }
    out = planner._ensure_rename_from_mappings(plan, context_packet)
    assert len(out.tool_calls) == 2
    mapping = out.tool_calls[1]["params"]["mapping"]
    assert mapping["Posting date"] == "date"
    assert mapping["Performance_Views"] == "impressions"


def test_align_plan_params_to_context_rewrites_weekly_tool_columns():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.aggregate_weekly",
                "params": {
                    "date_col": "Date",
                    "group_by_cols": ["Market", "publisher_name", "missing_dimension"],
                    "metric_rules": {"spends": "sum", "impressions": "sum"},
                },
            }
        ],
        reasoning="Weekly rollup.",
    )
    context_packet = {
        "approved_mappings": [
            {"source_column": "Date", "target_column": "date", "decision": "Keep"},
            {"source_column": "Market", "target_column": "market", "decision": "Keep"},
            {"source_column": "Spend", "target_column": "spends", "decision": "Keep"},
            {"source_column": "Publisher Name", "target_column": "publisher_name", "decision": "Keep"},
            {"source_column": "Follower count (pt of engagement)", "target_column": "No match", "decision": "Keep"},
            {"source_column": "Exclude Me", "target_column": "No match", "decision": "Discard"},
        ],
        "business_rules": [],
        "planning_summary": {
            "source_summary": {
                "prepared_columns": [
                    "Date",
                    "Market",
                    "Spend",
                    "Publisher Name",
                    "Follower count (pt of engagement)",
                    "Exclude Me",
                ]
            }
        },
    }

    aligned = planner._align_plan_to_approved_mappings(plan, context_packet)
    params = aligned.tool_calls[0]["params"]

    assert params["date_col"] == "date"
    assert params["group_by_cols"] == ["market", "publisher_name"]
    assert params["metric_rules"] == {"spends": "sum"}
    assert "Aligned weekly/date tool params to the approved mapping context." in aligned.reasoning


def test_result_to_dict_serializes_datetime_payloads_for_mcp():
    result = ToolResult(
        success=True,
        data=pd.DataFrame(
            [
                {"date": pd.Timestamp("2026-04-13"), "spends": 123.4},
                {"date": pd.NaT, "spends": 0.0},
            ]
        ),
        message="ok",
    )

    payload = _result_to_dict(result, start_time=0.0, rows_in=2)

    assert payload["ok"] is True
    assert payload["context"]["sample"]["table"][0]["date"] == "2026-04-13T00:00:00"
    assert payload["full_data"][1]["date"] is None


def test_planner_prompt_includes_context_block_snippets(monkeypatch):
    planner = PlanGenerator(llm_client=None)
    captured = {}

    class DummyResponse:
        text = '{"confidence": 0.9, "tool_calls": [], "business_rule_actions": [], "approval_items": [], "expected_schema": []}'

    def fake_generate(prompt, generation_config=None):
        captured["prompt"] = prompt
        return DummyResponse()

    monkeypatch.setattr(planner.llm_wrapper, "generate_content", fake_generate)

    planner.generate(
        structure_analysis={"tables": [], "column_analysis": [], "overall_structure": "flat", "confidence": 0.9},
        examples=[],
        target_template=None,
        context_packet={
            "planning_summary": {
                "mapping_summary": {},
                "layout_summary": {"context_blocks_count": 1},
                "interpreted_context": {
                    "fields": {
                        "modeling_period_start": "2025-01-01",
                        "modeling_period_end": "2025-03-31",
                        "publisher": "Instagram",
                    },
                    "assumptions": ["Use booking date instead of invoice date"],
                    "evidence": [{"field": "publisher", "value": "Instagram", "block_label": "Top metadata"}],
                },
                "rules_summary": [],
                "source_summary": {},
                "user_notes": [],
            },
            "context_block_snippets": [
                {
                    "block_id": "meta_1",
                    "block_label": "Top metadata",
                    "summary": "Modeling Period | 2025-01-01 to 2025-03-31",
                    "text_preview": ["Modeling Period | 2025-01-01 to 2025-03-31"],
                    "non_empty_cells": [{"row": 0, "col": 0, "value": "Modeling Period"}],
                }
            ],
        },
    )

    assert "Context block evidence" in captured["prompt"] or "Top metadata" in captured["prompt"]
    assert "Local context" in captured["prompt"] or "publisher" in captured["prompt"]
    assert "Top metadata" in captured["prompt"]
    assert "Modeling Period | 2025-01-01 to 2025-03-31" in captured["prompt"]
    assert "publisher" in captured["prompt"].lower()
    assert "Use booking date instead of invoice date" in captured["prompt"]


def test_finalize_expected_columns_unions_template_allowlist_with_llm_schema():
    class StubLLM:
        def generate_content(self, prompt, **kwargs):
            return type(
                "obj",
                (),
                {
                    "text": (
                        '{"confidence": 0.9, "tool_calls": [], "business_rule_actions": [], '
                        '"approval_items": [], '
                        '"expected_schema": ["channel", "publisher", "region", "spends", "impressions", "Name"]}'
                    )
                },
            )()

    planner = PlanGenerator(llm_client=StubLLM())

    target_template = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel", "publisher"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": ["name", "region"],
            "date_granularity": "weekly",
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {"metric_rules": {"impressions": "sum", "spends": "sum"}},
        "properties": {
            "date": {"type": "string", "format": "date"},
            "channel": {"type": "string"},
            "publisher": {"type": "string"},
            "name": {"type": "string"},
            "region": {"type": "string"},
            "spends": {"type": "number"},
            "impressions": {"type": "integer"},
        },
    }

    context_packet = {
        "approved_mappings": [
            {"source_column": "Campaign Name", "target_column": "name", "decision": "Metadata", "role": "supporting"},
            {"source_column": "Market", "target_column": "region", "decision": "Metadata", "role": "supporting"},
        ],
        "business_rules": [],
    }

    plan = planner.generate(
        structure_analysis={"tables": [], "column_analysis": [], "overall_structure": "flat", "confidence": 0.9},
        examples=[],
        target_template=target_template,
        context_packet=context_packet,
    )

    assert plan.expected_columns[:5] == ["channel", "publisher", "region", "spends", "impressions"]
    assert "date" in plan.expected_columns
    assert "name" in plan.expected_columns
    assert plan.expected_columns.index("name") < plan.expected_columns.index("date")


def test_drop_columns_protects_template_primary_date():
    planner = PlanGenerator(llm_client=None)
    target_template = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel"],
            "metrics": ["spends", "impressions"],
        },
        "properties": {
            "date": {"format": "date"},
            "channel": {},
            "spends": {},
            "impressions": {},
        },
    }
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": ["Name"],
                    "block_start_columns": ["Name"],
                },
            },
            {
                "tool": "transform.drop_columns",
                "params": {"columns": ["Name", "Market", "date"]},
            },
        ],
        reasoning="",
    )
    context_packet = {
        "approved_mappings": [
            {"source_column": "DATE", "target_column": "date", "decision": "primary"},
        ],
    }
    out = planner._adjust_drop_columns_for_block_metrics(
        plan, target_template, context_packet
    )
    drop_params = next(t for t in out.tool_calls if t.get("tool") == "transform.drop_columns")[
        "params"
    ]
    dropped = [str(c).lower() for c in drop_params["columns"]]
    assert "date" not in dropped
    assert "name" in dropped or "market" in dropped


def test_ensure_expand_weighted_spend_allocation():
    planner = PlanGenerator(llm_client=None)
    target_template = {
        "x_scope": {"metrics": ["spends", "impressions"]},
        "properties": {"spends": {}, "impressions": {}},
    }
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": ["Name"],
                    "block_start_columns": ["Name"],
                    "allocations": [{"metric_col": "spends", "method": "equal"}],
                    "auto_detect_block_metrics": False,
                },
            },
        ],
        reasoning="",
    )
    context_packet = {
        "approved_mappings": [
            {
                "source_column": "Performance_Views",
                "target_column": "impressions",
                "decision": "keep",
            },
        ],
    }
    out = planner._ensure_expand_weighted_spend_allocation(
        plan, context_packet, target_template
    )
    expand = out.tool_calls[0]["params"]
    assert expand["allocations"][0]["method"] == "by_weight"
    assert expand["allocations"][0]["weight_col"] == "impressions"
