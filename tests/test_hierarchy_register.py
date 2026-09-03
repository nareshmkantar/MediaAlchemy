"""Tests for media hierarchy registration (Upload gate)."""
from __future__ import annotations

import pandas as pd
import pytest

from sia.agent.hierarchy_register import (
    catalog_for_api,
    compare_hierarchies,
    detect_combined_fields,
    detect_publisher,
    enterprise_info_defaults,
    infer_grain,
    load_catalog,
    mapping_attribute_targets,
    media_hierarchy_levels,
    normalize_enterprise_info,
    normalize_registration_payload,
    propose_for_source,
    propose_for_job,
    registration_is_complete,
    sample_preview,
    suggest_column_target,
)
from sia.agent.job_manager import JobManager


@pytest.fixture()
def catalog():
    return load_catalog(force_reload=True)


def _enterprise_ok():
    return normalize_enterprise_info({
        "values": {"market": "UK", "category": "Dairy", "brand": "Alpro"},
        "enabled_optional_ids": [],
        "advertiser": {"value": "Danone", "source_column": ""},
    })


def test_catalog_loads_publishers(catalog):
    assert "dv360" in catalog["publishers"]
    assert "facebook" in catalog["publishers"]
    api = catalog_for_api()
    assert any(p["id"] == "google_ads" for p in api["publishers"])
    assert any(f["id"] == "campaign" for f in api["standard_fields"])
    assert api.get("media_hierarchy")
    assert api.get("enterprise_info")
    assert api.get("common_attributes")


def test_detect_publisher_from_filename(catalog):
    hit = detect_publisher("Facebook_Ads_Q3.csv", catalog=catalog)
    assert hit["publisher_id"] == "facebook"
    assert hit["confidence"] >= 70

    unknown = detect_publisher("mystery_extract.xlsx", catalog=catalog)
    assert unknown["publisher_id"] is None
    assert "Unknown" in (unknown["reason"] or "")


def test_detect_publisher_never_defaults_to_dv360(catalog):
    hit = detect_publisher("weekly_report.xlsx", columns=["Date", "Spend"], catalog=catalog)
    assert hit["publisher_id"] != "dv360" or hit["confidence"] == 0
    if hit["publisher_id"] is None:
        assert hit["confidence"] == 0


def test_suggest_column_target_and_infer_grain(catalog):
    assert suggest_column_target("Ad set name", catalog)["target"] == "ad_group"
    # Legacy IO / Order names map onto Campaign on the shared spine
    assert suggest_column_target("IO Name", catalog)["target"] == "campaign"
    assert suggest_column_target("Order", catalog)["target"] == "campaign"
    assert suggest_column_target("Device Type", catalog)["target"] == "device"
    assert suggest_column_target("Budget", catalog)["target"] == "spends"
    assert suggest_column_target("Cost", catalog)["target"] == "spends"
    assert suggest_column_target("Spend", catalog)["target"] == "spends"

    grain = infer_grain(
        "facebook",
        ["Date", "Account name", "Campaign name", "Ad set name", "Ad name", "Amount spent"],
        catalog,
    )
    assert grain["grain_level_id"] in ("ad_group", "ad", "creative")
    assert grain["grain_level_name"] in ("Ad Group", "Ad", "Creative")
    assert grain["detected_hierarchy_columns"]


def test_column_matches_an_alias_only_as_a_whole_name(catalog):
    """Substring matching read qualifiers as their entity; whole names only now."""
    # "order" no longer leaks into every order* header.
    assert suggest_column_target("orderCurrency", catalog)["target"] is None
    assert suggest_column_target("orderBudget", catalog)["target"] is None
    assert suggest_column_target("orderStartDate", catalog)["target"] is None
    # "site" (a Publisher alias) no longer hides inside "onsite".
    assert suggest_column_target("actions.onsite_conversion.post_save.7d_click", catalog)["target"] is None
    # camelCase and snake_case are still the same name as the spaced alias.
    assert suggest_column_target("orderName", catalog)["target"] == "campaign"
    assert suggest_column_target("order_name", catalog)["target"] == "campaign"
    assert suggest_column_target("adset_name", catalog)["target"] == "ad_group"


