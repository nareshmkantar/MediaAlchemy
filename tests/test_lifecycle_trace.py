"""Tests for lifecycle events on ProcessingTrace used by the Debug timeline."""
from sia.models.confidence import ProcessingTrace


def test_add_lifecycle_event_appends_structured_entry():
    trace = ProcessingTrace(trace_id="t-1")
    trace.add_lifecycle_event("setup", status="info", message="Starting job")
    trace.add_lifecycle_event(
        "plan_review",
        status="pending",
        message="Plan ready for user review",
        metadata={"checkpoint_id": "cp-123"},
    )
    trace.add_lifecycle_event("output_ready", status="ok", metadata={"output_file": "/tmp/out.xlsx"})

    assert len(trace.lifecycle_events) == 3
    phases = [ev["phase"] for ev in trace.lifecycle_events]
    assert phases == ["setup", "plan_review", "output_ready"]

    pending = trace.lifecycle_events[1]
    assert pending["status"] == "pending"
    assert pending["message"] == "Plan ready for user review"
    assert pending["metadata"]["checkpoint_id"] == "cp-123"
    assert "timestamp" in pending

    ready = trace.lifecycle_events[2]
    assert ready["metadata"]["output_file"].endswith(".xlsx")


def test_lifecycle_events_serialized_in_to_dict():
    trace = ProcessingTrace(trace_id="t-2")
    trace.add_lifecycle_event("plan", status="ok", message="Plan generated")

    payload = trace.to_dict()
    assert "lifecycle_events" in payload
    assert len(payload["lifecycle_events"]) == 1
    assert payload["lifecycle_events"][0]["phase"] == "plan"
