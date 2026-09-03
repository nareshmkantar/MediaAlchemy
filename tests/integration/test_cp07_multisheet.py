"""
CP-07 multisheet E2E regression (Phase 10.1).

Simulates Digital_UK + Radio_DE: bleed fails evals and critical gate;
align + stamp fix restores isolation and clears union false duplicates.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from sia.evals.collation_sanity import evaluate_union_false_duplicates
from sia.evals.display import build_eval_display
from sia.evals.runner import PipelineEvalRunner
from sia.integrity.context_isolation import (
    align_output_metrics_to_clean_template,
    apply_local_context_dimensions,
    assess_source_context_isolation,
)

from tests.fixtures.cp07_frames import (
    cp07_multisheet_job,
    digital_uk_frame,
    radio_de_bleed_frame,
    radio_de_context_packet,
    radio_de_correct_frame,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]


def _record_cp07_pipeline_evals(job: dict, radio_frame: pd.DataFrame) -> None:
    radio_sid = "cp07-multisheet:file_001:Radio_DE"
    digital_sid = "cp07-multisheet:file_001:Digital_UK"
    cp = radio_de_context_packet(source_id=radio_sid)
    PipelineEvalRunner.record_context_isolation(
        job,
        radio_sid,
        frame=radio_frame,
        context_packet=cp,
        precomputed=assess_source_context_isolation(radio_frame, cp, job=job, source_id=radio_sid),
    )
    PipelineEvalRunner.record_collation(
        job,
        frames_by_source={
            digital_sid: digital_uk_frame(),
            radio_sid: radio_frame,
        },
        join_keys=["date", "publisher", "spends", "impressions"],
    )


def test_cp07_bleed_fails_critical_gate_and_eval_display():
    job = cp07_multisheet_job()
    _record_cp07_pipeline_evals(job, radio_de_bleed_frame())

    assert PipelineEvalRunner.critical_gate_blocks(job) is True
    display = build_eval_display(job)
    assert display["verdict"] == "blocked"
    assert display["export_safe"] is False
    assert any(
        v.get("type") == "dimension_mismatch"
        for v in display["issues"]
    )


def test_cp07_union_false_duplicate_on_bleed():
    frames = {
        "Digital_UK": digital_uk_frame(),
        "Radio_DE": radio_de_bleed_frame(),
    }
    result = evaluate_union_false_duplicates(
        frames,
        join_keys=["date", "publisher", "spends", "impressions"],
    )
    assert result["metrics"]["union_false_duplicates"] >= 1
    assert result["pass"] is False


def test_cp07_align_stamp_fix_passes_isolation_and_gate():
    job = cp07_multisheet_job()
    radio_sid = "cp07-multisheet:file_001:Radio_DE"
    cp = radio_de_context_packet(source_id=radio_sid)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "radio_clean.xlsx"
        pd.DataFrame(
            {
                "date": ["2025-03-01"],
                "publisher": ["SiteC"],
                "spends": [230.0],
                "impressions": [2270.0],
            }
        ).to_excel(path, index=False)
        job["materialized_clean_templates"] = {
            radio_sid: {"path": str(path)},
        }
        bleed = radio_de_bleed_frame()
        aligned = align_output_metrics_to_clean_template(bleed, job, radio_sid)
        fixed = apply_local_context_dimensions(aligned, cp, job=job, source_id=radio_sid)

    report = assess_source_context_isolation(fixed, cp, job=job, source_id=radio_sid)
    assert report["pass"] is True
    assert fixed["market"].iloc[0] == "DE"
    assert fixed["channel"].iloc[0] == "radio"
    assert float(fixed["spends"].iloc[0]) == 230.0

    _record_cp07_pipeline_evals(job, fixed)
    assert PipelineEvalRunner.critical_gate_blocks(job) is False

    union = evaluate_union_false_duplicates(
        {
            "cp07-multisheet:file_001:Digital_UK": digital_uk_frame(),
            radio_sid: fixed,
        },
        join_keys=["date", "publisher", "spends", "impressions"],
    )
    assert union["metrics"]["union_false_duplicates"] == 0
    assert union["pass"] is True

    display = build_eval_display(job)
    assert display["verdict"] in ("pass", "advisory")
    assert display["export_safe"] is True


def test_cp07_correct_frames_no_bleed_signals():
    frames = {
        "Digital_UK": digital_uk_frame(),
        "Radio_DE": radio_de_correct_frame(),
    }
    union = evaluate_union_false_duplicates(
        frames,
        join_keys=["date", "publisher", "spends", "impressions"],
    )
    assert union["pass"] is True
    cp = radio_de_context_packet()
    report = assess_source_context_isolation(radio_de_correct_frame(), cp)
    assert report["pass"] is True