def test_grain_ignores_qualifier_columns(catalog):
    """The Amazon file that read as Campaign off orderCurrency and friends."""
    grain = infer_grain(
        columns=[
            "orderName", "orderId", "orderCurrency", "orderBudget", "orderStartDate", "totalCost",
        ],
        catalog=catalog,
    )
    assert grain["grain_level_id"] == "campaign"
    # orderId is an identifier, not the Campaign name column, so it is not evidence.
    evidence = {row["source_column"] for row in grain["detected_hierarchy_columns"]}
    assert evidence == {"orderName"}


def test_id_columns_are_not_grain_evidence(catalog):
    """An id column matches no alias, so it makes no claim about grain.

    DV360 ships Line Item ID but no Line Item name; without an alias for it the
    file falls back to Publisher and the analyst picks from the preview.
    """
    assert suggest_column_target("orderId", catalog)["target"] is None
    assert suggest_column_target("Line Item ID", catalog)["target"] is None

    grain = infer_grain(
        columns=["Date", "Insertion Order ID", "Line Item ID", "Impressions"],
        catalog=catalog,
    )
    assert grain["grain_level_id"] == "publisher"
    assert grain["detected_hierarchy_columns"] == []


def test_shared_media_hierarchy_spine(catalog):
    levels = media_hierarchy_levels(catalog)
    ids = [lv["id"] for lv in levels]
    assert ids == ["publisher", "campaign", "ad_group", "ad", "creative"]
    # Publishers no longer carry deep platform trees
    pub = catalog["publishers"]["amazon_dsp"]
    assert not pub.get("hierarchy")

    grain = infer_grain(
        "amazon_dsp",
        ["Date", "Advertiser", "Order", "Line Item", "Media Cost"],
        catalog,
    )
    # Order → campaign, Line Item → ad_group
    assert grain["grain_level_id"] == "ad_group"
    assert grain["grain_level_name"] == "Ad Group"


def test_infer_grain_uses_columns_not_publisher(catalog):
    """Grain is the deepest Config spine level in the headers, even with no publisher."""
    grain = infer_grain(
        None,
        ["Date", "Campaign name", "Ad set name", "Spend"],
        catalog,
    )
    assert grain["grain_level_id"] == "ad_group"
    found = {row["target"] for row in grain["detected_hierarchy_columns"]}
    assert "campaign" in found
    assert "ad_group" in found


def test_infer_grain_defaults_to_publisher_when_no_lower_level_columns(catalog):
    grain = infer_grain("amazon_dsp", ["Date", "Spend", "Impressions"], catalog)
    assert grain["grain_level_id"] == "publisher"
    assert grain["grain_level_name"] == "Publisher"
    assert grain["detected_hierarchy_columns"] == []
    assert "defaulting to Publisher" in grain["reason"]


def test_facebook_conversion_headers_do_not_count_as_publisher_grain(catalog):
    """site inside onsite must not become a Publisher hierarchy hit."""
    grain = infer_grain(
        columns=[
            "Date",
            "spend",
            "impressions",
            "actions.onsite_conversion.post_save.7d_click",
            "actions.onsite_conversion.post_net_like",
        ],
        catalog=catalog,
    )
    assert grain["grain_level_id"] == "publisher"
    assert grain["detected_hierarchy_columns"] == []


def test_propose_for_source_includes_preview(catalog, tmp_path):
    path = tmp_path / "facebook_ads.csv"
    path.write_text(
        "Date,Campaign name,Ad set name,Spend\n"
        "2024-01-01,Spring,Retarget,10\n"
        "2024-01-02,Spring,Prospect,12\n",
        encoding="utf-8",
    )
    proposal = propose_for_source(
        {"source_id": "s1", "file_path": str(path), "file_name": "facebook_ads.csv"},
        catalog=catalog,
    )
    preview = proposal["preview"]
    assert "Campaign name" in preview["headers"]
    assert preview["column_roles"]["Campaign name"] == "campaign"
    assert preview["column_roles"]["Ad set name"] == "ad_group"
    assert preview["rows"][0][preview["headers"].index("Campaign name")] == "Spring"
    assert proposal["grain_level_id"] == "ad_group"


