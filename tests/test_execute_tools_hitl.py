import pandas as pd

from sia.agent.hitl import HITLManager
from sia.agent.nodes import execute_tools_node


def test_execute_tools_node_routes_malformed_tools_to_plan_review():
    state = {
        "grid": None,
        "current_df": pd.DataFrame({"value": [1, 2]}),
        "suggested_tools": [{"tool": "transform.keep_columns", "params": {}, "_valid": False}],
        "expected_columns": ["value"],
        "hitl_manager": HITLManager(),
        "hitl_checkpoints": [],
    }

    result = execute_tools_node(state)

    assert result["requires_review"] is True
    assert result["hitl_pending_approval"] is True
    assert result["hitl_pause_type"] == "plan_review"
    assert len(result["hitl_checkpoints"]) == 1
    checkpoint = result["hitl_checkpoints"][0]
    assert checkpoint["checkpoint_type"] == "plan_review"
    assert checkpoint["title"] == "Execution Plan Review Required"


def test_execute_tools_node_routes_missing_extraction_to_plan_review():
    state = {
        "grid": None,
        "current_df": None,
        "suggested_tools": [{"tool": "transform.keep_columns", "params": {"columns": ["value"]}}],
        "scoped_source": {"requires_extraction": True},
        "expected_columns": ["value"],
        "hitl_manager": HITLManager(),
        "hitl_checkpoints": [],
    }

    result = execute_tools_node(state)

    assert result["requires_review"] is True
    assert result["hitl_pending_approval"] is True
    assert result["hitl_pause_type"] == "plan_review"
    assert len(result["hitl_checkpoints"]) == 1
    checkpoint = result["hitl_checkpoints"][0]
    assert checkpoint["checkpoint_type"] == "plan_review"
    assert checkpoint["title"] == "Extraction Step Missing"
