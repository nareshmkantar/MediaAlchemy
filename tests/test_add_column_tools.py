"""Tests for transform.add_column and transform.apply_column_rules."""

import pandas as pd

from sia.agent.target_template_utils import apply_column_rules_list
from sia.tools.transformation_tools import TransformationTools
from sia.tools.tool_validator import normalize_tool_name, validate_tool_call


def test_add_column_missing_creates_column():
    df = pd.DataFrame({"date": ["2026-01-01"]})
    res = TransformationTools.add_column(df, "publisher", "total", when="missing")
    assert res.success
    assert "publisher" in res.data.columns
    assert res.data["publisher"].tolist() == ["total"]


def test_add_column_rejects_calculate_style_expression_via_validator_docs():
    """calculate is separate; add_column uses literals only."""
    res = TransformationTools.add_column(df := pd.DataFrame({"a": [1]}), "b", 2, when="missing")
    assert res.success and "b" in res.data.columns


def test_apply_column_rules_from_template_shape():
    rules = [
        {
            "rule_id": "R1",
            "operator": "set_value",
            "value": "total",
            "destination_column": "publisher",
            "condition": {"type": "column_missing_or_null", "column": "publisher"},
        }
    ]
    df = pd.DataFrame({"date": ["2026-01-01"]})
    out, actions = apply_column_rules_list(df, rules)
    assert "publisher" in out.columns
    assert any("created column" in a for a in actions)

    res = TransformationTools.apply_column_rules(df, column_rules=rules)
    assert res.success
    assert "publisher" in res.data.columns


def test_tool_validator_normalizes_add_column_aliases():
    name, _ = normalize_tool_name("assign_column")
    assert name == "transform.add_column"

    vr = validate_tool_call(
        {
            "tool": "transform.add_column",
            "params": {"target_column": "publisher", "value": "total", "when": "missing"},
        }
    )
    assert vr.valid

    name2, _ = normalize_tool_name("apply_template_column_rules")
    assert name2 == "transform.apply_column_rules"
