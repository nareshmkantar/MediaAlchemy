"""JSON serialization for planner/debug payloads with datetimes."""

from __future__ import annotations

import datetime
import json

import pandas as pd

from sia.agent.json_safe import dumps as json_safe_dumps


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
