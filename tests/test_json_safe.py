"""JSON serialization for planner/debug payloads with datetimes."""

from __future__ import annotations

import datetime
import json
import math

import pandas as pd

from sia.agent.json_safe import dumps as json_safe_dumps, sanitize_json_tree


def test_json_safe_dumps_datetime_in_nested_structure():
    payload = {
        "tables": [
            {
                "column_analysis": [
                    {"name": "date", "sample": datetime.datetime(2024, 3, 15, 12, 0, 0)},
                ]
            }
        ]
    }
    text = json_safe_dumps(payload, indent=2)
    parsed = json.loads(text)
    assert "2024-03-15" in parsed["tables"][0]["column_analysis"][0]["sample"]


def test_json_safe_dumps_pandas_timestamp():
    payload = {"ts": pd.Timestamp("2024-01-01")}
    text = json_safe_dumps(payload)
    assert "2024-01-01" in text


def test_json_safe_dumps_nan_becomes_null():
    payload = {"units_sold": float("nan"), "ok": 1.0}
    parsed = json.loads(json_safe_dumps(payload))
    assert parsed["units_sold"] is None
    assert parsed["ok"] == 1.0


def test_sanitize_json_tree_nested_nan():
    out = sanitize_json_tree({"rows": [{"units_sold": math.nan}]})
    assert out["rows"][0]["units_sold"] is None
