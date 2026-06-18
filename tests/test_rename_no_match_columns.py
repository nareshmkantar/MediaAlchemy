"""Rename must keep No match / supporting columns at their physical source names."""
import pandas as pd

from sia.agent.target_template_utils import (
    allowed_post_transform_columns,
    build_rename_mapping_from_approved_mappings,
    sanitize_rename_mapping,
)
from sia.tools.transformation_tools import TransformationTools


def test_sanitize_rename_mapping_never_targets_no_match():
    raw = {
        "Name": "No match",
        "Market": "no match",
        "Performance_Views": "impressions",
    }
    assert sanitize_rename_mapping(raw) == {"Performance_Views": "impressions"}


def test_build_rename_mapping_from_approved_mappings_skips_no_match():
    approved = [
        {"source_column": "Name", "target_column": "No match", "decision": "keep"},
        {"source_column": "POST", "target_column": "channel", "decision": "keep"},
    ]
    assert build_rename_mapping_from_approved_mappings(approved) == {"POST": "channel"}


def test_rename_columns_preserves_no_match_source_columns():
    df = pd.DataFrame(
        {
            "NAME": ["Creator A"],
            "MARKET": ["ID"],
            "POST": ["IG Reel 1"],
            "POSTING DATE": ["2024-01-12"],
            "PERFORMANCE_VIEWS": [1615],
            "BUDGETS_TOTAL": [4285.54],
        }
    )
    mapping = {
        "POST": "channel",
        "POSTING DATE": "date",
        "PERFORMANCE_VIEWS": "impressions",
        "BUDGETS_TOTAL": "spends",
        "NAME": "No match",
        "MARKET": "No match",
    }
    result = TransformationTools.rename_columns(df, mapping)
    assert result.success
    cols = set(result.data.columns)
    assert cols == {"NAME", "MARKET", "channel", "date", "impressions", "spends"}
    assert "No match" not in cols


def test_rename_strips_trailing_whitespace_on_all_columns():
    df = pd.DataFrame(
        {
            "date ": [1],
            "publisher ": [2],
            "Media Spend": [3.0],
        }
    )
    result = TransformationTools.rename_columns(df, {"date ": "calendar_date"})
    assert result.success
    assert list(result.data.columns) == ["calendar_date", "publisher", "Media Spend"]
    assert result.changes_made.get("stripped_label_whitespace") == {"publisher ": "publisher"}


def test_rename_strip_preserves_internal_spaces():
    df = pd.DataFrame({" POSTING DATE ": ["2024-01-01"]})
    result = TransformationTools.rename_columns(df, {})
    assert result.success
    assert list(result.data.columns) == ["POSTING DATE"]


def test_allowed_columns_keep_no_match_without_supporting_role():
    template = {
        "x_scope": {"uid_hierarchy": ["date", "channel"], "metrics": ["impressions", "spends"]},
        "properties": {
            "date": {},
            "channel": {},
            "impressions": {},
            "spends": {},
        },
    }
    allowed = allowed_post_transform_columns(
        template,
        approved_mappings=[
            {"source_column": "NAME", "target_column": "No match", "decision": "keep"},
            {"source_column": "POST", "target_column": "channel", "decision": "keep"},
        ],
    )
    assert "NAME" in allowed
    assert "channel" in allowed
