"""Tests for aggregate sum reconciliation."""
import pandas as pd

from sia.integrity.metric_reconcile import (
    check_aggregate_sum_reconciliation,
    sums_close,
)

TEMPLATE = {
    "x_scope": {
        "uid_hierarchy": ["date", "channel"],
        "metrics": ["spends", "impressions"],
        "supporting_columns": [],
    },
    "properties": {
        "date": {},
        "channel": {},
        "spends": {},
        "impressions": {},
    },
}


def test_sums_close_within_tolerance():
    assert sums_close(1000.0, 1000.5, tol_pct=0.001, tol_abs=0.01)
    assert not sums_close(1000.0, 900.0, tol_pct=0.001, tol_abs=0.01)


def test_aggregate_sum_preserved_passes():
    before = pd.DataFrame(
        [
            {"date": "2026-01-01", "channel": "tv", "spends": 10.0, "impressions": 100},
            {"date": "2026-01-02", "channel": "tv", "spends": 20.0, "impressions": 200},
        ]
    )
    after = pd.DataFrame(
        [{"date": "2026-01-01", "channel": "tv", "spends": 30.0, "impressions": 300}]
    )
    violation = check_aggregate_sum_reconciliation(
        before,
        after,
        "transform.aggregate_weekly",
        {"metric_rules": {"spends": "sum", "impressions": "sum"}},
        {"target_template": TEMPLATE},
    )
    assert violation is None


def test_aggregate_sum_mismatch_fails():
    before = pd.DataFrame(
        [
            {"date": "2026-01-01", "channel": "tv", "spends": 10.0, "impressions": 100},
            {"date": "2026-01-02", "channel": "tv", "spends": 20.0, "impressions": 200},
        ]
    )
    after = pd.DataFrame(
        [{"date": "2026-01-01", "channel": "tv", "spends": 25.0, "impressions": 300}]
    )
    violation = check_aggregate_sum_reconciliation(
        before,
        after,
        "transform.aggregate_weekly",
        {"metric_rules": {"spends": "sum", "impressions": "sum"}},
        {"target_template": TEMPLATE},
    )
    assert violation is not None
    assert violation["subtype"] == "aggregate_sum_mismatch"
    assert violation["requires_hitl"] is True
    spend_check = next(c for c in violation["checks"] if c["column"] == "spends")
    assert spend_check["expected"] == 30.0
    assert spend_check["actual"] == 25.0
