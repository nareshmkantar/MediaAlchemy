"""Tests for pipeline checkpoint export API."""

from __future__ import annotations

import json

from sia.debug.checkpoint_export import build_checkpoint_export, list_checkpoint_specs


def _sample_job() -> dict:
    source_id = "demo:file_001:Radio_DE"
    return {
        "id": "demo_job",
        "filename": "CP-07.xlsx",
        "status": "completed",
        "source_registry": [
            {"source_id": source_id, "sheet_name": "Radio_DE", "file_name": "CP-07.xlsx"},
        ],
        "mapping_registry": [
            {
                "source_id": source_id,
                "source_column": "Spend",
                "target_column": "spends",
                "decision": "keep",
            }
        ],
        "layout_registry": {source_id: {"main_blocks": [{"block_id": "b1"}]}},
        "llm_traces": [
            {
                "trace_id": "t1",
                "component": "structure_analyzer",
                "timestamp": "2026-01-01T10:00:00",
                "source_id": source_id,
                "system_prompt": "STRUCTURE_SYS",
                "user_prompt": "grid summary…",
                "raw_response": '{"tables":[]}',
                "input_context": {"grid_summary_length": 1200},
            },
            {
                "trace_id": "t2",
                "component": "plan_generator",
                "timestamp": "2026-01-01T10:02:00",
                "source_id": source_id,
                "system_prompt": "PLAN_SYS",
                "user_prompt": "mappings…",
                "raw_response": '{"tool_calls":[]}',
                "input_context": {"context_briefing": {"included_sections": ["mappings"]}},
            },
            {
                "trace_id": "t3",
                "component": "output_verifier",
                "timestamp": "2026-01-01T10:05:00",
                "source_id": source_id,
                "system_prompt": "VERIFY_SYS",
                "user_prompt": "verify df",
                "raw_response": '{"is_flat": true}',
            },
        ],
        "state_snapshots": [
            {
                "snapshot_id": "s1",
                "label": "job.multi_source.source_start",
                "phase": "before_process_file",
                "source_id": source_id,
                "sheet_name": "Radio_DE",
                "timestamp": "2026-01-01T09:59:00",
                "context": {"setup_counts": {"approved_mappings": 1}},
                "agent_state": {"source_id": source_id},
            },
            {
                "snapshot_id": "s2",
                "label": "job.multi_source.source_done",
                "phase": "after_process_file",
                "source_id": source_id,
                "timestamp": "2026-01-01T10:06:00",
                "agent_state": {"is_flat": True},
            },
        ],
        "debug_events": [
            {
                "event_id": "e1",
                "label": "Context packet built",
                "phase": "context",
                "source_id": source_id,
            }
        ],
    }


def test_list_checkpoint_specs_ordered():
    specs = list_checkpoint_specs()
    assert len(specs) >= 10
    orders = [s["order"] for s in specs]
    assert orders == sorted(orders)


def test_build_checkpoint_export_matches_traces_and_snapshots():
    job = _sample_job()
    source_id = job["source_registry"][0]["source_id"]
    out = build_checkpoint_export(job, source_id=source_id)

    assert out["job_id"] == "demo_job"
    assert out["llm_trace_count"] == 3
    by_id = {c["stage_id"]: c for c in out["checkpoints"]}

    sa = by_id["after_structure_analyzer"]
    assert sa["llm_trace"]["component"] == "structure_analyzer"
    assert sa["llm_trace"]["system_prompt"] == "STRUCTURE_SYS"

    cp_ready = by_id["context_packet_ready"]
    assert cp_ready["state_snapshot"]["phase"] == "before_process_file"
    assert cp_ready["debug_event"]["label"] == "Context packet built"

    plan = by_id["after_plan_generator"]
    assert plan["llm_trace"]["component"] == "plan_generator"
    assert plan["llm_trace"]["input_context"]["context_briefing"]["included_sections"] == ["mappings"]

    verify = by_id["after_execute_verify"]
    assert verify["llm_trace"]["component"] == "output_verifier"

    fin = by_id["after_finalize"]
    assert fin["state_snapshot"]["phase"] == "after_process_file"


def test_build_checkpoint_export_single_stage_filter():
    job = _sample_job()
    out = build_checkpoint_export(
        job,
        source_id=job["source_registry"][0]["source_id"],
        stage_id="after_plan_generator",
    )
    assert len(out["checkpoints"]) == 1
    assert out["checkpoints"][0]["stage_id"] == "after_plan_generator"


def test_build_checkpoint_export_unknown_stage():
    job = _sample_job()
    out = build_checkpoint_export(job, stage_id="not_a_stage")
    assert "error" in out
    assert "available_stages" in out


def test_checkpoint_api_route(flask_app_job):
    """Flask route returns checkpoint bundle for an in-memory job."""
    client = flask_app_job["client"]
    job_id = flask_app_job["job_id"]
    source_id = flask_app_job["source_id"]
    res = client.get(f"/api/debug/{job_id}/checkpoints?source_id={source_id}")
    assert res.status_code == 200
    data = res.get_json()
    assert data["job_id"] == job_id
    assert len(data.get("checkpoints") or []) >= 10


def test_checkpoint_stages_api_route(flask_app_job):
    client = flask_app_job["client"]
    res = client.get("/api/tests/checkpoints/stages")
    assert res.status_code == 200
    data = res.get_json()
    assert data.get("success") is True
    assert len(data.get("stages") or []) >= 10


def test_context_diff_tail_filters_to_selected_source_on_job_stages():
    """FR market rows from TV_FR must not appear under Digital_UK demarcation view."""
    uk = "job:file_001:Digital_UK"
    fr = "job:file_001:TV_FR"
    job = {
        "id": "multi_diff",
        "source_registry": [
            {"source_id": uk, "sheet_name": "Digital_UK"},
            {"source_id": fr, "sheet_name": "TV_FR"},
        ],
        "layout_registry": [],
        "context_diff_log": [
            {
                "source_id": fr,
                "field": "market",
                "after": "France",
                "stage": "packet_build",
                "reason": "scoped field built",
            },
            {
                "source_id": uk,
                "field": "market",
                "after": "UK",
                "stage": "packet_build",
                "reason": "scoped field built",
            },
            {
                "source_id": "job:file_001:Print_US",
                "field": "market",
                "after": "US",
                "stage": "plan_generate",
                "reason": "plan bound to active source",
            },
        ],
    }
    out = build_checkpoint_export(job, source_id=uk, stage_id="guided_demarcation")
    assert len(out["checkpoints"]) == 1
    tail = out["checkpoints"][0].get("context_diff_tail") or []
    assert all(
        (not r.get("source_id")) or r.get("source_id") == uk for r in tail
    )
    assert not any(str(r.get("after")) == "France" for r in tail)
    assert any(str(r.get("after")) == "UK" for r in tail)