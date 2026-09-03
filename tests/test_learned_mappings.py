"""Learned mappings: capture on confirmation, apply immediately, cap the queues."""

import pytest

from sia.agent import learned_mappings as lm
from sia.agent.field_mapping_policy import (
    build_value_harmonization_map,
    build_value_token_map,
)
from sia.agent.hierarchy_register import load_catalog
from sia.agent.schema_mapper import (
    _load_column_synonyms_from_config,
    invalidate_column_synonym_cache,
)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Never touch the real config/learned_mappings.json."""
    store = tmp_path / "learned_mappings.json"
    monkeypatch.setattr(lm, "_path", lambda: store)
    monkeypatch.setattr(lm, "_CACHE", None)
    invalidate_column_synonym_cache()
    yield store
    monkeypatch.setattr(lm, "_CACHE", None)
    invalidate_column_synonym_cache()


def test_confirmed_mapping_is_remembered_as_unvalidated(isolated_store):
    stored = lm.record_confirmed_mapping([
        {"source_column": "Media Cost GBP", "target_column": "spends"},
        {"source_column": "Impr.", "target_column": "impressions"},
        {"source_column": "Notes", "target_column": "No match"},
    ])

    assert stored == 2
    assert isolated_store.is_file()
    aliases = lm.learned_column_aliases()
    assert aliases["spends"] == ["Media Cost GBP"]
    assert aliases["impressions"] == ["Impr."]
    # Captured automatically, so it still needs a human to sign it off.
    entry = lm.load_learned()["column_aliases"]["spends"][0]
    assert entry["validated"] is False
    assert entry["count"] == 1


def test_unvalidated_alias_applies_to_the_next_source():
    """Validated is a review marker; both tiers drive mapping."""
    lm.record_column_alias("spends", "Media Cost GBP")

    synonyms = _load_column_synonyms_from_config()

    assert "Media Cost GBP" in synonyms.get("spends", [])


def test_repeated_confirmation_counts_instead_of_duplicating():
    lm.record_column_alias("spends", "Media Cost")
    lm.record_column_alias("spends", "media cost")

    entries = lm.load_learned()["column_aliases"]["spends"]
    assert len(entries) == 1
    assert entries[0]["count"] == 2


def test_alias_matching_its_own_target_is_not_stored():
    assert lm.record_column_alias("spends", "Spends") is False
    assert lm.learned_column_aliases() == {}


def test_open_field_queue_keeps_only_the_latest_unique_values():
    values = [f"CAMP_{i:04d}" for i in range(lm.OPEN_QUEUE_CAP + 25)]
    lm.record_field_values("campaign", values, mode="open")

    queued = lm.load_learned()["field_values"]["campaign"]["values"]
    assert len(queued) == lm.OPEN_QUEUE_CAP
    # The queue is a rolling window, so the newest survive and the oldest fall off.
    kept = {e["value"] for e in queued}
    assert "CAMP_0124" in kept
    assert "CAMP_0000" not in kept


def test_open_field_values_identify_their_column_but_are_not_rewritten():
    lm.record_field_values("campaign", ["DEU_EDP_ALPRO_Q1"], mode="open")

    token_map = build_value_token_map(load_catalog())
    assert token_map.get("deu_edp_alpro_q1") == "campaign"

    # Its standard is itself, so harmonisation leaves the cell alone.
    harmonize = build_value_harmonization_map("campaign", load_catalog())
    assert harmonize["deu_edp_alpro_q1"] == "DEU_EDP_ALPRO_Q1"


def test_closed_field_only_queues_values_config_does_not_already_cover():
    catalog = load_catalog()
    known = build_value_harmonization_map("country", catalog)
    already_mapped = next(iter(known))

    lm.record_field_values("country", [already_mapped, "„Deutschland"], mode="closed")

    queued = [e["value"] for e in lm.load_learned()["field_values"]["country"]["values"]]
    assert queued == ["„Deutschland"]


def test_closed_field_inbox_entry_has_no_standard_until_promoted():
    lm.record_field_values("country", ["U.K.!"], mode="closed")
    entry = lm.load_learned()["field_values"]["country"]["values"][0]
    assert entry["standard"] == ""
    assert entry["validated"] is False

    # Pointing it at a standard value is the promote action.
    assert lm.set_value_standard("country", "U.K.!", "United Kingdom") is True

    harmonize = build_value_harmonization_map("country", load_catalog())
    assert harmonize["u.k.!"] == "United Kingdom"
    entry = lm.load_learned()["field_values"]["country"]["values"][0]
    assert entry["validated"] is True


def test_metric_and_date_fields_are_never_queued():
    assert lm.record_field_values("spends", [100, 200]) == 0
    assert lm.record_field_values("date", ["2026-01-01"]) == 0
    assert lm.load_learned()["field_values"] == {}


def test_validating_and_removing_entries():
    lm.record_column_alias("spends", "Media Cost")
    assert lm.set_alias_validated("spends", "media cost", True) is True
    assert lm.load_learned()["column_aliases"]["spends"][0]["validated"] is True

    assert lm.remove_alias("spends", "Media Cost") is True
    assert "spends" not in lm.load_learned()["column_aliases"]
    assert lm.remove_alias("spends", "Media Cost") is False


def test_validated_entries_survive_the_cap():
    lm.record_field_values("campaign", ["KEEPER"], mode="open")
    lm.set_value_validated("campaign", "KEEPER", True)
    lm.record_field_values(
        "campaign",
        [f"CAMP_{i:04d}" for i in range(lm.OPEN_QUEUE_CAP + 10)],
        mode="open",
    )

    queued = {e["value"] for e in lm.load_learned()["field_values"]["campaign"]["values"]}
    assert "KEEPER" in queued
    assert len(queued) == lm.OPEN_QUEUE_CAP


def test_batch_writes_the_store_once(isolated_store):
    with lm.batch():
        lm.record_column_alias("spends", "Cost A")
        lm.record_column_alias("spends", "Cost B")
        assert not isolated_store.is_file()

    assert isolated_store.is_file()
    assert len(lm.load_learned()["column_aliases"]["spends"]) == 2


def test_summary_counts_unreviewed_work():
    lm.record_column_alias("spends", "Media Cost")
    lm.record_column_alias("clicks", "Link Clicks", validated=True)
    lm.record_field_values("campaign", ["CAMP_1", "CAMP_2"], mode="open")

    counts = lm.learned_summary()["counts"]
    assert counts["aliases"] == 2
    assert counts["aliases_unvalidated"] == 1
    assert counts["values"] == 2
    assert counts["values_unvalidated"] == 2
