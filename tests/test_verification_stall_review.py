from sia.agent.hitl import HITLManager
from sia.agent.job_manager import JobManager


def test_empty_stall_actions_are_filled_on_queue():
    jm = JobManager()
    job_id = "job_stall_actions"
    jm.create_job(job_id, "sample.csv", "/tmp/sample.csv")
    cp_id = jm.add_checkpoint_to_review(
        job_id,
        {
            "checkpoint_id": "cp_stall_empty",
            "checkpoint_type": "verification_stall",
            "title": "Replanner Escalation",
            "available_actions": [],
            "pending_state": {"sheet_name": "Sheet1"},
        },
    )
    stored = jm.pending_checkpoints[cp_id]
    assert stored["available_actions"] == HITLManager.STALL_REVIEW_ACTIONS
    queued = [item for item in jm.get_all_pending_reviews() if item.get("checkpoint_id") == cp_id]
    assert queued
    assert queued[0]["available_actions"] == ["accept_as_is", "retry", "cancel"]


def test_legacy_queued_stall_gets_actions_on_read():
    jm = JobManager()
    job_id = "job_stall_legacy"
    jm.create_job(job_id, "sample.csv", "/tmp/sample.csv")
    jm.pending_checkpoints["cp_stall_legacy"] = {
        "checkpoint_id": "cp_stall_legacy",
        "job_id": job_id,
        "filename": "sample.csv",
        "type": "verification_stall",
        "title": "Replanner Escalation",
        "available_actions": [],
        "status": "pending",
        "created_at": "2026-08-14T12:20:00",
    }
    queued = jm.get_all_pending_reviews()
    stall = next(item for item in queued if item["checkpoint_id"] == "cp_stall_legacy")
    assert stall["available_actions"] == ["accept_as_is", "retry", "cancel"]
    assert jm.pending_checkpoints["cp_stall_legacy"]["available_actions"] == [
        "accept_as_is",
        "retry",
        "cancel",
    ]
