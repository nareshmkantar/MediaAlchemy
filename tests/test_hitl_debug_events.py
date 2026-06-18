"""Tests for HITL synthetic debug events (Agent Trace)."""

from datetime import datetime, timezone

from sia.debug.hitl_debug_events import (
    record_hitl_pause_debug,
    record_hitl_resume_debug,
    _find_parent_event_id,
)


def test_find_parent_event_id_picks_latest_node():
    job = {
        "debug_events": [
            {
                "event_id": "n1",
                "parent_event_id": None,
                "process_type": "node",
                "module": "generate_plan",
                "timestamp": "2026-01-01T10:00:00+00:00",
            },
            {
                "event_id": "n2",
                "parent_event_id": "n1",
                "process_type": "node",
                "module": "generate_plan",
                "timestamp": "2026-01-01T10:05:00+00:00",
            },
        ]
    }
    assert _find_parent_event_id(job, "generate_plan") == "n2"


def test_record_hitl_pause_dedupes_and_links_parent():
    job = {
        "debug_events": [
            {
                "event_id": "gen-1",
                "parent_event_id": None,
                "process_type": "node",
                "module": "generate_plan",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
        "state_snapshots": [],
    }

    checkpoints = [
        {
            "checkpoint_id": "cp-abc",
            "checkpoint_type": "plan_review",
            "title": "Plan review",
            "severity": "medium",
        }
    ]

    class FakeTrace:
        hitl_pause_type = "plan_review"
        review_reason = "Plan needs review"
        hitl_checkpoints = checkpoints
        deletion_previews = []
        low_confidence_items = []
        pending_state = {
            "hitl_pause_type": "plan_review",
            "hitl_checkpoints": checkpoints,
            "source_id": "src-1",
        }

    eid1 = record_hitl_pause_debug(job, FakeTrace(), source_id="src-1")
    eid2 = record_hitl_pause_debug(job, FakeTrace(), source_id="src-1")
    assert eid1 == eid2 == "hitl-pause-cp-abc"
    assert len(job["debug_events"]) == 2

    pause_ev = job["debug_events"][-1]
    assert pause_ev["process_type"] == "hitl_pause"
    assert pause_ev["parent_event_id"] == "gen-1"
    assert pause_ev["status"] == "pending"

    snaps = [s for s in job["state_snapshots"] if s.get("anchor_event_id") == "hitl-pause-cp-abc"]
    assert len(snaps) == 1
    assert snaps[0]["phase"] == "hitl_pause"
    assert snaps[0]["label"] == "state.hitl.plan_review"


def test_record_hitl_resume_child_of_pause():
    job = {
        "debug_events": [
            {
                "event_id": "hitl-pause-cp-xyz",
                "parent_event_id": "gen-1",
                "process_type": "hitl_pause",
                "module": "plan_review",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
        "state_snapshots": [],
    }

    record_hitl_resume_debug(
        job,
        checkpoint_id="cp-xyz",
        pause_type="plan_review",
        action="approve",
        pending_state={"source_id": "src-2"},
    )

    resume_events = [e for e in job["debug_events"] if e.get("process_type") == "hitl_resume"]
    assert len(resume_events) == 1
    assert resume_events[0]["parent_event_id"] == "hitl-pause-cp-xyz"
    assert resume_events[0]["event_id"] == "hitl-resume-cp-xyz-approve"
