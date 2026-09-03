"""Tests for catastrophic integrity pause triggers."""
import pandas as pd

from sia.integrity.metric_reconcile import check_post_tool_integrity
from sia.tools.tool_validator import is_destructive_tool

TEMPLATE = {
    "x_scope": {
        "uid_hierarchy": ["date"],
        "metrics": ["spends", "impressions"],
        "supporting_columns": [],
    },
    "properties": {"date": {}, "spends": {}, "impressions": {}},
}


def test_zero_rows_triggers_violation():
    before = pd.DataFrame([{"date": "2026-01-01", "spends": 1.0, "impressions": 2}])
    after = before.iloc[0:0].copy()
    violation = check_post_tool_integrity(
        before,
        after,
        "transform.filter_summaries",
        {},
        {"target_template": TEMPLATE},
        rows_before=1,
        rows_after=0,
        is_destructive_fn=is_destructive_tool,
        norm_tool="transform.filter_summaries",
    )
    assert violation is not None
    assert violation["subtype"] == "zero_rows"
    assert violation["requires_hitl"] is True


def test_mass_row_loss_triggers_violation():
    before = pd.DataFrame({"date": [f"2026-01-{i:02d}" for i in range(1, 11)], "spends": range(10)})
    after = before.head(1).copy()
    violation = check_post_tool_integrity(
        before,
        after,
        "transform.filter_summaries",
        {},
        {"target_template": TEMPLATE},
        rows_before=10,
        rows_after=1,
        is_destructive_fn=is_destructive_tool,
        norm_tool="transform.filter_summaries",
    )
    assert violation is not None
    assert violation["subtype"] == "mass_row_loss"


def test_metric_column_wiped_triggers_violation():
    before = pd.DataFrame([{"date": "2026-01-01", "spends": 100.0, "impressions": 50}])
    after = pd.DataFrame([{"date": "2026-01-01", "impressions": 50}])
    violation = check_post_tool_integrity(
        before,
        after,
        "transform.drop_columns",
        {"columns": ["spends"]},
        {"target_template": TEMPLATE},
        rows_before=1,
        rows_after=1,
        is_destructive_fn=is_destructive_tool,
        norm_tool="transform.drop_columns",
    )
    assert violation is not None
    assert violation["subtype"] == "metric_column_wiped"
    assert "spends" in violation["columns_affected"]


def test_metric_column_text_values_after_layout_extract_not_treated_as_wiped():
    """Pre–type-cast text metrics should not false-positive after layout.extract."""
    before = pd.DataFrame([{"date": "2026-01-01", "spends": 100.0, "impressions": 50}])
    after = pd.DataFrame([{"date": "2026-01-01", "spends": "1,234.50", "impressions": "50"}])
    violation = check_post_tool_integrity(
        before,
        after,
        "layout.extract",
        {},
        {"target_template": TEMPLATE},
        rows_before=1,
        rows_after=1,
        is_destructive_fn=is_destructive_tool,
        norm_tool="layout.extract",
    )
    assert violation is None


def test_metric_column_rename_case_change_not_treated_as_wiped():
    """Impressions → IMPRESSIONS after transform.rename is not a metric wipe."""
    before = pd.DataFrame(
        [
            {"channel": "digital", "date": "2025-01-01", "Impressions": 3982, "Spends": 269},
            {"channel": "digital", "date": "2025-01-02", "Impressions": 4910, "Spends": 328},
        ]
    )
    after = before.rename(
        columns={"Impressions": "IMPRESSIONS", "Spends": "SPENDS", "channel": "CHANNEL", "date": "DATE"}
    )
    violation = check_post_tool_integrity(
        before,
        after,
        "transform.rename",
        {
            "mapping": {
                "Impressions": "IMPRESSIONS",
                "Spends": "SPENDS",
                "channel": "CHANNEL",
                "date": "DATE",
            }
        },
        {"target_template": TEMPLATE},
        rows_before=len(before),
        rows_after=len(after),
        is_destructive_fn=is_destructive_tool,
        norm_tool="transform.rename",
    )
    assert violation is None


