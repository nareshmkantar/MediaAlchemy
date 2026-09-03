"""Execution deferral / tool-call accuracy evals."""

from __future__ import annotations

from sia.evals.execution_accuracy import evaluate_execution_deferral
from sia.evals.runner import PipelineEvalRunner


def test_deferral_ok_when_aggregate_weekly_deferred_not_executed():
    planned = [{"tool": "transform.rename_columns"}, {"tool": "transform.aggregate_weekly"}]
    executed = [{"tool": "transform.rename_columns"}]
    deferred = [{"tool": "transform.aggregate_weekly"}]
    out = evaluate_execution_deferral(
        defer_grain=True,
        planned_tools=planned,
        executed_tools=executed,
        deferred_tools=deferred,
    )
    assert out["pass"] is True
    assert out["metrics"]["deferral_ok"] is True
    assert out["metrics"]["grain_tools_deferred"] == ["transform.aggregate_weekly"]
    assert not out["violations"]


def test_critical_when_aggregate_weekly_runs_pre_collation():
    planned = [{"tool": "transform.aggregate_weekly"}]
    executed = [{"tool": "transform.aggregate_weekly"}]
    out = evaluate_execution_deferral(
        defer_grain=True,
        planned_tools=planned,
        executed_tools=executed,
        deferred_tools=[],
    )
    assert out["pass"] is False
    assert any(v.get("type") == "grain_tool_executed_pre_collation" for v in out["violations"])


def test_runner_records_execution_deferral_on_job():
    job = {"job_id": "exec-defer-test"}
    PipelineEvalRunner.record_execution_deferral(
        job,
        "src-a",
        defer_grain=True,
        planned_tools=[{"tool": "transform.aggregate_weekly"}],
        executed_tools=[],
        deferred_tools=[{"tool": "transform.aggregate_weekly"}],
    )
    bucket = job["pipeline_evals"]["per_source"]["src-a"]["execution"]
    assert bucket["metrics"]["grain_tools_deferred"] == ["transform.aggregate_weekly"]
    assert bucket["pass"] is True
    assert job["tool_invocations_by_source"]["src-a"]["planned"]