def test_sample_preview_keeps_hierarchy_columns_when_wide():
    headers = [f"Col_{i}" for i in range(30)] + ["Campaign name"]
    df = pd.DataFrame({h: [f"{h}-v"] for h in headers})
    preview = sample_preview(
        headers,
        df,
        [{"source_column": "Campaign name", "target": "campaign"}],
    )
    assert "Campaign name" in preview["headers"]
    assert preview["truncated"] is True
    assert preview["column_roles"]["Campaign name"] == "campaign"


def test_enterprise_info_defaults(catalog):
    defaults = enterprise_info_defaults(catalog)
    mand_ids = [f["id"] for f in defaults["fields"] if f.get("mandatory")]
    assert mand_ids == ["market", "category", "brand"]
    assert defaults["values"].get("market") == "Germany"
    assert defaults["values"].get("category") == "Dairy"
    assert defaults["values"].get("brand") == "Alpro"
    opt_ids = [f["id"] for f in defaults["fields"] if not f.get("mandatory")]
    assert "region" in opt_ids
    assert "sub_brand" in opt_ids
    assert "advertiser" in opt_ids
    assert "business_unit" in opt_ids
    assert "advertiser" in defaults["enabled_optional_ids"]
    assert "business_unit" in defaults["enabled_optional_ids"]
    api = catalog_for_api()
    assert api.get("enterprise_info_defaults")
    attrs = mapping_attribute_targets(catalog)
    assert "campaign" in attrs
    assert "device" in attrs
    assert "market" in attrs
    assert "advertiser" in attrs
    assert "business_unit" in attrs
    # Alphabetical by display label (Advertiser before Campaign, Audience before Brand, …)
    assert attrs.index("advertiser") < attrs.index("campaign")
    assert attrs.index("audience") < attrs.index("brand")
    assert attrs.index("ad") < attrs.index("ad_group")
    from sia.agent.hierarchy_register import mapping_metric_targets, mapping_target_option_catalog
    metrics = mapping_metric_targets(catalog)
    metric_ids = {m["id"] for m in metrics}
    assert "spends" in metric_ids
    assert "impressions" in metric_ids
    assert "clicks" in metric_ids
    assert "roas" not in metric_ids
    opts = mapping_target_option_catalog(catalog)
    assert any(o["id"] == "campaign_kpi" and o["kind"] == "dimension" for o in opts)
    assert any(o["id"] == "spends" and o.get("supports_currency") for o in opts)


def test_detect_combined_fields_packed_campaign(catalog, tmp_path):
    df = pd.DataFrame({
        "Date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
        "Campaign Name": [
            "US_Alpro_Awareness",
            "UK_Alpro_Consideration",
            "DE_Activia_Conversion",
            "FR_Alpro_Awareness",
        ],
        "Spend": [10, 20, 30, 40],
    })
    found = detect_combined_fields(df, catalog=catalog)
    cols = {f["source_column"] for f in found}
    assert "Campaign Name" in cols
    packed = next(f for f in found if f["source_column"] == "Campaign Name")
    assert packed["delimiter"] == "_"
    assert len([t for t in packed["target_dimensions"] if t]) >= 2
    assert packed.get("part_count", 0) >= 2
    assert len(packed.get("parts") or []) >= 2
    assert len(packed.get("part_samples") or []) >= 2


def test_detect_combined_fields_leaves_unrecognised_parts_unassigned(catalog):
    """A repeated or unknown token must not borrow a dimension it has no evidence for."""
    df = pd.DataFrame({
        "Campaign Name": [
            "DEU_EDP_ALPRO_ALPRO_Amazon002026_Conver_ROAS",
            "DEU_EDP_ALPRO_ALPRO_Amazon002026_Conver_ROAS",
            "GBR_EDP_ACTIVIA_ACTIVIA_Amazon002026_Conver_CPC",
        ],
        "Spend": [10, 20, 30],
    })
    packed = next(
        f for f in detect_combined_fields(df, catalog=catalog)
        if f["source_column"] == "Campaign Name"
    )
    targets = packed["target_dimensions"]

    assert targets[0] == "country"
    assert targets[2] == "brand"
    # Duplicate brand token and the opaque id are not leftover-filled.
    assert targets[3] in (None, "")
    assert targets[4] in (None, "")
    assert targets[3] != "region"
    assert packed["parts"][3]["target"] in (None, "")
    # A later part with a real alias keeps its own dimension.
    assert targets[5] == "campaign_objective"
    assert targets[6] == "campaign_kpi"


