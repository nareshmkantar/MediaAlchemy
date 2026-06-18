"""Tests for transform.infer_block_boundary_columns."""

import json

import pandas as pd

from sia.tools.transformation_tools import TransformationTools
from sia.tools.tool_validator import normalize_tool_name


def test_infer_finds_name_as_delimiter_when_ref_dropped():
    """After dropping Ref, Name still marks block-header rows sparsely."""
    df = pd.DataFrame(
        [
            {"Ref": "R1", "Name": "Alice", "channel": "fb", "spends": 100.0},
            {"Ref": "", "Name": "", "channel": "fb", "spends": ""},
            {"Ref": "", "Name": "", "channel": "fb", "spends": ""},
            {"Ref": "R2", "Name": "Bob", "channel": "yt", "spends": 200.0},
            {"Ref": "", "Name": "", "channel": "yt", "spends": ""},
        ]
    )
    df2 = df.drop(columns=["Ref"])
    r = TransformationTools.infer_block_boundary_columns(
        df2,
        exclude_columns=["spends"],
        metric_columns_hint=["spends", "impressions"],
    )
    assert r.success
    report = json.loads(r.message)
    assert "Name" in report["recommended_block_start_columns"]


def test_infer_respects_exclude_columns():
    df = pd.DataFrame(
        [
            {"Label": "x", "Spend": 50},
            {"Label": "", "Spend": ""},
            {"Label": "y", "Spend": 200},
            {"Label": "", "Spend": ""},
        ]
    )
    r = TransformationTools.infer_block_boundary_columns(df, exclude_columns=["Label"])
    assert r.success
    report = json.loads(r.message)
    assert "Label" not in report["recommended_block_start_columns"]


def test_normalize_alias_suggest_block():
    name, ok = normalize_tool_name("suggest_block_start_columns")
    assert ok
    assert name == "transform.infer_block_boundary_columns"
