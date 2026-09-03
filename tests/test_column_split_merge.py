"""Re-detection keeps analyst choices and refreshes everything else."""

from web_server import _merge_column_split


def proposal(dims, samples):
    return {
        "source_column": "Campaign Name",
        "part_count": len(samples),
        "part_samples": list(samples),
        "target_dimensions": list(dims),
    }


def saved_part(index, target, sample, user_set):
    return {
        "index": index,
        "sample": sample,
        "target": target,
        "custom_name": "",
        "combine_with_previous": False,
        "user_set": user_set,
    }


def test_autosaved_guess_gives_way_to_fresh_detection():
    """The bug behind ALPRO showing as Region: a stale guess outliving a fix."""
    fresh = proposal(
        ["country", "advertiser", "brand", "", "", "campaign_objective"],
        ["DEU", "EDP", "ALPRO", "ALPRO", "Amazon002026", "Conver"],
    )
    stale = {
        "source_column": "Campaign Name",
        "parts": [
            saved_part(0, "country", "DEU", False),
            saved_part(1, "advertiser", "EDP", False),
            saved_part(2, "brand", "ALPRO", False),
            saved_part(3, "region", "ALPRO", False),
            saved_part(4, "campaign_type", "Amazon002026", False),
            saved_part(5, "campaign_objective", "Conver", False),
        ],
        "target_dimensions": ["country", "advertiser", "brand", "region", "campaign_type", "campaign_objective"],
    }

    merged = _merge_column_split(fresh, stale)

    assert merged["target_dimensions"] == [
        "country", "advertiser", "brand", "", "", "campaign_objective",
    ]
    assert all(part["user_set"] is False for part in merged["parts"])


def test_analyst_choice_survives_re_detection():
    fresh = proposal(["country", "", "brand"], ["DEU", "Amazon002026", "ALPRO"])
    stale = {
        "source_column": "Campaign Name",
        "parts": [
            saved_part(0, "country", "DEU", False),
            saved_part(1, "inventory", "Amazon002026", True),
            saved_part(2, "region", "ALPRO", False),
        ],
    }

    merged = _merge_column_split(fresh, stale)

    # Chosen by a human, so it stays; guessed by the detector, so it refreshes.
    assert merged["target_dimensions"] == ["country", "inventory", "brand"]
    assert merged["parts"][1]["user_set"] is True


def test_choice_is_dropped_when_the_split_shifts():
    """A different sample at that position means the old choice described something else."""
    fresh = proposal(["country", "brand"], ["DEU", "ACTIVIA"])
    stale = {
        "source_column": "Campaign Name",
        "parts": [
            saved_part(0, "country", "DEU", False),
            saved_part(1, "inventory", "Amazon002026", True),
        ],
    }

    merged = _merge_column_split(fresh, stale)

    assert merged["target_dimensions"] == ["country", "brand"]
    assert merged["parts"][1]["user_set"] is False


def test_split_level_decisions_are_preserved():
    fresh = proposal(["country"], ["DEU"])
    stale = {
        "source_column": "Campaign Name",
        "accepted": False,
        "single_dimension": True,
        "single_target": "campaign",
        "single_custom_name": "",
        "parts": [saved_part(0, "country", "DEU", False)],
    }

    merged = _merge_column_split(fresh, stale)

    assert merged["single_dimension"] is True
    assert merged["single_target"] == "campaign"
    assert merged["accepted"] is False


def test_combined_parts_report_the_dimension_they_folded_into():
    fresh = proposal(["country", "brand", "campaign_objective"], ["DEU", "ALPRO", "Conver"])
    stale = {
        "source_column": "Campaign Name",
        "parts": [
            saved_part(0, "country", "DEU", False),
            {**saved_part(1, "brand", "ALPRO", True), "combine_with_previous": True},
            saved_part(2, "campaign_objective", "Conver", False),
        ],
    }

    merged = _merge_column_split(fresh, stale)

    assert merged["target_dimensions"] == ["country", "country", "campaign_objective"]


def test_growing_part_count_fills_from_detection():
    fresh = proposal(["country", "brand", "campaign_kpi"], ["DEU", "ALPRO", "ROAS"])
    stale = {
        "source_column": "Campaign Name",
        "parts": [saved_part(0, "market", "DEU", True)],
    }

    merged = _merge_column_split(fresh, stale)

    assert len(merged["parts"]) == 3
    assert merged["target_dimensions"] == ["market", "brand", "campaign_kpi"]