def test_mixed_part_does_not_take_leftover_region_from_one_row(catalog):
    """Part 4 is ALPRO / EDP / ALL — ALL is a Region alias, but that is not the slot's best match."""
    df = pd.DataFrame({
        "orderName": [
            "DEU_EDP_ALPRO_ALPRO_Amazon002026_Conver_ROAS_NA_YES_BonusQ2",
            "DEU_EDP_ALPRO_EDP_MTG-022026_Consid_CPC_NA_YES_Display",
            "DEU_EDP_ALPRO_EDP_MTG-022026_Aware_CTR_NA_YES_FireTVBonusQ2",
            "DEU_EDP_ALPRO_ALL_AlmondPBAY-012026_Consid_CPC_NA_YES_Display",
        ],
    })
    packed = next(
        f for f in detect_combined_fields(df, catalog=catalog)
        if f["source_column"] == "orderName"
    )
    targets = packed["target_dimensions"]
    assert targets[0] == "country"
    assert targets[1] == "advertiser"
    assert targets[2] == "brand"
    assert targets[3] != "region"
    assert not targets[3]


def test_compare_hierarchies_mixed_grain(catalog):
    regs = [
        {
            "source_id": "a",
            "file_name": "Facebook_Ads.csv",
            "publisher_id": "facebook",
            "publisher_name": "Facebook",
            "grain_level": 3,
            "grain_level_id": "ad_group",
            "grain_level_name": "Ad Group",
            "grain_level_index": 2,
            "detected_hierarchy_columns": [{"target": "ad_group"}],
        },
        {
            "source_id": "b",
            "file_name": "DV360_UK.xlsx",
            "publisher_id": "dv360",
            "publisher_name": "DV360",
            "grain_level": 2,
            "grain_level_id": "campaign",
            "grain_level_name": "Campaign",
            "grain_level_index": 1,
            "detected_hierarchy_columns": [{"target": "campaign"}],
        },
    ]
    cmp = compare_hierarchies(regs, template_uid_hierarchy=["date", "channel", "publisher"], catalog=catalog)
    assert cmp["mixed_grain"] is True
    assert cmp["requires_mixed_grain_ack"] is True
    assert any(w["code"] == "mixed_grain" for w in cmp["warnings"])


def test_registration_complete_gating(catalog):
    sources = [
        {"source_id": "s1", "contains_main_data": True},
        {"source_id": "s2", "contains_main_data": True},
    ]
    incomplete, reasons = registration_is_complete(
        [], sources, catalog=catalog, enterprise_info=_enterprise_ok(),
    )
    assert incomplete is False
    assert reasons

    registry = [
        {
            "source_id": "s1",
            "publisher_id": "facebook",
            "grain_level_id": "ad_group",
            "grain_level": 3,
            "grain_level_name": "Ad Group",
        },
        {
            "source_id": "s2",
            "publisher_id": "dv360",
            "grain_level_id": "campaign",
            "grain_level": 2,
            "grain_level_name": "Campaign",
        },
    ]
    ok_no_ack, reasons2 = registration_is_complete(
        registry, sources, mixed_grain_acknowledged=False, catalog=catalog, enterprise_info=_enterprise_ok(),
    )
    assert ok_no_ack is False
    assert any("Mixed" in r for r in reasons2)

    ok_no_ent, reasons_ent = registration_is_complete(
        registry, sources, mixed_grain_acknowledged=True, catalog=catalog, enterprise_info={},
    )
    assert ok_no_ent is False
    assert any("Market" in r or "market" in r.lower() for r in reasons_ent)

    ok, reasons3 = registration_is_complete(
        registry, sources, mixed_grain_acknowledged=True, catalog=catalog, enterprise_info=_enterprise_ok(),
    )
    assert ok is True
    assert reasons3 == []


