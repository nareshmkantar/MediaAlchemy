import json

from sia.agent.graph import should_pause_after_replan
from sia.agent.replanner import Replanner, tool_plan_fingerprint


def _parser() -> Replanner:
    return Replanner.__new__(Replanner)


def test_replanner_uses_full_tool_calls_and_ignores_legacy_delta_plan():
    response = json.dumps(
        {
            "analysis": "Fix the latest missing-column issue.",
            "strategy": "retry_different",
            "tool_calls": [
                {
                    "step": 1,
                    "tool": "transform.add_column",
                    "params": {"target_column": "market", "value": "France"},
                }
            ],
            "delta_plan": {
                "insert_after": [
                    {
                        "step": 1,
                        "op": {
                            "tool": "transform.add_column",
                            "params": {"target_column": "legacy", "value": "ignored"},
                        },
                    }
                ]
            },
            "confidence": 0.8,
            "recommendation": "proceed",
        }
    )

    plan = _parser()._parse_replan_response(
        response,
        previous_tools=[
            {
                "step": 1,
                "tool": "transform.add_column",
                "params": {"target_column": "old", "value": "old"},
            }
        ],
    )

    assert [call["params"]["target_column"] for call in plan.tool_calls] == ["market"]


def test_replanner_does_not_reconstruct_plan_from_delta_only_response():
    response = json.dumps(
        {
            "analysis": "Legacy response",
            "strategy": "retry_different",
            "delta_plan": {
                "replace": [
                    {
                        "step": 1,
                        "with": {
                            "tool": "transform.add_column",
                            "params": {"target_column": "market", "value": "France"},
                        },
                    }
                ]
            },
            "confidence": 0.5,
        }
    )

    plan = _parser()._parse_replan_response(
        response,
        previous_tools=[
            {
                "step": 1,
                "tool": "transform.add_column",
                "params": {"target_column": "old", "value": "old"},
            }
        ],
    )

    assert plan.tool_calls == []


def test_replanner_injects_real_schema_before_validation():
    template = {
        "type": "object",
        "properties": {
            "spends": {"type": "number", "minimum": 0},
            "impressions": {"type": "number", "minimum": 0},
        },
    }
    response = json.dumps(
        {
            "analysis": "Validate the corrected output.",
            "strategy": "retry_different",
            "tool_calls": [{"step": 1, "tool": "verify.schema", "params": {}}],
            "confidence": 0.9,
            "recommendation": "proceed",
        }
    )

    plan = _parser()._parse_replan_response(response, target_template=template)

    assert len(plan.tool_calls) == 1
    schema = plan.tool_calls[0]["params"]["schema"]
    assert schema["properties"]["spends"]["minimum"] == 0
    assert "impressions" in schema["properties"]


def test_strategy_escalation_maps_to_human_review():
    response = json.dumps(
        {
            "diagnosis": [{"cause": "repeated_failure", "evidence": "same issue twice"}],
            "strategy": "escalate_to_human",
            "tool_calls": [],
            "confidence": 0.2,
        }
    )

    plan = _parser()._parse_replan_response(response)

    assert plan.requires_human_review is True
    assert plan.review_reason == "Replanner recommends human intervention"
    assert "repeated_failure" in plan.reasoning


def test_plan_fingerprint_ignores_step_and_description_but_not_params():
    first = [
        {
            "step": 1,
            "tool": "transform.add_column",
            "params": {"target_column": "market", "value": "France"},
            "description": "first wording",
        }
    ]
    same = [
        {
            "step": 7,
            "tool": "transform.add_column",
            "params": {"value": "France", "target_column": "market"},
            "description": "different wording",
        }
    ]
    changed = [
        {
            "step": 1,
            "tool": "transform.add_column",
            "params": {"target_column": "market", "value": "Germany"},
        }
    ]

    assert tool_plan_fingerprint(first) == tool_plan_fingerprint(same)
    assert tool_plan_fingerprint(first) != tool_plan_fingerprint(changed)


def test_replan_without_llm_keeps_processed_frame():
    import pandas as pd

    from sia.agent.nodes import replan_node

    frame = pd.DataFrame({"date": ["2026-01-01"], "spends": [1.0]})
    out = replan_node(
        {
            "llm_client": None,
            "current_df": frame,
            "suggested_tools": [{"tool": "verify.schema"}],
            "iteration": 1,
            "trace_steps": [],
            "context_packet": {},
        }
    )
    assert out["current_df"] is frame


def test_replan_route_pauses_before_execution_when_review_is_pending():
    assert should_pause_after_replan({"hitl_pending_approval": True}) == "hitl_pause"
    assert should_pause_after_replan({"hitl_pending_approval": False}) == "execute_tools"
