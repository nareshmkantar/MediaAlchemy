"""Planner review decisions flow into mappings and tool plans."""
from sia.agent.base import ExtractionPlan
from sia.agent.planner_decisions import (
    apply_resolved_approvals_to_context,
    apply_resolved_approvals_to_plan,
    infer_metric_target_strategy,
    merge_resolved_approval_items,
)


def test_merge_resolved_approval_items():
    orig = [
        {
            "target_column": "spends",
            "question": "How should spends be built?",
            "options": [
                {"id": "single_column", "label": "Use Budgets_Total only"},
                {"id": "combine_sources", "label": "Sum Fee + Media + Other"},
            ],
        }
    ]
    res = [{"analyst_confirm": "single_column", "resolution_note": "Choice: Use Budgets_Total only"}]
    merged = merge_resolved_approval_items(orig, res)
    assert merged[0]["analyst_confirm"] == "single_column"
    assert merged[0]["target_column"] == "spends"


def test_infer_single_column_spend_strategy():
    item = {
        "target_column": "spends",
        "analyst_confirm": "single_column",
        "options": [{"id": "single_column", "label": "Use Budgets_Total as spends"}],
    }
    assert infer_metric_target_strategy(item) == "single_column"


def test_apply_single_column_demotes_extra_mappings_and_strips_calculate():
    mappings = [
        {"source_column": "Budgets_Total", "target_column": "spends", "decision": "keep"},
        {"source_column": "Fee", "target_column": "spends", "decision": "keep"},
        {"source_column": "Media", "target_column": "spends", "decision": "keep"},
    ]
    resolved = [
        {
            "target_column": "spends",
            "analyst_confirm": "single_column",
            "options": [{"id": "single_column", "label": "Use Budgets_Total only"}],
        }
    ]
    cp = apply_resolved_approvals_to_context({"approved_mappings": mappings}, resolved)
    patched = cp["approved_mappings"]
    spends_rows = [m for m in patched if m.get("target_column") == "spends"]
    assert len(spends_rows) == 1
    assert spends_rows[0]["source_column"] == "Budgets_Total"

    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "transform.rename", "params": {"mapping": {"Budgets_Total": "spends"}}},
            {
                "tool": "transform.calculate",
                "params": {
                    "target_column": "spends",
                    "expression": "spends_1 + spends_2 + spends_3",
                },
            },
        ],
        reasoning="",
    )
    out = apply_resolved_approvals_to_plan(plan, cp)
    names = [t.get("tool") for t in out.tool_calls]
    assert "transform.calculate" not in names


def test_apply_combine_sources_builds_sum_calculate():
    mappings = [
        {"source_column": "Fee", "target_column": "spends", "decision": "keep"},
        {"source_column": "Media", "target_column": "spends", "decision": "keep"},
        {"source_column": "Other", "target_column": "spends", "decision": "keep"},
    ]
    resolved = [
        {
            "target_column": "spends",
            "analyst_confirm": "combine_sources",
            "options": [{"id": "combine_sources", "label": "Sum Fee + Media + Other"}],
        }
    ]
    cp = apply_resolved_approvals_to_context({"approved_mappings": mappings}, resolved)
    assert "duplicate_target_mappings" in cp
    plan = ExtractionPlan(tool_calls=[], reasoning="")
    out = apply_resolved_approvals_to_plan(plan, cp)
    calc = next(t for t in out.tool_calls if t.get("tool") == "transform.calculate")
    assert "Fee" in calc["params"]["expression"]
    assert "Media" in calc["params"]["expression"]
