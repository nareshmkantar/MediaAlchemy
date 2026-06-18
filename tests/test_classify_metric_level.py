"""Tests for transform.classify_metric_level heuristic grain labelling."""

import json

import pandas as pd

from sia.tools.transformation_tools import TransformationTools
from sia.tools.tool_validator import normalize_tool_name


def test_classify_detects_block_spend_and_row_impressions():
    df = pd.DataFrame(
        [
            {"Ref": "R1", "Post": "", "Spend": "100", "Impressions": "1000"},
            {"Ref": "", "Post": "P1", "Spend": "", "Impressions": "400"},
            {"Ref": "", "Post": "P2", "Spend": "", "Impressions": "600"},
            {"Ref": "R2", "Post": "", "Spend": "200", "Impressions": "2000"},
            {"Ref": "", "Post": "P3", "Spend": "", "Impressions": "2000"},
        ]
    )
    r = TransformationTools.classify_metric_level(df, block_start_columns=["Ref"])
    assert r.success
    report = json.loads(r.message)
    cols = {c["column"]: c for c in report["columns"]}
    assert cols["Spend"]["metric_grain_level"] == "block_header_total"
    assert cols["Impressions"]["metric_grain_level"] == "post_row_metric"
    sug = cols["Spend"]["suggested_allocate_block_metric"]
    assert sug is not None
    assert sug["method"] == "by_weight"
    assert sug["weight_col"] == "Impressions"


def test_classify_without_boundaries_is_unspecified():
    df = pd.DataFrame([{"a": 1, "b": 2}])
    r = TransformationTools.classify_metric_level(df, block_start_columns=None)
    assert r.success
    report = json.loads(r.message)
    for c in report["columns"]:
        assert c["metric_grain_level"] == "unspecified_without_boundaries"


def test_normalize_Classify_Metric_Level_alias():
    name, ok = normalize_tool_name("Classify_Metric_Level")
    assert ok
    assert name == "transform.classify_metric_level"
