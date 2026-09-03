"""Phase 4: context diff log."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from sia.context.diff_log import (
    MAX_CONTEXT_DIFF_LOG,
    append_context_diff,
    build_debug_export_payload,
    distinct_column_values,
    ensure_context_diff_log,
    filter_context_diff_log,
    log_dimension_column_snapshots,
    log_rebind_actions,
    log_scoped_fields_built,
    log_value_change,
)
from sia.integrity.context_isolation import (
    apply_local_context_dimensions,
    rebind_source_local_plan_literals,
)


def test_ring_buffer_caps_at_max_entries():
    job: dict = {}
    for i in range(MAX_CONTEXT_DIFF_LOG + 5):
        append_context_diff(
            job,
            {
                "stage": "test",
                "source_id": "s1",
                "field": f"f{i}",
                "before": i,
                "after": i + 1,
                "reason": "x",
            },
        )
    log = ensure_context_diff_log(job)
    assert len(log) == MAX_CONTEXT_DIFF_LOG
    assert log[0]["field"] == "f5"


def test_log_rebind_parses_market_change():
    job: dict = {}
    log_rebind_actions(
        job,
        source_id="radio-de",
        actions=["rebound market: 'UK' → 'DE' (source-local context)"],
    )
    row = job["context_diff_log"][0]
    assert row["field"] == "market"
    assert row["before"] == "UK"
    assert row["after"] == "DE"
    assert row["stage"] == "plan_rebind"


def test_dimension_snapshot_logs_distinct_change():
    job: dict = {}
    before = pd.DataFrame({"market": ["UK", "UK", ""]})
    after = pd.DataFrame({"market": ["DE", "DE"]})
    logged = log_dimension_column_snapshots(
        job,
        stage="stamp_dimensions",
        source_id="radio-de",
        df_before=before,
        df_after=after,
    )
    assert len(logged) == 1
    assert job["context_diff_log"][0]["before"] == ["UK"]
    assert job["context_diff_log"][0]["after"] == ["DE"]


def test_apply_local_context_dimensions_writes_diff_log():
    job: dict = {}
    df = pd.DataFrame({"market": ["UK"], "spends": [10.0]})
    cp = {
        "interpreted_context": {
            "fields": {"market": "DE"},
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "DE",
                    "scope": "block",
                    "confidence": 1.0,
                    "source_id": "radio-de",
                },
            },
        },
    }
    out = apply_local_context_dimensions(df, cp, job=job, source_id="radio-de")
    assert out is not None
    assert list(out["market"]) == ["DE"]
    market_rows = [r for r in job["context_diff_log"] if r.get("field") == "market"]
    assert market_rows
    assert market_rows[0]["before"] == ["UK"]
    assert market_rows[0]["after"] == ["DE"]


def test_rebind_integration_logs_to_job():
    job: dict = {}
    cp = {
        "interpreted_context": {
            "fields": {"market": "DE"},
            "scoped_fields": {
                "market": {"name": "market", "value": "DE", "scope": "block", "source_id": "radio-de"},
            },
        },
        "lineage": {"source_id": "radio-de"},
    }
    tools = [
        {
            "tool": "transform.add_column",
            "params": {"target_column": "market", "value": "UK"},
        }
    ]
    rebound, actions = rebind_source_local_plan_literals(tools, cp, job=job)
    assert rebound[0]["params"]["value"] == "DE"
    assert actions
    assert any(r.get("field") == "market" for r in job["context_diff_log"])


def test_filter_context_diff_log():
    job = {
        "context_diff_log": [
            {"source_id": "a", "field": "market", "stage": "packet_build"},
            {"source_id": "b", "field": "channel", "stage": "plan_rebind"},
        ]
    }
    rows = filter_context_diff_log(job, source_id="a", field="market")
    assert len(rows) == 1


def test_build_debug_export_includes_trail():
    job = {"id": "j1", "context_diff_log": [{"field": "market"}]}
    payload = build_debug_export_payload(job)
    assert payload["job_id"] == "j1"
    assert payload["context_diff_log"][0]["field"] == "market"


def test_log_scoped_fields_built():
    job: dict = {}
    log_scoped_fields_built(job, source_id="s1", fields={"market": "DE", "channel": "radio"})
    assert len(job["context_diff_log"]) == 2


def test_distinct_column_values():
    df = pd.DataFrame({"market": ["UK", "", None, "UK"]})
    assert distinct_column_values(df, "market") == ["UK"]
    assert distinct_column_values(df, "missing") == []


_GOLDEN_REBIND = Path(__file__).resolve().parent.parent / "fixtures" / "context_flow" / "rebind_diff_golden.json"


def test_rebind_diff_log_matches_golden_snapshot():
    """Phase 10.2: golden diff rows for CP-07-style plan rebind."""
    golden = json.loads(_GOLDEN_REBIND.read_text(encoding="utf-8"))
    job: dict = {}
    cp = {
        "interpreted_context": {
            "fields": {"market": "DE", "channel": "radio"},
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "DE",
                    "scope": "block",
                    "source_id": "job:file:Radio_DE",
                },
                "channel": {
                    "name": "channel",
                    "value": "radio",
                    "scope": "block",
                    "source_id": "job:file:Radio_DE",
                },
            },
        },
        "lineage": {"source_id": "job:file:Radio_DE"},
    }
    tools = [
        {"tool": "transform.add_column", "params": {"target_column": "market", "value": "UK"}},
        {"tool": "transform.add_column", "params": {"target_column": "channel", "value": "digital"}},
    ]
    rebind_source_local_plan_literals(tools, cp, job=job)
    actual = [
        {k: row[k] for k in ("stage", "source_id", "field", "before", "after", "reason")}
        for row in job["context_diff_log"]
    ]
    assert actual == golden

