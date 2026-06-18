"""Tests for deterministic post-tool column pruning.

These ensure excluded/unmapped columns do not survive into the final
dataframe even when the planner forgets to emit an explicit drop tool.
"""
import pandas as pd

from sia.agent.target_template_utils import (
    allowed_post_transform_columns,
    prune_dataframe_to_template,
)


TEMPLATE = {
    "x_scope": {
        "uid_hierarchy": ["date", "channel"],
        "metrics": ["spends", "impressions"],
        "supporting_columns": ["region"],
    },
    "business_logic": {"column_rules": []},
    "aggregation_logic": {},
    "properties": {
        "date": {"type": "string"},
        "channel": {"type": "string"},
        "spends": {"type": "number"},
        "impressions": {"type": "number"},
        "region": {"type": "string"},
    },
}


def test_allowed_columns_include_template_and_mappings():
    allowed = allowed_post_transform_columns(
        TEMPLATE,
        approved_mappings=[
            {"source_column": "Clicks", "target_column": "impressions", "decision": "keep"},
            {"source_column": "Notes", "target_column": "notes", "decision": "metadata"},
            {"source_column": "IgnoreMe", "target_column": "exclude", "decision": "exclude"},
            {"source_column": "Name", "target_column": "No match", "decision": "keep"},
        ],
        business_rules=[{"rule_id": "R1", "target_column": "publisher"}],
        extra_allowed=["week_start"],
    )
    assert {"date", "channel", "spends", "impressions", "region"}.issubset(allowed)
    assert "notes" in allowed
    assert "Name" in allowed
    assert "publisher" in allowed
    assert "week_start" in allowed
    assert "exclude" not in allowed


def test_prune_drops_unmapped_columns_and_reorders():
    df = pd.DataFrame(
        [
            {
                "channel": "search",
                "date": "2026-04-13",
                "impressions": 10,
                "spends": 100.0,
                "region": "NA",
                "leftover_helper": "should drop",
                "raw_notes": "should drop",
                "week_start": "2026-04-13",
            }
        ]
    )
    pruned, notes = prune_dataframe_to_template(
        df,
        TEMPLATE,
        approved_mappings=[],
        business_rules=[],
        extra_allowed=["week_start"],
    )
    assert "leftover_helper" not in pruned.columns
    assert "raw_notes" not in pruned.columns
    assert "week_start" in pruned.columns
    assert list(pruned.columns)[:5] == ["date", "channel", "region", "spends", "impressions"]
    assert notes and "pruned" in notes[0]


def test_prune_noop_when_everything_allowed():
    df = pd.DataFrame([{"date": "2026-04-13", "channel": "x", "spends": 1.0,
                         "impressions": 2, "region": "NA"}])
    pruned, notes = prune_dataframe_to_template(df, TEMPLATE)
    assert list(pruned.columns) == ["date", "channel", "region", "spends", "impressions"]
    assert any("reordered" in n for n in notes)


def test_prune_keeps_supporting_source_column_without_target_mapping():
    df = pd.DataFrame(
        [{"date": "2026-04-13", "channel": "x", "spends": 1.0, "impressions": 2, "region": "NA", "Name": "IG Reel 1"}]
    )
    pruned, notes = prune_dataframe_to_template(
        df,
        TEMPLATE,
        approved_mappings=[{"source_column": "Name", "target_column": "No match", "decision": "keep", "role": "supporting"}],
    )
    assert "Name" in pruned.columns
    assert any("reordered" in n for n in notes) or notes == []


def test_prune_returns_original_when_template_missing():
    df = pd.DataFrame([{"a": 1, "b": 2}])
    pruned, notes = prune_dataframe_to_template(df, None)
    assert pruned is df
    assert notes == []
