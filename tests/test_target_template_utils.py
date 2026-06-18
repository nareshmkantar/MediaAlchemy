import pandas as pd

from sia.agent.target_template_utils import (
    apply_template_column_rules,
    collation_merge_column_key_order,
    column_rule_mapping_exceptions,
    mapping_target_columns_for_ui,
    mapping_targets_requiring_source,
    normalize_target_template,
    pre_transform_column_order_for_ui,
    pre_transform_target_columns,
    validate_template_shape,
)


def test_validate_and_pre_transform_columns():
    tpl = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel"],
            "metrics": ["spends"],
            "supporting_columns": ["region"],
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {},
        "properties": {
            "date": {"type": "string"},
            "channel": {"type": "string"},
            "spends": {"type": "number"},
            "region": {"type": "string"},
        },
    }
    ok, err = validate_template_shape(tpl)
    assert ok, err
    assert pre_transform_target_columns(tpl) == ["date", "channel", "spends", "region"]
    assert pre_transform_column_order_for_ui(tpl) == ["date", "channel", "region", "spends"]
    assert mapping_target_columns_for_ui(tpl) == ["date", "channel", "spends"]


def test_collation_merge_column_key_order_dates_uid_supporting_then_metrics():
    tpl = {
        "x_scope": {
            "uid_hierarchy": ["channel", "date", "publisher"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": ["flag"],
        },
        "properties": {
            "channel": {},
            "date": {"format": "date"},
            "publisher": {},
            "spends": {},
            "impressions": {},
            "flag": {},
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {},
    }
    ok, err = validate_template_shape(tpl)
    assert ok, err
    assert collation_merge_column_key_order(tpl) == [
        "date",
        "channel",
        "publisher",
        "flag",
        "spends",
        "impressions",
    ]


def test_validate_accepts_mismatched_sets_when_remaining_properties_can_be_inferred_as_supporting():
    tpl = {
        "x_scope": {
            "uid_hierarchy": ["date"],
            "metrics": ["spends"],
            "supporting_columns": [],
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {},
        "properties": {"date": {}, "spends": {}, "extra": {}},
    }
    ok, err = validate_template_shape(tpl)
    assert ok, err
    assert normalize_target_template(tpl)["x_scope"]["supporting_columns"] == ["extra"]


def test_normalize_target_template_accepts_user_uploaded_shape_without_supporting_columns():
    tpl = {
        "x_scope": {
            "variable_type": "Paid Media Impressions",
            "modelling_start_date": "2023-04-01",
            "modelling_end_date": "2025-03-31",
            "uid_hierarchy": ["date", "channel", "publisher"],
            "metrics": ["spends", "impressions"],
            "date_granularity": "weekly",
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {"metric_rules": {"impressions": "sum", "spends": "sum"}},
        "properties": {
            "date": {"type": "string", "format": "date"},
            "channel": {"type": "string"},
            "publisher": {"type": "string"},
            "region": {"type": "string"},
            "spends": {"type": "number"},
            "impressions": {"type": "integer"},
        },
    }
    normalized = normalize_target_template(tpl)
    ok, err = validate_template_shape(normalized)
    assert ok, err
    assert normalized["x_scope"]["modeling_period"]["start_date"] == "2023-04-01"
    assert normalized["x_scope"]["modeling_period"]["end_date"] == "2025-03-31"
    assert normalized["x_scope"]["supporting_columns"] == ["region"]
    assert pre_transform_target_columns(normalized) == [
        "date", "channel", "publisher", "spends", "impressions", "region"
    ]
    assert mapping_target_columns_for_ui(normalized) == [
        "date", "channel", "publisher", "spends", "impressions"
    ]


def test_normalize_target_template_accepts_legacy_uid_and_nested_aggregation_logic():
    tpl = {
        "x_scope": {
            "uid": ["date", "market", "brand", "channel", "publisher_name"],
            "aggregation_logic": {
                "metric_rules": {
                    "impressions": "sum",
                    "total_cost": "sum",
                    "clicks": "sum",
                }
            },
        },
        "properties": {
            "date": {},
            "market": {},
            "brand": {},
            "channel": {},
            "publisher_name": {},
            "impressions": {},
            "total_cost": {},
            "clicks": {},
            "campaign_type": {},
        },
    }
    normalized = normalize_target_template(tpl)
    ok, err = validate_template_shape(normalized)
    assert ok, err
    assert normalized["x_scope"]["uid_hierarchy"] == [
        "date", "market", "brand", "channel", "publisher_name"
    ]
    assert normalized["x_scope"]["metrics"] == ["impressions", "total_cost", "clicks"]
    assert normalized["x_scope"]["supporting_columns"] == ["campaign_type"]
    assert normalized["aggregation_logic"]["metric_rules"]["total_cost"] == "sum"


def test_publisher_rule_excluded_from_mapping_requirements():
    tpl = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel", "publisher"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": ["region"],
        },
        "business_logic": {
            "column_rules": [
                {
                    "rule_id": "BL_PUBLISHER_DEFAULT",
                    "operator": "set_value",
                    "value": "total",
                    "destination_column": "publisher",
                    "condition": {"type": "column_missing_or_null", "column": "publisher"},
                }
            ]
        },
        "aggregation_logic": {},
        "properties": {
            "date": {},
            "channel": {},
            "publisher": {},
            "region": {},
            "spends": {},
            "impressions": {},
        },
    }
    assert "publisher" in column_rule_mapping_exceptions(tpl)
    need = mapping_targets_requiring_source(tpl)
    assert "publisher" not in need
    assert "date" in need


def test_apply_template_column_rules_creates_missing_column():
    tpl = {
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
        }
    }
    df = pd.DataFrame({"date": ["2026-01-01"]})
    out, actions = apply_template_column_rules(df, tpl)
    assert "publisher" in out.columns
    assert out["publisher"].tolist() == ["total"]
    assert any("created column" in a for a in actions)
