import pandas as pd

from sia.agent.nodes import verify_output_node


class StubVerifier:
    def verify(self, **_kwargs):
        from sia.agent.base import VerificationResult
        return VerificationResult(
            is_flat=True,
            confidence=0.95,
            issues=[],
            rule_issues=[],
            suggested_tools=[],
            summary="Structure looks flat."
        )


def test_verify_output_node_flags_business_rule_violations(monkeypatch):
    monkeypatch.setattr("sia.agent.nodes.OutputVerifier", lambda _llm_client: StubVerifier())

    state = {
        "llm_client": object(),
        "current_df": pd.DataFrame(
            {
                "date_paid_media": ["03/20/2026", "2026-03-21"],
                "clicks_paid_media": [1, ""],
            }
        ),
        "expected_columns": ["date_paid_media", "clicks_paid_media"],
        "tools_history": [],
        "issues_history": [],
        "iteration": 1,
        "max_iterations": 3,
        "business_rules": [
            {"target_column": "date_paid_media", "rule_type": "format", "rule_expression": "yyyy-mm-dd"},
            {"target_column": "clicks_paid_media", "rule_type": "fill_blank", "rule_expression": "0"},
        ],
        "planned_rule_actions": [
            {"target_column": "date_paid_media", "action": "format", "value": "yyyy-mm-dd"},
            {"target_column": "clicks_paid_media", "action": "fill_blank", "value": "0"},
        ],
        "hitl_checkpoints": [],
        "hitl_pending_approval": False,
        "confidence_trajectory": [],
        "verifier_issues": [],
    }

    result = verify_output_node(state)

    assert result["is_flat"] is False
    assert result["trace_steps"][0]["rule_issues_count"] == 2
    assert any(issue["issue_type"] == "business_rule_format_violation" for issue in result["verifier_issues"])
    assert any(issue["issue_type"] == "business_rule_blank_violation" for issue in result["verifier_issues"])
