"""Tests for build_template_contract and compute_column_gaps."""

from sia.agent.target_template_utils import (
    build_template_contract,
    compute_column_gaps,
)


def test_build_template_contract_partitions():
    tpl = {
        "title": "Test",
        "x_scope": {
            "variable_type": "Paid Media",
            "uid_hierarchy": ["date", "channel", "publisher"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": ["region"],
            "date_granularity": "daily",
        },
        "business_logic": {
            "column_rules": [
                {
                    "rule_id": "BL_PUBLISHER",
                    "operator": "set_value",
                    "value": "total",
                    "destination_column": "publisher",
                    "condition": {"type": "column_missing_or_null", "column": "publisher"},
                }
            ]
        },
        "aggregation_logic": {"metric_rules": {"impressions": "sum"}},
        "properties": {
            "date": {},
            "channel": {},
            "publisher": {},
            "spends": {},
            "impressions": {},
            "region": {},
        },
    }
    contract = build_template_contract(tpl)
    assert contract["uid_hierarchy"] == ["date", "channel", "publisher"]
    assert contract["metrics"] == ["spends", "impressions"]
    assert contract["supporting_columns"] == ["region"]
    assert "publisher" in contract["rule_satisfied_without_mapping"]
    assert len(contract["column_rules"]) == 1


def test_compute_column_gaps_suggests_publisher_add():
    tpl = {
        "x_scope": {
            "uid_hierarchy": ["date", "publisher"],
            "metrics": ["impressions"],
            "supporting_columns": [],
        },
        "business_logic": {
            "column_rules": [
                {
                    "rule_id": "R1",
                    "operator": "set_value",
                    "value": "total",
                    "destination_column": "publisher",
                    "condition": {"type": "column_missing_or_null", "column": "publisher"},
                }
            ]
        },
        "properties": {"date": {}, "publisher": {}, "impressions": {}},
    }
    gaps = compute_column_gaps(tpl, ["date", "impressions", "CHANNEL"])
    assert "publisher" in gaps["missing_uid"]
    assert len(gaps["suggested_add_columns"]) == 1
    assert gaps["suggested_add_columns"][0]["target_column"] == "publisher"
    assert gaps["suggested_add_columns"][0]["value"] == "total"
