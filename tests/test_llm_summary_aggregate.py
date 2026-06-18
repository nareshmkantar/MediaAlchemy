"""Run Summary must aggregate every persisted LLM trace, not only the last observer batch."""

import pytest

from sia.debug.llm_observer import refresh_job_llm_summary, summarize_llm_traces


def test_summarize_llm_traces_sums_all_calls():
    traces = [
        {
            "trace_id": "a",
            "latency_ms": 1000,
            "input_tokens": 100,
            "output_tokens": 10,
            "confidence_score": 0.8,
            "model_id": "model-a",
            "success": True,
        },
        {
            "trace_id": "b",
            "latency_ms": 2500,
            "input_tokens": 200,
            "output_tokens": 20,
            "confidence_score": 0.9,
            "model_id": "model-a",
            "success": True,
        },
    ]
    summary = summarize_llm_traces(traces)
    assert summary["total_calls"] == 2
    assert summary["total_latency_ms"] == 3500
    assert summary["total_input_tokens"] == 300
    assert summary["total_output_tokens"] == 30
    assert summary["avg_confidence"] == pytest.approx(0.85)
    assert summary["model_id"] == "model-a"


def test_refresh_job_llm_summary_replaces_last_batch_only_summary():
    job = {
        "llm_traces": [
            {"trace_id": "1", "latency_ms": 100, "input_tokens": 10, "output_tokens": 1, "confidence_score": 0.5},
            {"trace_id": "2", "latency_ms": 200, "input_tokens": 20, "output_tokens": 2, "confidence_score": 0.7},
        ],
        "llm_summary": {
            "total_calls": 1,
            "total_latency_ms": 200,
            "total_input_tokens": 20,
            "total_output_tokens": 2,
        },
    }
    summary = refresh_job_llm_summary(job)
    assert summary["total_calls"] == 2
    assert summary["total_latency_ms"] == 300
    assert job["llm_summary"]["total_calls"] == 2