def test_job_manager_hierarchy_stage0(tmp_path):
    path = tmp_path / "a.xlsx"
    pd.DataFrame({"Date": [1], "Campaign": ["x"], "Spend": [1]}).to_excel(path, index=False)
    m = JobManager()
    m.create_job("hjob", "Facebook_Ads.xlsx", str(path), sheets=["Sheet1"])
    job = m.get_job("hjob")
    assert job.get("hierarchy_register_complete") is False
    uxs = m.build_ux_stepper_summary(job)
    assert uxs["stage0_complete"] is False
    assert uxs["hierarchy_register_complete"] is False

    sid = m.get_source_id("hjob", "Sheet1")
    saved = m.save_hierarchy_registry(
        "hjob",
        [{
            "source_id": sid,
            "publisher_id": "facebook",
            "publisher_name": "Facebook",
            "grain_level": 2,
            "grain_level_id": "campaign",
            "grain_level_name": "Campaign",
            "registered": True,
            "combined_fields": [],
        }],
        mixed_grain_acknowledged=False,
        mark_complete=True,
        enterprise_info=_enterprise_ok(),
    )
    assert saved["hierarchy_register_complete"] is True
    assert saved.get("enterprise_info", {}).get("values", {}).get("market") == "UK"
    uxs2 = m.build_ux_stepper_summary(m.get_job("hjob"))
    assert uxs2["stage0_complete"] is True
    assert uxs2["hierarchy_register_complete"] is True


def test_propose_for_job_reads_sample(tmp_path):
    path = tmp_path / "DV360_UK.xlsx"
    pd.DataFrame({
        "Date": ["2024-01-01", "2024-01-02"],
        "Advertiser Name": ["Adv", "Adv"],
        "Campaign": ["C1", "C2"],
        "IO Name": ["IO1", "IO2"],
        "Line Item": ["LI1", "LI2"],
        "Total Cost": [10, 20],
        "Impressions": [100, 200],
    }).to_excel(path, index=False)

    m = JobManager()
    m.create_job("pj", "DV360_UK.xlsx", str(path), sheets=["Sheet1"])
    job = m.get_job("pj")
    proposal = propose_for_job(job, template_uid_hierarchy=["date", "channel", "publisher"])
    assert proposal["kpis"]["sources"] == 1
    assert proposal.get("enterprise_info")
    assert proposal.get("media_hierarchy")
    src = proposal["sources"][0]
    assert src["publisher_id"] == "dv360"
    assert src["grain_level_id"] in ("ad_group", "campaign", "ad", "creative", "publisher")


def test_normalize_registration_payload_and_api_shape(tmp_path):
    path = tmp_path / "f.xlsx"
    pd.DataFrame({"A": [1]}).to_excel(path, index=False)
    m = JobManager()
    m.create_job("nj", "f.xlsx", str(path), sheets=["Sheet1"])
    job = m.get_job("nj")
    sid = m.get_source_id("nj", "Sheet1")
    rows = normalize_registration_payload(
        [{
            "source_id": sid,
            "publisher_id": "tiktok",
            "grain_level_id": "ad_group",
            "combined_fields": [{
                "source_column": "Campaign Name",
                "delimiter": "_",
                "target_dimensions": ["country", "brand", "campaign"],
                "samples": ["US_Alpro_Awareness"],
                "confidence": 80,
                "accepted": True,
            }],
        }],
        job,
    )
    assert len(rows) == 1
    assert rows[0]["publisher_id"] == "tiktok"
    assert rows[0]["grain_level_name"] == "Ad Group"
    assert "combined_fields" not in rows[0]  # splits moved to column standardize step
    assert rows[0]["registered"] is True


def test_stage1_requires_hierarchy_complete(tmp_path):
    """Existing UX tests assumed stage0 = files only; hierarchy now gates stage0/1."""
    path = tmp_path / "a.xlsx"
    pd.DataFrame({"x": [1]}).to_excel(path, index=False)
    m = JobManager()
    m.create_job("ux_h", "a.xlsx", str(path), sheets=["S1"])
    sid = m.get_source_id("ux_h", "S1")
    m.mark_ux_source_layout("ux_h", sid, True)
    m.mark_ux_source_mapping("ux_h", sid, True)
    assert m.build_ux_stepper_summary(m.get_job("ux_h"))["stage1_complete"] is False

    m.save_hierarchy_registry(
        "ux_h",
        [{
            "source_id": sid,
            "publisher_id": "google_ads",
            "publisher_name": "Google Ads",
            "grain_level_id": "campaign",
            "grain_level": 2,
            "grain_level_name": "Campaign",
            "registered": True,
        }],
        mark_complete=True,
        enterprise_info=_enterprise_ok(),
    )
    assert m.build_ux_stepper_summary(m.get_job("ux_h"))["stage1_complete"] is True
