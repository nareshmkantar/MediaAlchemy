from sia.agent.base import ExtractionPlan
from sia.agent.hitl import HITLManager
from sia.agent.job_manager import JobManager
from sia.agent.nodes import generate_plan_node
from sia.models.confidence import ProcessingTrace


class StubPlanGenerator:
    def __init__(self, _llm_client):
        pass

    def generate(self, *_args, **_kwargs):
        return ExtractionPlan(
            tool_calls=[{"tool": "normalize_headers", "params": {}}],
            business_rule_actions=[{"target_column": "date_paid_media", "action": "format", "value": "yyyy-mm-dd"}],
            approval_items=[
                {
                    "summary": "Confirm the event date source column before execution.",
                    "target_column": "date_paid_media",
                    "owner": "analyst",
                }
            ],
            expected_columns=["date_paid_media"],
            confidence=0.92,
            reasoning="One mapping still needs analyst confirmation.",
        )


def test_generate_plan_node_creates_plan_review_checkpoint_for_approval_items(monkeypatch):
    monkeypatch.setattr("sia.agent.nodes.PlanGenerator", StubPlanGenerator)

    state = {
        "llm_client": object(),
        "structure_analysis": {"tables": [{"name": "main"}]},
        "target_template": {
            "x_scope": {"uid_hierarchy": ["date_paid_media"], "metrics": [], "supporting_columns": []},
            "business_logic": {"column_rules": []},
            "aggregation_logic": {},
            "properties": {"date_paid_media": {"type": "string"}},
        },
        "context_packet": {
            "source_metadata": {"file_name": "example.xlsx", "sheet_name": "Sheet1"},
            "approved_mappings": [],
            "business_rules": [],
            "user_notes": [],
        },
        "confidence_trajectory": [],
        "hitl_checkpoints": [],
        "hitl_manager": HITLManager(),
    }

    result = generate_plan_node(state)

    assert result["requires_review"] is True
    assert len(result["approval_items"]) == 1
    assert len(result["planned_rule_actions"]) == 1
    assert len(result["hitl_checkpoints"]) == 1
    assert result["hitl_pending_approval"] is True
    assert result["hitl_pause_type"] == "plan_review"
    checkpoint = result["hitl_checkpoints"][0]
    assert checkpoint["checkpoint_type"] == "plan_review"
    assert checkpoint["title"] == "Plan Assumptions Review"
    assert checkpoint["trigger_data"]["approval_items"][0]["target_column"] == "date_paid_media"
    assert checkpoint["trigger_data"]["planned_rule_actions"][0]["action"] == "format"
    assert checkpoint["trigger_data"]["tool_calls"][0]["tool"] == "normalize_headers"
    assert checkpoint["trigger_data"]["expected_columns"] == ["date_paid_media"]


def test_processing_trace_serializes_plan_artifacts():
    trace = ProcessingTrace(trace_id="trace-001")
    trace.approval_items = [{"summary": "Review derived metric mapping"}]
    trace.planned_rule_actions = [{"target_column": "clicks", "action": "fill_blank", "value": 0}]

    payload = trace.to_dict()

    assert payload["approval_items"][0]["summary"] == "Review derived metric mapping"
    assert payload["planned_rule_actions"][0]["action"] == "fill_blank"


def test_generate_plan_node_reuses_existing_plan_after_approval():
    existing_plan = ExtractionPlan(
        tool_calls=[{"tool": "normalize_headers", "params": {}}],
        business_rule_actions=[{"target_column": "date_paid_media", "action": "format", "value": "yyyy-mm-dd"}],
        approval_items=[{"summary": "Already reviewed"}],
        expected_columns=["date_paid_media"],
        confidence=0.91,
        reasoning="Approved by analyst.",
    )
    state = {
        "resume_mode": "use_existing_plan",
        "extraction_plan": existing_plan,
    }

    result = generate_plan_node(state)

    assert result["suggested_tools"] == existing_plan.tool_calls
    assert result["planned_rule_actions"] == existing_plan.business_rule_actions
    assert result["approval_items"] == []
    assert result["requires_review"] is False
    assert result["hitl_pending_approval"] is False
    assert result["trace_steps"][0]["reused_existing_plan"] is True


