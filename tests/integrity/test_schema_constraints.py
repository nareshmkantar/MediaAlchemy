"""Schema min/max constraint validation and Review-style HITL."""
from __future__ import annotations

import json

import pandas as pd

from sia.integrity.schema_constraints import (
    apply_constraint_resolutions,
    constraint_issues_from_dataframe,
    constraint_issues_from_report,
    enrich_constraint_issue,
    parse_validation_report,
    schema_constraint_to_hitl_state,
)
from sia.tools.transformation_tools import TransformationTools


def _schema_with_min_spend():
    return {
        "properties": {
            "spends": {
                "type": "number",
                "minimum": 0,
                "x_requirement": "mandatory",
            },
            "impressions": {
                "type": "number",
                "minimum": 0,
                "x_requirement": "optional",
            },
        }
    }


def test_lightweight_gate_scans_min_max_only_after_column_typing():
    df = pd.DataFrame(
        {
            "spends": [12.0, -3.5],
            "impressions": [100, 200],
            "channel": ["social", "not-an-enum"],
        }
    )
    schema = _schema_with_min_spend()
    schema["properties"]["channel"] = {
        "type": "string",
        "enum": ["social", "search"],
    }

    issues = constraint_issues_from_dataframe(df, schema)

    assert len(issues) == 1
    assert issues[0]["column"] == "spends"
    assert issues[0]["issue"] == "MIN_VIOLATION"
    assert issues[0]["check_phase"] == "post_column_typing"
    assert issues[0]["sample_values"] == [-3.5]


def test_lightweight_gate_is_case_insensitive_and_supports_maximum():
    df = pd.DataFrame({"SPENDS": [20, 101]})
    schema = {
        "properties": {
            "spends": {"type": "number", "minimum": 0, "maximum": 100},
        }
    }

    issues = constraint_issues_from_dataframe(df, schema)

    assert len(issues) == 1
    assert issues[0]["issue"] == "MAX_VIOLATION"
    assert issues[0]["resolved_column"] == "SPENDS"
    assert issues[0]["violation_count"] == 1


def test_lightweight_gate_ignores_missing_and_non_constrained_columns():
    df = pd.DataFrame({"channel": ["social"], "other": [-10]})
    assert constraint_issues_from_dataframe(df, _schema_with_min_spend()) == []


def test_validate_against_schema_flags_negative_spends_as_medium_fail():
    df = pd.DataFrame({"spends": [10.0, -5.0, 0.0], "impressions": [100, 50, 0]})
    result = TransformationTools.validate_against_schema(df, _schema_with_min_spend())
    assert result.success
    report = result.changes_made["validation_report"]
    assert report["valid"] is False
    assert report["constraint_violation_count"] == 1
    issues = [i for i in report["issues"] if i["issue"] == "MIN_VIOLATION"]
    assert len(issues) == 1
    assert issues[0]["severity"] == "MEDIUM"
    assert issues[0]["column"] == "spends"
    assert issues[0]["count"] == 1
    assert -5.0 in issues[0]["sample_values"]


def test_validate_against_schema_flags_maximum_breach():
    schema = {
        "properties": {
            "spends": {"type": "number", "maximum": 100, "x_requirement": "mandatory"},
        }
    }
    df = pd.DataFrame({"spends": [10.0, 250.0]})
    result = TransformationTools.validate_against_schema(df, schema)
    report = result.changes_made["validation_report"]
    assert report["valid"] is False
    max_issues = [i for i in report["issues"] if i["issue"] == "MAX_VIOLATION"]
    assert len(max_issues) == 1
    assert max_issues[0]["bound"] == 100


def test_parse_and_extract_constraint_issues_from_message():
    df = pd.DataFrame({"spends": [-1.0], "impressions": [1]})
    result = TransformationTools.validate_against_schema(df, _schema_with_min_spend())
    report = parse_validation_report(message=result.message, changes_made=result.changes_made)
    issues = constraint_issues_from_report(report)
    assert len(issues) == 1
    enriched = enrich_constraint_issue(df, issues[0])
    assert enriched["violation_count"] == 1
    assert enriched["resolved_column"] == "spends"


def test_schema_constraint_to_hitl_state_builds_review_checkpoint():
    df = pd.DataFrame({"spends": [-2.0, 3.0]})
    violations = [
        {
            "column": "spends",
            "issue": "MIN_VIOLATION",
            "bound": 0,
            "detail": "1 values below minimum (0)",
            "count": 1,
        }
    ]
    payload = schema_constraint_to_hitl_state(
        violations,
        df,
        {"iteration": 1, "current_step": "execute_tools", "hitl_checkpoints": []},
        tools_history_slice=[],
        deferred_post_collate=[],
        warnings=[],
        low_confidence_items=[],
        tool_index=4,
    )
    assert payload["hitl_pending_approval"] is True
    assert payload["hitl_pause_type"] == "schema_constraint_review"
    assert payload["hitl_checkpoints"][0]["checkpoint_type"] == "schema_mismatch"
    assert payload["hitl_checkpoints"][0]["trigger_data"]["tool_index"] == 4


def test_apply_constraint_resolutions_clip_and_drop():
    df = pd.DataFrame({"spends": [-10.0, 5.0, -1.0], "impressions": [1, 2, 3]})
    violations = [
        {
            "column": "spends",
            "issue": "MIN_VIOLATION",
            "bound": 0,
            "resolved_column": "spends",
        }
    ]
    clipped, notes = apply_constraint_resolutions(
        df, {"spends": "clip_to_bound"}, violations
    )
    assert list(clipped["spends"]) == [0.0, 5.0, 0.0]
    assert any("clipped" in n for n in notes)

    dropped, notes2 = apply_constraint_resolutions(
        df, {"spends": "drop_rows"}, violations
    )
    assert len(dropped) == 1
    assert float(dropped.iloc[0]["spends"]) == 5.0
    assert any("Dropped" in n for n in notes2)


def test_apply_keep_as_is_leaves_negatives():
    df = pd.DataFrame({"spends": [-3.0, 1.0]})
    out, notes = apply_constraint_resolutions(
        df,
        {"spends": "keep_as_is"},
        [{"column": "spends", "issue": "MIN_VIOLATION", "bound": 0}],
    )
    assert list(out["spends"]) == [-3.0, 1.0]
    assert any("kept" in n.lower() for n in notes)


def test_message_json_roundtrip_still_parses():
    df = pd.DataFrame({"spends": [-1.0]})
    result = TransformationTools.validate_against_schema(df, _schema_with_min_spend())
    parsed = json.loads(result.message)
    assert parsed["valid"] is False
    assert constraint_issues_from_report(parsed)
