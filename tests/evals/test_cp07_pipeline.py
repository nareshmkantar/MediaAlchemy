"""CP-07 multisheet regression: context bleed + union false duplicates."""

from __future__ import annotations

import pandas as pd

from sia.evals.collation_sanity import evaluate_union_false_duplicates
from sia.evals.runner import PipelineEvalRunner
from sia.integrity.context_isolation import assess_source_context_isolation

from tests.fixtures.cp07_frames import (
    digital_uk_frame,
    radio_de_bleed_frame,
    radio_de_correct_frame,
    radio_de_context_packet,
)


def test_context_bleed_fails_isolation_for_radio_de():
    cp = radio_de_context_packet()
    report = assess_source_context_isolation(radio_de_bleed_frame(), cp)
    assert report["pass"] is False
    assert any(v.get("type") == "dimension_mismatch" for v in report["violations"])


def test_critical_gate_blocks_on_context_isolation():
    job = {"job_id": "cp07-test"}
    PipelineEvalRunner.record_context_isolation(
        job,
        "Radio_DE",
        frame=radio_de_bleed_frame(),
        context_packet=radio_de_context_packet(),
        precomputed=assess_source_context_isolation(
            radio_de_bleed_frame(),
            radio_de_context_packet(),
        ),
    )
    assert PipelineEvalRunner.critical_gate_blocks(job) is True
    reasons = PipelineEvalRunner.blocked_reasons(job)
    assert any("dimension_mismatch" in r or "Radio_DE" in r for r in reasons)


def test_union_false_duplicates_when_bleed_copies_digital_metrics_to_radio():
    """Bleed makes Radio_DE body match Digital_UK → false duplicate on union keys."""
    frames = {
        "Digital_UK": digital_uk_frame(),
        "Radio_DE": radio_de_bleed_frame(),
    }
    result = evaluate_union_false_duplicates(frames, join_keys=["date", "publisher", "spends", "impressions"])
    assert result["metrics"]["union_false_duplicates"] >= 1
    assert result["pass"] is False


def test_union_no_false_duplicate_when_sources_are_correct():
    frames = {
        "Digital_UK": digital_uk_frame(),
        "Radio_DE": radio_de_correct_frame(),
    }
    result = evaluate_union_false_duplicates(frames, join_keys=["date", "publisher", "spends", "impressions"])
    assert result["metrics"]["union_false_duplicates"] == 0
    assert result["pass"] is True


def test_plan_contract_flags_cross_source_add_column_literal():
    from sia.evals.plan_contract import evaluate_plan_contract

    cp = {
        "lineage": {"sheet_name": "Radio_DE", "source_id": "job:file:Radio_DE"},
        "interpreted_context": {"fields": {"channel": "radio", "market": "DE"}},
    }
    tools = [
        {
            "tool": "transform.add_column",
            "params": {"target_column": "channel", "value": "digital", "when": "missing"},
        }
    ]
    out = evaluate_plan_contract(tools, context_packet=cp)
    assert out["pass"] is False
    assert any(v.get("type") == "context_grounding_critical" for v in out["violations"])
