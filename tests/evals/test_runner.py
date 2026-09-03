"""Unit tests for PipelineEvalRunner rollup."""

from __future__ import annotations

from sia.evals.runner import PipelineEvalRunner


def test_override_clears_critical_gate_block():
    job = {}
    PipelineEvalRunner.record_execution_event(
        job,
        "src_a",
        {"subtype": "zero_rows", "message": "Output empty after filter"},
    )
    assert PipelineEvalRunner.critical_gate_blocks(job) is True
    PipelineEvalRunner.record_override(job, reason="Analyst accepted empty slice for QA")
    assert PipelineEvalRunner.critical_gate_blocks(job) is False
    gate = job["pipeline_evals"]["critical_gate"]
    assert gate.get("overridden") is True