def test_metric_column_rename_to_template_target_not_treated_as_wiped():
    before = pd.DataFrame([{"date": "2026-01-01", "Impr": 100.0, "spends": 50}])
    after = pd.DataFrame([{"date": "2026-01-01", "impressions": 100.0, "spends": 50}])
    violation = check_post_tool_integrity(
        before,
        after,
        "transform.rename",
        {"mapping": {"Impr": "impressions"}},
        {"target_template": TEMPLATE},
        rows_before=1,
        rows_after=1,
        is_destructive_fn=is_destructive_tool,
        norm_tool="transform.rename",
    )
    assert violation is None


def test_metric_column_rename_cost_to_spends_not_treated_as_wiped():
    """Renaming Cost → spends is intentional; must not pause as metric wipe."""
    before = pd.DataFrame([{"date": "2026-01-01", "Cost": 100.0, "impressions": 50}])
    after = pd.DataFrame([{"date": "2026-01-01", "spends": 100.0, "impressions": 50}])
    mappings = [{"source_column": "Cost", "target_column": "spends", "decision": "map"}]
    violation = check_post_tool_integrity(
        before,
        after,
        "transform.rename",
        {"mapping": {"Cost": "spends"}},
        {"target_template": TEMPLATE, "approved_mappings": mappings},
        rows_before=1,
        rows_after=1,
        is_destructive_fn=is_destructive_tool,
        norm_tool="transform.rename",
    )
    assert violation is None


def test_metric_column_layout_extract_spend_vs_spends_not_wiped():
    """layout.extract may normalize Spends → SPEND; template metric spends must still match."""
    before = pd.DataFrame([{"date": "2026-01-01", "Spends": 207.0, "impressions": 2276}])
    after = pd.DataFrame([{"date": "2026-01-01", "SPEND": 207.0, "IMPRESSIONS": 2276}])
    violation = check_post_tool_integrity(
        before,
        after,
        "layout.extract",
        {},
        {"target_template": TEMPLATE},
        rows_before=1,
        rows_after=1,
        is_destructive_fn=is_destructive_tool,
        norm_tool="layout.extract",
    )
    assert violation is None


def test_metric_column_wiped_still_triggers_when_truly_removed():
    before = pd.DataFrame([{"date": "2026-01-01", "spends": 100.0, "impressions": 50}])
    after = pd.DataFrame([{"date": "2026-01-01", "spends": 100.0}])
    violation = check_post_tool_integrity(
        before,
        after,
        "transform.drop_columns",
        {"columns": ["impressions"]},
        {"target_template": TEMPLATE},
        rows_before=1,
        rows_after=1,
        is_destructive_fn=is_destructive_tool,
        norm_tool="transform.drop_columns",
    )
    assert violation is not None
    assert violation["subtype"] == "metric_column_wiped"
    assert "impressions" in [c.lower() for c in violation["columns_affected"]]


def test_integrity_violation_to_hitl_state_includes_checkpoint():
    from sia.integrity.metric_reconcile import integrity_violation_to_hitl_state

    violation = {
        "type": "integrity_violation",
        "subtype": "zero_rows",
        "tool": "transform.filter_summaries",
        "details": "CRITICAL: all rows removed",
        "confidence": 0.1,
    }
    payload = integrity_violation_to_hitl_state(
        violation,
        pd.DataFrame(),
        {"iteration": 1},
        tools_history_slice=[],
        deferred_post_collate=[],
        warnings=[],
        low_confidence_items=[],
    )
    assert payload["hitl_pending_approval"] is True
    assert len(payload["hitl_checkpoints"]) == 1
    assert payload["hitl_checkpoints"][0]["checkpoint_type"] == "checksum_failure"


def test_unpivot_shrink_triggers_violation():
    before = pd.DataFrame([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    after = before.head(1).copy()
    violation = check_post_tool_integrity(
        before,
        after,
        "transform.unpivot",
        {},
        {},
        rows_before=2,
        rows_after=1,
        is_destructive_fn=is_destructive_tool,
        norm_tool="transform.unpivot",
    )
    assert violation is not None
    assert violation["subtype"] == "unpivot_shrink"
