"""Phase 3.5: execute_tools blocks wrong-source plans."""

from __future__ import annotations

from sia.agent.base import ExtractionPlan
from sia.agent.nodes import execute_tools_node


class _DummyHITL:
    def create_checkpoint(self, *args, **kwargs):
        class CP:
            def to_dict(self):
                return {"type": "plan_review"}

        return CP()


def test_execute_tools_blocks_plan_with_wrong_source_id():
    state = {
        "grid": None,
        "current_df": None,
        "suggested_tools": [{"tool": "transform.rename", "params": {"mapping": {}}}],
        "scoped_source": {},
        "hitl_checkpoints": [],
        "hitl_manager": _DummyHITL(),
        "source_id": "radio-de",
        "context_packet": {
            "lineage": {"source_id": "radio-de", "sheet_name": "Radio_DE"},
            "interpreted_context": {"fields": {"market": "DE"}},
        },
        "extraction_plan": ExtractionPlan(
            tool_calls=[{"tool": "transform.rename", "params": {"mapping": {}}}],
            source_id="digital-uk",
        ),
    }
    out = execute_tools_node(state)
    assert out.get("hitl_pending_approval") is True
    assert "radio-de" in out.get("review_reason", "")
    assert "digital-uk" in out.get("review_reason", "")
