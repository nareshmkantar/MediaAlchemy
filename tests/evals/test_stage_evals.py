"""Tests for stage-scoped evals: argument correctness + plan adherence."""

from __future__ import annotations

from sia.evals.plan_contract import evaluate_argument_correctness
from sia.evals.execution_accuracy import (
    evaluate_execution_deferral,
    evaluate_invocation_adherence,
)


def _structure_with_columns(*labels):
    return {"columns": [{"column_label": lbl} for lbl in labels]}


def test_argument_correctness_flags_unknown_column():
    sa = _structure_with_columns("date", "spend", "market")
    tools = [
        {"tool": "transform.aggregate_weekly", "params": {"group_by": ["date", "region"]}},
    ]
    out = evaluate_argument_correctness(tools, structure_analysis=sa)
    m = out["metrics"]
    assert m["argument_refs_checked"] == 2
    assert m["argument_bad_ref_count"] == 1  # "region" not in columns
    assert m["argument_correctness_score"] == 0.5
    assert any(v["type"] == "argument_correctness" for v in out["violations"])
    assert out["pass"] is True  # advisory only


def test_argument_correctness_passes_when_all_columns_known():
    sa = _structure_with_columns("date", "spend", "market")
    tools = [
        {"tool": "transform.rename", "params": {"mapping": {"spend": "Spend"}}},
        {"tool": "transform.map_values", "params": {"column": "market"}},
    ]
    out = evaluate_argument_correctness(tools, structure_analysis=sa)
    assert out["metrics"]["argument_bad_ref_count"] == 0
    assert out["metrics"]["argument_correctness_score"] == 1.0


def test_argument_correctness_skips_without_universe():
    # No structure analysis / context packet -> no reliable column universe -> skip.
    tools = [{"tool": "transform.aggregate_weekly", "params": {"group_by": ["anything"]}}]
    out = evaluate_argument_correctness(tools)
    assert out["metrics"]["argument_refs_checked"] == 0
    assert "argument_correctness_score" not in out["metrics"]
    assert out["violations"] == []


def test_plan_adherence_detects_unplanned_and_missing():
    planned = [{"tool": "transform.rename"}, {"tool": "verify.schema"}]
    executed = [{"tool": "transform.rename"}, {"tool": "transform.deduplicate"}]
    out = evaluate_execution_deferral(
        defer_grain=False,
        planned_tools=planned,
        executed_tools=executed,
        deferred_tools=[],
    )
    m = out["metrics"]
    assert "transform.deduplicate" in m["unplanned_executed_tools"]
    assert "verify.schema" in m["missing_planned_tools"]
    assert m["plan_adherence_score"] < 1.0
    assert m["invocation_adherence_score"] < 1.0
    assert m["invocation_mismatch_count"] >= 1
    assert any(v["type"] == "plan_adherence_drift" for v in out["violations"])
    assert any(v["type"] == "invocation_adherence_drift" for v in out["violations"])
    # Advisory only — does not fail the stage.
    assert out["pass"] is True


def test_plan_adherence_perfect_when_executed_matches_plan():
    planned = [{"tool": "transform.rename"}, {"tool": "verify.schema"}]
    out = evaluate_execution_deferral(
        defer_grain=False,
        planned_tools=planned,
        executed_tools=list(planned),
        deferred_tools=[],
    )
    m = out["metrics"]
    assert m["plan_adherence_score"] == 1.0
    assert m["invocation_adherence_score"] == 1.0
    assert m["unplanned_executed_tools"] == []
    assert m["missing_planned_tools"] == []


def test_invocation_adherence_penalizes_repeated_tool_count_drift():
    """Set-based plan adherence can be 100% while invocation order/count differs."""
    planned = [
        {"tool": "transform.format", "params": {"column": "date", "format": "date:YYYY-MM-DD"}},
        {"tool": "transform.format", "params": {"column": "date", "format": "date:YYYY-MM-DD"}},
        {"tool": "transform.format", "params": {"column": "date", "format": "date:YYYY-MM-DD"}},
        {"tool": "transform.format", "params": {"column": "date", "format": "date:YYYY-MM-DD"}},
    ]
    executed = [
        {"tool": "transform.format", "params": {"column": "date", "format": "date:YYYY-MM-DD"}},
    ]
    inv = evaluate_invocation_adherence(planned_tools=planned, executed_tools=executed)
    assert inv["invocation_adherence_score"] < 1.0
    assert inv["invocation_missing_count"] == 3

    set_out = evaluate_execution_deferral(
        defer_grain=False,
        planned_tools=planned,
        executed_tools=executed,
    )
    assert set_out["metrics"]["plan_adherence_score_set"] == 1.0
    assert set_out["metrics"]["plan_adherence_score"] < 1.0


def test_plan_adherence_counts_deferred_as_accounted():
    # Grain tool planned then deferred (multi-source) should not count as "missing".
    planned = [{"tool": "transform.rename"}, {"tool": "transform.aggregate_weekly"}]
    executed = [{"tool": "transform.rename"}]
    deferred = [{"tool": "transform.aggregate_weekly"}]
    out = evaluate_execution_deferral(
        defer_grain=True,
        planned_tools=planned,
        executed_tools=executed,
        deferred_tools=deferred,
    )
    m = out["metrics"]
    assert m["missing_planned_tools"] == []
    assert m["plan_adherence_score"] == 1.0
