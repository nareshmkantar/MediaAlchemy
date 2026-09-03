"""HITL pause should queue only unresolved checkpoints for Review."""
from types import SimpleNamespace

from sia.agent.job_manager import JobManager


def test_integrity_hitl_state_excludes_resolved_prior_checkpoints():
    from sia.integrity.metric_reconcile import integrity_violation_to_hitl_state

    violation = {
        "type": "integrity_violation",
        "subtype": "mass_row_loss",
        "tool": "transform.filter_summaries",
        "details": "Too many rows removed",
        "confidence": 0.2,
    }
    resolved_plan = {
        "checkpoint_id": "cp_plan",
        "checkpoint_type": "plan_review",
        "resolved": True,
    }
    payload = integrity_violation_to_hitl_state(
        violation,
        None,
        {
            "iteration": 1,
            "hitl_checkpoints": [resolved_plan],
        },
        tools_history_slice=[],
        deferred_post_collate=[],
        warnings=[],
        low_confidence_items=[],
        tool_index=2,
    )
    cps = payload["hitl_checkpoints"]
    assert len(cps) == 1
    assert cps[0]["checkpoint_type"] == "checksum_failure"
    assert payload["integrity_pause_tool"] == "transform.filter_summaries"
    assert payload["integrity_resume_after_tool_index"] == 2


def test_add_checkpoint_skips_resolved_plan_when_integrity_arrives():
    jm = JobManager()
    job_id = "job_test_integrity_queue"
    jm.create_job(job_id, "sample.xlsx", "/tmp/sample.xlsx")
    resolved_plan = {
        "checkpoint_id": "cp_plan_old",
        "checkpoint_type": "plan_review",
        "title": "Plan",
        "resolved": True,
    }
    integrity_cp = {
        "checkpoint_id": "cp_integrity_1",
        "checkpoint_type": "checksum_failure",
        "title": "Integrity",
        "resolved": False,
        "trigger_reason": "mass row loss",
    }
    trace = SimpleNamespace(
        hitl_checkpoints=[resolved_plan, integrity_cp],
        pending_state={"iteration": 1},
        review_reason="Integrity pause",
        low_confidence_items=[],
        deletion_previews=[],
    )
    trace.hitl_checkpoints = [resolved_plan, integrity_cp]

    unresolved = [
        cp for cp in trace.hitl_checkpoints if isinstance(cp, dict) and not cp.get("resolved", False)
    ]
    assert len(unresolved) == 1
    cp_id = jm.add_checkpoint_to_review(job_id, {**unresolved[0], "pending_state": {}})
    pending = jm.get_all_pending_reviews()
    assert len(pending) == 1
    assert pending[0]["type"] == "checksum_failure"
    assert pending[0]["checkpoint_id"] == cp_id


def test_add_checkpoint_replaces_stale_integrity_items_for_same_job():
    jm = JobManager()
    job_id = "job_test_integrity_dedupe"
    jm.create_job(job_id, "sample.xlsx", "/tmp/sample.xlsx")
    first = {
        "checkpoint_id": "cp_integrity_old",
        "checkpoint_type": "checksum_failure",
        "title": "Old",
        "resolved": False,
        "trigger_reason": "old",
        "created_at": "2026-01-01T10:00:00",
    }
    second = {
        "checkpoint_id": "cp_integrity_new",
        "checkpoint_type": "checksum_failure",
        "title": "New",
        "resolved": False,
        "trigger_reason": "new",
        "created_at": "2026-01-01T11:00:00",
    }
    jm.add_checkpoint_to_review(job_id, {**first, "pending_state": {}})
    jm.add_checkpoint_to_review(job_id, {**second, "pending_state": {}})
    pending = jm.get_all_pending_reviews()
    assert len(pending) == 1
    assert pending[0]["checkpoint_id"] == "cp_integrity_new"