def test_job_manager_applies_planner_feedback_to_notes_and_rules():
    manager = JobManager()
    job = manager.create_job("job-1", "example.xlsx", "uploads/example.xlsx", sheets=["Sheet1"])
    job["scoped_source"] = {"sheet_name": "Sheet1"}
    manager.pending_checkpoints["cp-1"] = {
        "checkpoint_id": "cp-1",
        "job_id": "job-1",
        "type": "plan_review",
        "trigger_data": {
            "approval_items": [{"summary": "Confirm date column"}],
        },
    }

    result = manager.apply_plan_feedback(
        "job-1",
        "cp-1",
        "modify",
        {
            "analyst_notes": "Use booking date instead of invoice date.",
            "updated_rule_actions": [
                {"target_column": "date_paid_media", "action": "format", "value": "yyyy-mm-dd"}
            ],
        },
    )

    assert any("booking date" in note for note in job["user_notes"])
    assert result["rules_saved"][0]["target_column"] == "date_paid_media"


class LowConfidenceApprovalPlanGenerator:
    def __init__(self, _llm_client):
        pass

    def generate(self, *_args, **_kwargs):
        return ExtractionPlan(
            tool_calls=[{"tool": "transform.reorder_columns", "params": {}}],
            approval_items=[{"summary": "Confirm allocation", "target_column": "spends"}],
            expected_columns=["date", "spends"],
            confidence=0.32,
            reasoning="Low confidence plan with approval items.",
        )


def test_generate_plan_node_single_checkpoint_when_low_confidence_and_approval_items(monkeypatch):
    monkeypatch.setattr("sia.agent.nodes.PlanGenerator", LowConfidenceApprovalPlanGenerator)

    state = {
        "llm_client": object(),
        "sheet_name": "Notes_Global",
        "structure_analysis": {"tables": [{"name": "main"}]},
        "target_template": {"properties": {}, "x_scope": {"uid_hierarchy": [], "metrics": [], "supporting_columns": []}},
        "context_packet": {"source_metadata": {"sheet_name": "Notes_Global"}},
        "confidence_trajectory": [0.0],
        "hitl_checkpoints": [],
        "hitl_manager": HITLManager(),
    }

    result = generate_plan_node(state)

    assert len(result["hitl_checkpoints"]) == 1
    checkpoint = result["hitl_checkpoints"][0]
    assert checkpoint["checkpoint_type"] == "plan_review"
    assert checkpoint["confidence"] == 0.32
    assert checkpoint["trigger_data"]["plan_confidence"] == 0.32
    assert checkpoint["trigger_data"]["approval_items"]


def test_add_checkpoint_replaces_stale_plan_review_for_same_sheet():
    manager = JobManager()
    job_id = "job_plan_dedupe"
    manager.create_job(job_id, "sample.xlsx", "/tmp/sample.xlsx", sheets=["Notes_Global"])
    first = {
        "checkpoint_id": "cp_plan_old",
        "checkpoint_type": "plan_review",
        "trigger_reason": "old",
        "trigger_data": {"sheet_name": "Notes_Global", "plan_confidence": 0.32},
        "confidence": 0.32,
        "created_at": "2026-01-01T10:00:00",
    }
    second = {
        "checkpoint_id": "cp_plan_new",
        "checkpoint_type": "plan_review",
        "trigger_reason": "new",
        "trigger_data": {"sheet_name": "Notes_Global", "plan_confidence": 0.32},
        "confidence": 0.32,
        "created_at": "2026-01-01T11:00:00",
    }
    manager.add_checkpoint_to_review(job_id, {**first, "pending_state": {"sheet_name": "Notes_Global"}})
    manager.add_checkpoint_to_review(job_id, {**second, "pending_state": {"sheet_name": "Notes_Global"}})
    pending = [p for p in manager.get_all_pending_reviews() if p.get("type") == "plan_review"]
    assert len(pending) == 1
    assert pending[0]["checkpoint_id"] == "cp_plan_new"
    assert pending[0]["confidence"] == 0.32
