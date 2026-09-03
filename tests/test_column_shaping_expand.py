"""Tests for Column shaping → mapping expand and Primary/Supporting aliases."""

import pandas as pd

from sia.agent.column_shaping_expand import (
    apply_shaping_targets_and_aliases,
    materialize_column_shaping,
    output_alias_for_role,
    source_suffix_index,
)
from sia.agent.target_template_utils import build_rename_mapping_from_approved_mappings


def test_materialize_split_shows_part_names_not_packed():
    df = pd.DataFrame({
        "orderName": ["DEU_EDP_ALPRO_CAMP1", "DEU_EDP_ALPRO_CAMP2"],
        "date": ["2024-01-01", "2024-01-08"],
    })
    splits = [{
        "source_column": "orderName",
        "delimiter": "_",
        "accepted": True,
        "single_dimension": False,
        "parts": [
            {"index": 0, "sample": "DEU", "target": "country"},
            {"index": 1, "sample": "EDP", "target": "brand"},
            {"index": 2, "sample": "ALPRO", "target": "campaign"},
            {"index": 3, "sample": "CAMP1", "target": "campaign_objective", "custom_name": ""},
        ],
        "output_columns": [
            {"part_indexes": [0], "target": "country", "custom_name": ""},
            {"part_indexes": [1], "target": "brand", "custom_name": ""},
            {"part_indexes": [2], "target": "campaign", "custom_name": ""},
            {"part_indexes": [3], "target": "campaign_objective", "custom_name": ""},
        ],
    }]
    labels = {
        "country": "Country",
        "brand": "Brand",
        "campaign": "Campaign",
        "campaign_objective": "Campaign Objective",
    }
    out, meta = materialize_column_shaping(df, splits, label_by_id=labels)
    assert "orderName" not in out.columns
    assert "Country" in out.columns
    assert "Brand" in out.columns
    assert "Campaign Objective" in out.columns
    assert list(out["Country"]) == ["DEU", "DEU"]
    assert "date" in out.columns
    assert len(meta["part_rows"]) == 4
    assert meta["part_rows"][0]["target"] == "country"
    assert meta["packed_dropped"] == ["orderName"]


def test_apply_shaping_targets_override_heuristics():
    mappings = [
        {
            "column_name": "Country",
            "target_column": "channel",  # wrong heuristic — shaping must win
            "role": "exclude",
            "decision": "Discard",
        },
    ]
    meta = {
        "part_rows": [
            {"column_name": "Country", "packed_source": "orderName", "target": "country", "part_indexes": [0]},
        ],
        "single_targets": {},
        "packed_dropped": ["orderName"],
    }
    apply_shaping_targets_and_aliases(
        mappings,
        meta,
        source_index=1,
        primary_targets={"country"},
    )
    assert mappings[0]["target_column"] == "country"
    assert mappings[0]["target_match_method"] == "column_shaping"
    assert mappings[0]["split_from"] == "orderName"
    assert mappings[0]["role"] == "primary"
    assert mappings[0]["output_alias"] == "country"

    mappings[0]["role"] = "supporting"
    apply_shaping_targets_and_aliases(mappings, meta, source_index=2, primary_targets={"country"})
    assert mappings[0]["role"] == "supporting"
    assert mappings[0]["output_alias"] == "country_2"


def test_mapping_rows_stale_vs_shaping():
    from sia.agent.column_shaping_expand import mapping_rows_stale_vs_shaping

    splits = [{
        "source_column": "orderName",
        "accepted": True,
        "single_dimension": False,
        "output_columns": [{"part_indexes": [0], "target": "country"}],
    }]
    stale = [{"column_name": "orderName", "target_column": "No match"}]
    fresh = [{"column_name": "Country", "target_column": "country", "split_from": "orderName"}]
    assert mapping_rows_stale_vs_shaping(stale, splits) is True
    assert mapping_rows_stale_vs_shaping(fresh, splits) is False


def test_output_alias_helpers():
    assert output_alias_for_role("Campaign", "primary", 1) == "Campaign"
    assert output_alias_for_role("Campaign", "supporting", 1) == "Campaign_1"
    assert output_alias_for_role("Campaign", "supporting", 3) == "Campaign_3"
    assert output_alias_for_role("No match", "primary", 1) == ""


def test_rename_prefers_output_alias():
    rows = [
        {
            "source_column": "campaign_part",
            "target_column": "campaign",
            "output_alias": "campaign_2",
            "decision": "Metadata",
            "role": "supporting",
        },
        {
            "source_column": "camp_a",
            "target_column": "campaign",
            "output_alias": "campaign",
            "decision": "Keep",
            "role": "primary",
        },
    ]
    rename = build_rename_mapping_from_approved_mappings(rows)
    assert rename["campaign_part"] == "campaign_2"
    assert rename["camp_a"] == "campaign"


def test_source_suffix_index():
    job = {
        "source_registry": [
            {"source_id": "a"},
            {"source_id": "b"},
        ]
    }
    assert source_suffix_index(job, "a") == 1
    assert source_suffix_index(job, "b") == 2
    assert source_suffix_index(job, "missing") == 1
