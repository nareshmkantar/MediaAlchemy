from sia.agent.field_mapping_policy import build_value_token_map, field_value_mode
from sia.agent.hierarchy_register import load_catalog


def test_field_value_modes_separate_closed_open_and_non_value_fields():
    assert field_value_mode("country", field_type="dimension") == "closed"
    assert field_value_mode("campaign", field_type="hierarchy") == "open"
    assert field_value_mode("date", field_type="date") == "none"
    assert field_value_mode("spends", field_type="metric") == "none"


def test_country_aliases_identify_country_instead_of_market():
    token_map = build_value_token_map(load_catalog(force_reload=True))

    assert token_map["uk"] == "country"
    assert token_map["usa"] == "country"
    assert token_map["germany"] == "country"


def test_starter_finite_lists_are_available_without_manual_setup():
    token_map = build_value_token_map(load_catalog(force_reload=True))

    assert token_map["conver"] == "campaign_objective"
    assert token_map["retargeting"] == "campaign_type"
    assert token_map["connected tv"] == "device"
    assert token_map["roas"] == "campaign_kpi"
    assert token_map["ctr"] == "campaign_kpi"
    assert token_map["cpi"] == "campaign_kpi"
