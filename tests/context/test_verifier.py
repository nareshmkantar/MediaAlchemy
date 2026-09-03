"""Phase 5: ContextVerifier deterministic gates."""

from __future__ import annotations

import pandas as pd

from sia.agent.base import ExtractionPlan
from sia.agent.nodes import execute_tools_node
from sia.context.verifier import ContextVerifier
from sia.evals.runner import PipelineEvalRunner
from sia.integrity.context_isolation import assess_source_context_isolation


class _DummyHITL:
    def create_checkpoint(self, *args, **kwargs):
        class CP:
            def to_dict(self):
                return {"type": "plan_review"}

        return CP()


def _radio_cp():
    return {
        "lineage": {"sheet_name": "Radio_DE", "source_id": "radio-de"},
        "interpreted_context": {
            "fields": {"market": "DE", "channel": "radio"},
            "scoped_fields": {
                "market": {"name": "market", "value": "DE", "scope": "block", "source_id": "radio-de"},
            },
        },
    }


def test_verify_plan_context_flags_market_bleed():
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.add_column",
                "params": {"target_column": "market", "value": "UK"},
            }
        ],
        source_id="radio-de",
    )
    result = ContextVerifier.verify_plan_context(plan, _radio_cp())
    assert result.pass_ is False
    assert any(v.get("type") == "context_grounding_critical" for v in result.violations)


def test_verify_plan_context_passes_after_rebind_literals():
    plan = ExtractionPlan(
        tool_calls=[
            {
                "tool": "transform.add_column",
                "params": {"target_column": "market", "value": "DE"},
            }
        ],
        source_id="radio-de",
    )
    result = ContextVerifier.verify_plan_context(plan, _radio_cp())
    assert result.pass_ is True


def test_verify_output_frame_dimension_mismatch():
    df = pd.DataFrame({"market": ["UK"], "channel": ["digital"]})
    result = ContextVerifier.verify_output_frame(df, _radio_cp(), source_id="radio-de")
    assert result.pass_ is False
    assert any(v.get("type") == "dimension_mismatch" for v in result.violations)


def test_assess_delegates_to_verifier():
    df = pd.DataFrame({"market": ["DE"], "channel": ["radio"]})
    report = assess_source_context_isolation(df, _radio_cp(), source_id="radio-de")
    assert report["pass"] is True
    assert report["local_context"]["market"] == "DE"


def test_execute_tools_blocks_context_bleed_after_rebind_insufficient():
    """Missing lineage is a critical context gate even when tools look valid."""
    state = {
        "grid": None,
        "current_df": None,
        "suggested_tools": [{"tool": "transform.rename", "params": {"mapping": {}}}],
        "scoped_source": {},
        "hitl_checkpoints": [],
        "hitl_manager": _DummyHITL(),
        "source_id": "radio-de",
        "context_packet": {
            "lineage": {"source_id": "radio-de"},
            "interpreted_context": {"fields": {"market": "DE"}},
        },
        "extraction_plan": ExtractionPlan(
            tool_calls=[{"tool": "transform.rename", "params": {"mapping": {}}}],
            source_id="radio-de",
        ),
    }
    out = execute_tools_node(state)
    assert out.get("hitl_pending_approval") is True
    assert "Context verifier" in out.get("review_reason", "")


def test_summarize_for_judge_includes_diff_trail():
    job = {
        "pipeline_evals": {"per_source": {}, "critical_gate": {"pass": True, "blocked_reasons": []}},
        "context_diff_log": [
            {
                "stage": "plan_rebind",
                "field": "market",
                "before": "UK",
                "after": "DE",
                "source_id": "radio-de",
            }
        ],
    }
    text = PipelineEvalRunner.summarize_for_judge(job)
    assert "Context diff trail" in text
    assert "market" in text
