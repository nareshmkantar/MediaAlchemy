"""Phase B/C: durable job metadata + HITL checkpoint persistence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sia.agent.job_manager import JobManager
from sia.agent.job_persistence import _sanitize_pending_state
from sia.agent.base import ExtractionPlan


@pytest.fixture
def persist_env(tmp_path, monkeypatch):
    db_path = tmp_path / "metadata.db"
    monkeypatch.setenv("SCHEMA_AGENT_METADATA_DB", str(db_path))
    monkeypatch.delenv("SCHEMA_AGENT_PERSIST_JOBS", raising=False)
    yield db_path


def test_job_metadata_survives_restart(persist_env):
    m1 = JobManager()
    m1.create_job("job_a", "demo.xlsx", str(persist_env.parent / "demo.xlsx"), sheets=["Sheet1"])
    m1.save_layout_registry(
        "job_a",
        [{"layout_id": "b1", "source_id": m1.get_job("job_a")["source_registry"][0]["source_id"], "decision": "Keep"}],
    )
    m1.patch_ux_state("job_a", {"current_ux_stage": 2, "relationships_gate_complete": True})
    m1.update_job_status("job_a", "processing", "Running", "Process")

    m2 = JobManager()
    job = m2.get_job("job_a")
    assert job is not None
    assert job["status"] == "processing"
    assert int(job.get("current_ux_stage") or 0) == 2
    assert len(job.get("layout_registry") or []) == 1
    assert job.get("relationships_gate_complete") is True


def test_hitl_checkpoint_survives_restart(persist_env):
    m1 = JobManager()
    m1.create_job("job_b", "multi.xlsx", str(persist_env.parent / "multi.xlsx"), sheets=["UK", "DE"])
    cp_id = m1.add_checkpoint_to_review(
        "job_b",
        {
            "checkpoint_id": "cp_rel_1",
            "checkpoint_type": "file_relationship_review",
            "title": "Combine outputs",
            "trigger_reason": "Choose union/join/independent",
            "pending_state": {
                "workflow": "multi_source",
                "source_ids": ["s1", "s2"],
                "process_all_sources": True,
            },
        },
    )
    assert cp_id == "cp_rel_1"
    m1.update_job_status("job_b", "awaiting_review", "Relationship review", "Relationship Review")

    m2 = JobManager()
    assert m2.get_job("job_b") is not None
    assert m2.get_job("job_b")["status"] == "awaiting_review"
    cp = m2.pending_checkpoints.get("cp_rel_1")
    assert cp is not None
    assert cp["type"] == "file_relationship_review"
    ps = cp.get("pending_state") or {}
    assert ps.get("workflow") == "multi_source"
    assert m2.get_pending_review_count() >= 1


def test_remove_job_deletes_from_store(persist_env):
    m1 = JobManager()
    m1.create_job("job_c", "x.xlsx", str(persist_env.parent / "x.xlsx"))
    m1.add_checkpoint_to_review(
        "job_c",
        {"checkpoint_id": "cp_x", "checkpoint_type": "plan_review", "pending_state": {"sheet_name": "S1"}},
    )
    m1.remove_job("job_c")

    m2 = JobManager()
    assert m2.get_job("job_c") is None
    assert "cp_x" not in m2.pending_checkpoints


def test_persistence_disabled_without_env(monkeypatch):
    monkeypatch.delenv("SCHEMA_AGENT_METADATA_DB", raising=False)
    monkeypatch.delenv("SCHEMA_AGENT_PERSIST_JOBS", raising=False)
    m = JobManager()
    assert m._persist_jobs is False


def test_sanitize_pending_state_skips_llm_client_without_recursion():
    class CircularClient:
        def __init__(self):
            self.ref = self

    plan = ExtractionPlan(tool_calls=[{"tool": "verify.schema"}], confidence=0.9, reasoning="ok")
    state = {
        "sheet_name": "Digital_UK",
        "iteration": 2,
        "llm_client": CircularClient(),
        "hitl_manager": object(),
        "current_df": object(),
        "extraction_plan": plan,
        "structure_analysis": {"tables": [{"label": "main"}]},
    }
    sanitized = _sanitize_pending_state(state)
    assert sanitized["sheet_name"] == "Digital_UK"
    assert sanitized["iteration"] == 2
    assert sanitized["extraction_plan"]["confidence"] == 0.9
    assert "llm_client" not in sanitized
    assert "hitl_manager" not in sanitized
    assert "current_df" not in sanitized


def test_add_checkpoint_with_agent_like_pending_state(monkeypatch):
    monkeypatch.delenv("SCHEMA_AGENT_METADATA_DB", raising=False)
    m = JobManager()

    class CircularClient:
        def __init__(self):
            self.ref = self

    cp_id = m.add_checkpoint_to_review(
        "job_hitl",
        {
            "checkpoint_id": "cp_plan",
            "checkpoint_type": "plan_review",
            "title": "Review plan",
            "pending_state": {
                "sheet_name": "Digital_UK",
                "llm_client": CircularClient(),
                "extraction_plan": ExtractionPlan(tool_calls=[{"tool": "extract.table"}]),
            },
        },
    )
    assert cp_id == "cp_plan"
    cp = m.pending_checkpoints["cp_plan"]
    assert cp["pending_state"]["sheet_name"] == "Digital_UK"
    assert "llm_client" not in cp["pending_state"]
