"""Tests for smart drop_columns HITL (skip user-discarded columns)."""
import pandas as pd

from sia.integrity.drop_columns_hitl import (
    drop_columns_requires_hitl,
    generate_drop_columns_hitl_preview,
    user_excluded_column_names,
)

TEMPLATE = {
    "x_scope": {
        "uid_hierarchy": ["date", "channel"],
        "metrics": ["spends", "impressions"],
        "supporting_columns": ["region"],
    },
    "properties": {
        "date": {},
        "channel": {},
        "spends": {},
        "impressions": {},
        "region": {},
    },
}


def test_user_excluded_from_mapping_discard():
    state = {
        "approved_mappings": [
            {"source_column": "junk_col", "decision": "discard"},
        ],
    }
    assert "junk_col" in user_excluded_column_names(state)


def test_user_excluded_from_mapping_role_exclude():
    state = {
        "approved_mappings": [
            {"source_column": "orderId", "target_column": "No match", "role": "exclude", "decision": "Keep"},
        ],
    }
    assert "orderid" in user_excluded_column_names(state)


def test_drop_user_discarded_column_skips_hitl():
    df = pd.DataFrame(
        [{"date": "2026-01-01", "junk_col": "x", "spends": 10.0, "impressions": 100}]
    )
    state = {
        "target_template": TEMPLATE,
        "approved_mappings": [
            {"source_column": "junk_col", "decision": "discard"},
        ],
    }
    requires, sensitive = drop_columns_requires_hitl(
        state, {"columns": ["junk_col"]}, df
    )
    assert requires is False
    assert sensitive == []


def test_drop_metric_column_requires_hitl():
    df = pd.DataFrame(
        [{"date": "2026-01-01", "channel": "tv", "spends": 10.0, "impressions": 100}]
    )
    state = {"target_template": TEMPLATE, "approved_mappings": []}
    requires, sensitive = drop_columns_requires_hitl(state, {"columns": ["spends"]}, df)
    assert requires is True
    assert "spends" in sensitive


def test_generate_preview_for_sensitive_drop():
    df = pd.DataFrame(
        [{"date": "2026-01-01", "channel": "tv", "spends": 10.0, "impressions": 100}]
    )
    state = {"target_template": TEMPLATE, "approved_mappings": []}
    preview = generate_drop_columns_hitl_preview(
        df, "transform.drop_columns", {"columns": ["spends"]}, state
    )
    assert preview is not None
    assert "spends" in preview.columns_to_delete
