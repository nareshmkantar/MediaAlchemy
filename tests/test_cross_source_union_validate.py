"""Tests for cross-source union readiness validation and tool wiring."""

import pandas as pd
import pytest

from sia.tools.cross_source_union_validate import validate_column_sets_for_union
from sia.tools.pipeline_catalog import TOOL_PRIMARY_STAGE, assert_primary_stage_coverage
from sia.tools.tool_validator import TOOL_SCHEMAS
from sia.tools.transformation_tools import TransformationTools


def test_column_sets_from_job_mapping_use_keep_targets_not_raw_headers():
    from web_server import column_sets_by_source_from_job_mapping

    job = {
        "mapping_registry": [
            {
                "source_id": "amazon",
                "source_column": "orderStartDate",
                "target_column": "date",
                "decision": "Keep",
            },
            {
                "source_id": "amazon",
                "source_column": "date",
                "target_column": "No match",
                "role": "exclude",
                "decision": "Discard",
            },
            {
                "source_id": "cm360",
                "source_column": "Campaign End Date",
                "target_column": "date",
                "decision": "Keep",
            },
            {
                "source_id": "cm360",
                "source_column": "Media Cost",
                "target_column": "spends",
                "decision": "Keep",
            },
        ]
    }
    sets = column_sets_by_source_from_job_mapping(job)
    assert sets["amazon"] == ["date"]
    assert sets["cm360"] == ["date", "spends"]
    report = validate_column_sets_for_union(sets)
    assert "date" in report["intersection"]
    assert not any(w.get("code") == "NO_COLUMN_INTERSECTION" for w in report["warnings"])


def test_validate_column_sets_for_union_overlap():
    r = validate_column_sets_for_union(
        {"a": ["x", "y"], "b": ["x", "z"]},
        key_hints=["x"],
    )
    assert r["ok"] is True
    assert "x" in r["intersection"]
    assert not any(w.get("code") == "NO_COLUMN_INTERSECTION" for w in r["warnings"])


def test_validate_column_sets_for_union_disjoint():
    r = validate_column_sets_for_union({"a": ["u"], "b": ["v"]})
    assert r["blocking"] is True
    assert any(w.get("code") == "NO_COLUMN_INTERSECTION" for w in r["warnings"])


def test_validate_key_hint_not_shared():
    r = validate_column_sets_for_union({"a": ["x"], "b": ["x"]}, key_hints=["missing"])
    assert any(w.get("code") == "KEY_HINT_NOT_SHARED" for w in r["warnings"])


def test_validate_date_granularity_mismatch():
    r = validate_column_sets_for_union(
        {"s1": ["x"], "s2": ["x"]},
        date_granularity_by_source={"s1": "daily", "s2": "weekly"},
    )
    assert any(w.get("code") == "DATE_GRANULARITY_MISMATCH" for w in r["warnings"])


def test_tool_primary_stage_covers_new_tools():
    assert_primary_stage_coverage(list(TOOL_SCHEMAS.keys()))
    assert "transform.align_schema" in TOOL_PRIMARY_STAGE
    assert "transform.union_resolve" in TOOL_PRIMARY_STAGE
    assert "validate.cross_source_union" in TOOL_PRIMARY_STAGE


def test_align_schema_deterministic_order():
    df = pd.DataFrame({"b": [1, 2], "a": [3, 4]})
    res = TransformationTools.align_schema(
        df,
        target_columns=["a", "c", "b"],
        aliases={"c": "a"},
        fill_value=0,
    )
    assert res.success
    assert list(res.data.columns) == ["a", "c", "b"]
    assert res.data["c"].tolist() == [3, 4]


def test_union_resolve_first_and_sum():
    df = pd.DataFrame({"k": [1, 1, 2], "m": [10.0, 5.0, 3.0]})
    r1 = TransformationTools.union_resolve(df, key_columns=["k"], policy="first")
    assert r1.success and len(r1.data) == 2
    r2 = TransformationTools.union_resolve(df, key_columns=["k"], policy="sum", metric_columns=["m"])
    assert r2.success
    row = r2.data[r2.data["k"] == 1].iloc[0]
    assert float(row["m"]) == pytest.approx(15.0)


def test_union_resolve_prefer_source():
    df = pd.DataFrame(
        {"k": [1, 1, 2], "src": ["A", "B", "B"], "v": [10, 20, 30]},
    )
    r = TransformationTools.union_resolve(
        df,
        key_columns=["k"],
        policy="prefer_source",
        source_tag_column="src",
        prefer_source_id="B",
    )
    assert r.success
    assert r.data[r.data["k"] == 1].iloc[0]["v"] == 20


def test_validate_cross_source_union_tool_unchanged_frame():
    df = pd.DataFrame({"x": [1]})
    r = TransformationTools.validate_cross_source_union_tool(
        df,
        column_sets_by_source={"s1": ["a"], "s2": ["b"]},
    )
    assert r.success
    assert len(r.data) == len(df)
    assert "union_readiness" in (r.changes_made or {})
