import asyncio

import pandas as pd

from sia.agent.schema_mapper import SchemaMapper, _dataframe_for_column_profile


def test_dataframe_for_column_profile_uses_full_frame_when_small():
    df = pd.DataFrame({"a": range(800)})
    out = _dataframe_for_column_profile(df)
    assert len(out) == 800


def test_dataframe_for_column_profile_head_tail_when_large():
    df = pd.DataFrame({"a": range(5000)})
    out = _dataframe_for_column_profile(df)
    assert len(out) == 1000
    assert out["a"].iloc[0] == 0
    assert out["a"].iloc[499] == 499
    assert out["a"].iloc[500] == 4500
    assert out["a"].iloc[-1] == 4999


class _FakeMappingResponse:
    text = '[{"classification":"Structural Metadata","decision":"Keep","reasoning":"LLM fallback","confidence":0.7}]'


class _CountingLLM:
    def __init__(self):
        self.calls = 0

    async def generate_content_async(self, _prompt):
        self.calls += 1
        return _FakeMappingResponse()


def test_propose_mapping_marks_blank_and_numeric_like_columns():
    mapper = SchemaMapper(llm_client=None, prompts_dir="does-not-exist")
    df = pd.DataFrame(
        {
            "Empty Column": [None, "", "   "],
            "CPM": ["1.5", "2.0", "3.5"],
            "Campaign Name": ["Launch", "Retargeting", "Brand"],
        }
    )

    mapping = {
        item["column_name"]: item
        for item in asyncio.run(mapper.propose_mapping(df))
    }

    assert mapping["Empty Column"]["column_type"] == "Blank"
    assert mapping["Empty Column"]["classification"] == "Blank / Empty"
    assert mapping["Empty Column"]["decision"] == "Discard"
    assert mapping["Empty Column"]["stats"]["kind"] == "blank"

    assert mapping["CPM"]["column_type"] == "Metric"
    assert mapping["CPM"]["stats"]["kind"] == "numeric"
    assert mapping["CPM"]["stats"]["max"] == 3.5


def test_create_mcwt_keeps_metadata_and_falls_back_when_selection_is_empty():
    mapper = SchemaMapper(llm_client=None, prompts_dir="does-not-exist")
    df = pd.DataFrame(
        {
            "Campaign": ["A", "B"],
            "Spend": [10, 20],
        }
    )

    mcwt = mapper.create_mcwt(df, {"Campaign": "Metadata", "Spend": "Keep"})
    assert list(mcwt.columns) == ["Campaign", "Spend"]

    preserved = mapper.create_mcwt(df, {"Campaign": "Discard", "Spend": "Discard"})
    assert list(preserved.columns) == ["Campaign", "Spend"]


def test_suggest_target_column_market_prefers_region_over_channel_partial():
    """'Market' is an explicit region synonym; must not lose to channel's 'marketing_channel' partial."""
    mapper = SchemaMapper(llm_client=None, prompts_dir="does-not-exist")
    target_columns = ["date", "channel", "publisher", "region", "spends", "impressions"]
    r = mapper._suggest_target_column("Market", target_columns)
    assert r["target_column"] == "region"
    assert r["target_match_method"] == "synonyms_json"


def test_suggest_target_column_uses_sample_values_for_geo_mapping():
    mapper = SchemaMapper(llm_client=None, prompts_dir="does-not-exist")
    target_columns = ["date", "channel", "publisher", "market", "spends"]
    r = mapper._suggest_target_column(
        "Geo",
        target_columns,
        meta={"unique_values": ["US", "AU", "ID"], "inferred_type": "Dimension"},
    )
    assert r["target_column"] == "market"
    assert r["target_match_method"] == "value_profile"


def test_suggest_target_column_recognizes_composite_platform_values_as_channel():
    mapper = SchemaMapper(llm_client=None, prompts_dir="does-not-exist")
    target_columns = ["date", "channel", "publisher", "impressions"]
    r = mapper._suggest_target_column(
        "Post",
        target_columns,
        meta={"unique_values": ["IG Reel 1", "TikTok 2", "IG Story 3"], "inferred_type": "Dimension"},
    )
    assert r["target_column"] == "channel"
    assert r["target_match_method"] == "value_profile"


def test_suggest_target_column_numeric_metric_avoids_value_profile_to_spend_targets():
    """Likes/followers are numeric; value samples must not pick spends/impressions."""
    mapper = SchemaMapper(llm_client=None, prompts_dir="does-not-exist")
    target_columns = ["date", "channel", "publisher", "region", "spends", "impressions"]
    r = mapper._suggest_target_column(
        "Performance_Likes",
        target_columns,
        meta={"unique_values": ["7", "100", "5000"], "inferred_type": "Metric"},
    )
    assert r["target_match_method"] != "value_profile"
    assert r["target_column"] != "spends"


def test_propose_mapping_skips_llm_when_all_columns_resolved_by_synonyms():
    llm = _CountingLLM()
    mapper = SchemaMapper(llm_client=llm, prompts_dir="does-not-exist")
    df = pd.DataFrame(
        {
            "date_paid_media": ["2026-01-01"],
            "channel_paid_media": ["TV"],
            "total_cost_paid_media": [10.0],
            "impressions_paid_media": [100],
        }
    )

    rows = asyncio.run(
        mapper.propose_mapping(
            df,
            target_columns=["date", "channel", "spends", "impressions"],
        )
    )

    assert llm.calls == 0
    assert {row["target_column"] for row in rows} == {
        "date",
        "channel",
        "spends",
        "impressions",
    }
    assert all(row["llm_skipped"] for row in rows)
    assert all(row["semantic_status"] == "deterministic" for row in rows)


def test_propose_mapping_calls_llm_when_any_non_blank_column_unresolved():
    llm = _CountingLLM()
    mapper = SchemaMapper(llm_client=llm, prompts_dir="does-not-exist")
    df = pd.DataFrame(
        {
            "date_paid_media": ["2026-01-01"],
            "Unknown Label": ["abc"],
        }
    )

    rows = asyncio.run(
        mapper.propose_mapping(
            df,
            target_columns=["date", "channel", "spends", "impressions"],
        )
    )

    assert llm.calls == 1
    assert len(rows) == 2


def test_infer_column_role_primary_target_wins_over_derived_classification():
    role = SchemaMapper.infer_column_role(
        "Derived",
        "Discard",
        "Dimension",
        target_column="market",
        primary_targets={"date", "channel", "market", "publisher", "spends", "impressions"},
    )
    assert role == "primary"


def test_heuristic_mapping_market_maps_to_primary_not_exclude():
    mapper = SchemaMapper(llm_client=None, prompts_dir="does-not-exist")
    meta = {"unique_values": ["CA", "US"], "inferred_type": "Dimension"}
    rows = mapper._heuristic_mapping(
        pd.DataFrame({"Market": ["CA", "US"]}),
        {"Market": meta},
        target_columns=["date", "channel", "market", "publisher", "spends", "impressions"],
        primary_targets={"date", "channel", "market", "publisher", "spends", "impressions"},
    )
    assert rows[0]["target_column"] == "market"
    assert rows[0]["role"] == "primary"
    assert rows[0]["decision"] == "Keep"
