"""Post-execution relationship review must not re-run every source pipeline."""

import pandas as pd

from web_server import _relationship_review_resume_plan


def test_resume_plan_reuses_cached_frames_instead_of_full_reprocess():
    job = {
        "status": "completed",
        "_multi_source_frames_by_source": {
            "amazon": pd.DataFrame({"date": ["2024-01-01"]}),
            "cm360": pd.DataFrame({"date": ["2024-01-02"]}),
        },
    }
    checkpoint = {"trigger_data": {"post_execution": True}}
    assert _relationship_review_resume_plan(job, checkpoint) == "collation_only"
    assert set(job["_pending_collation_frames"]) == {"amazon", "cm360"}


def test_resume_plan_already_done_when_completed_without_frames():
    job = {"status": "completed"}
    checkpoint = {"trigger_data": {"post_execution": True}}
    assert _relationship_review_resume_plan(job, checkpoint) == "already_done"


def test_resume_plan_missing_frames_for_post_exec_in_progress():
    job = {"status": "awaiting_review", "_post_execution_relationship_review": True}
    checkpoint = {"trigger_data": {"post_execution": True}}
    assert _relationship_review_resume_plan(job, checkpoint) == "missing_frames"


def test_resume_plan_pre_exec_still_full_reprocess():
    job = {"status": "awaiting_review"}
    checkpoint = {"trigger_data": {}}
    assert _relationship_review_resume_plan(job, checkpoint) == "full_reprocess"
