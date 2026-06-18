import pandas as pd

from sia.agent.date_column_inference import (
    build_date_column_observations_for_planning,
    build_date_inference_approval_items,
    infer_date_cadence_fast,
    infer_date_column_profile,
    infer_date_range_column_pair,
    infer_date_range_pair_heuristic,
)


def test_infer_daily_cadence():
    idx = pd.date_range("2024-01-01", periods=40, freq="D")
    s = pd.Series(idx)
    p = infer_date_column_profile(s, "d")
    assert p["inferred_cadence"] == "daily"
    assert p["median_gap_days_rounded"] == 1


def test_infer_weekly_cadence():
    idx = pd.date_range("2024-01-01", periods=20, freq="7D")
    s = pd.Series(idx)
    p = infer_date_column_profile(s, "w")
    assert p["inferred_cadence"] == "weekly"


def test_infer_monthly_cadence():
    idx = pd.date_range("2024-01-01", periods=24, freq="MS")
    s = pd.Series(idx)
    p = infer_date_column_profile(s, "m")
    assert p["inferred_cadence"] == "monthly"


def test_infer_date_cadence_fast_vectorized_monthly():
    idx = pd.date_range("2024-01-01", periods=24, freq="MS")
    s = pd.Series(idx)
    p = infer_date_cadence_fast(s, "m")
    assert p.get("inference_method") == "vectorized_bulk"
    assert p["inferred_cadence"] == "monthly"
    assert p["n_unique_parsed_dates"] == 24


def test_month_name_strings_parse():
    s = pd.Series(["Jan 2024", "Feb 2024", "Mar 2024", "Apr 2024", "May 2024"])
    p = infer_date_column_profile(s, "period")
    assert p["parse_rate"] >= 0.9
    assert p["inferred_cadence"] == "monthly"


def test_build_observations_for_mapped_uid():
    df = pd.DataFrame(
        {
            "Event Date": pd.date_range("2024-01-01", periods=30, freq="D"),
            "cost": [1.0] * 30,
        }
    )
    template = {
        "x_scope": {"uid_hierarchy": ["date_paid_media"], "metrics": ["total_cost_paid_media"]},
        "properties": {"date_paid_media": {"type": "string"}},
    }
    mappings = [
        {"source_column": "Event Date", "target_column": "date_paid_media", "decision": "Keep"},
        {"source_column": "cost", "target_column": "total_cost_paid_media", "decision": "Keep"},
    ]
    out = build_date_column_observations_for_planning(
        df,
        mappings,
        template,
        guided_date_granularity="weekly",
    )
    assert out["columns"]
    assert out["columns"][0]["inferred_cadence"] == "daily"
    assert "guided setup" in (out.get("mismatch_note") or "").lower()


def test_ddmon_hyphen_label_not_textual_range():
    """Day-Month labels use a hyphen inside one token; must not count as start–end range text."""
    s = pd.Series(["26-Jun", "27-Jun"])
    p = infer_date_column_profile(s, "date")
    assert float(p.get("range_like_cell_share") or 0) < 0.12
    assert p.get("likely_range_string_cells") is False


def test_en_dash_between_two_dates_counts_as_textual_range():
    s = pd.Series(["2024-01-01 – 2024-01-07", "2024-01-08 – 2024-01-14"])
    p = infer_date_column_profile(s, "flight")
    assert p.get("likely_range_string_cells") is True


def test_spaced_ascii_hyphen_between_dates_counts_as_textual_range():
    s = pd.Series(["2024-01-01 - 2024-01-07", "2024-01-08 - 2024-01-14"])
    p = infer_date_column_profile(s, "flight")
    assert p.get("likely_range_string_cells") is True


def test_infer_date_range_heuristic_rejects_date_plus_numeric_metrics():
    """Numeric metrics parse as epoch datetimes; they must not pair with a real date column."""
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-26", "2026-06-27"]),
            "cost": [6.33, 20.175],
            "impressions": [422, 1345],
        }
    )
    assert infer_date_range_pair_heuristic(df) is None


def test_datetime_iso_strings_not_mistaken_for_textual_ranges():
    """Excel/datetime cells stringify to ISO-like text with hyphens; must not flag as flight ranges."""
    s = pd.Series(pd.to_datetime(["2026-06-26", "2026-06-27"]))
    p = infer_date_column_profile(s, "date")
    assert p.get("likely_range_string_cells") is False
    assert float(p.get("range_like_cell_share") or 0) < 0.12
    assert p["inferred_cadence"] == "daily"
    assert p["median_gap_days"] == 1.0
    assert p["n_unique_parsed_dates"] == 2


def test_two_unique_dates_median_gap_weekly():
    s = pd.Series(pd.to_datetime(["2024-01-01", "2024-01-08"]))
    p = infer_date_column_profile(s, "w")
    assert p["n_unique_parsed_dates"] == 2
    assert p["median_gap_days"] == 7.0
    assert p["inferred_cadence"] == "weekly"
    assert p["gap_p25_days"] == 7.0 and p["gap_p75_days"] == 7.0


def test_single_unique_date_insufficient_for_gap():
    s = pd.Series(pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-01"]))
    p = infer_date_column_profile(s, "d")
    assert p["n_unique_parsed_dates"] == 1
    assert p.get("median_gap_days") is None
    assert p["inferred_cadence"] == "insufficient_data"


def test_infer_date_cadence_fast_two_unique_daily():
    s = pd.Series(pd.to_datetime(["2024-03-01", "2024-03-02"]))
    p = infer_date_cadence_fast(s, "d")
    assert p.get("inference_method") == "vectorized_bulk"
    assert p["n_unique_parsed_dates"] == 2
    assert p["median_gap_days"] == 1.0
    assert p["inferred_cadence"] == "daily"


def test_range_like_share_flag():
    s = pd.Series(
        [
            "2024-01-01 to 2024-01-07",
            "2024-01-08 to 2024-01-14",
            "2024-01-15 to 2024-01-21",
        ]
    )
    p = infer_date_column_profile(s, "flight")
    assert p.get("likely_range_string_cells") is True
    assert p.get("inferred_cadence") == "weekly"
    assert p.get("median_window_days") == 7
    assert float(p.get("confidence") or 0) >= 0.7


def test_textual_range_jan_to_feb():
    s = pd.Series(
        [
            "Jan 7th 2024 to Feb 6th 2024",
            "Feb 7th 2024 to Mar 6th 2024",
            "Mar 7th 2024 to Apr 5th 2024",
        ]
    )
    p = infer_date_column_profile(s, "flight")
    assert p.get("parse_rate", 0) >= 0.9
    assert p.get("median_window_days") is not None
    assert int(p["median_window_days"]) >= 28


def test_build_date_inference_approval_items_low_confidence():
    pkg = {
        "columns": [
            {
                "column": "messy",
                "target_column": "date_paid_media",
                "inferred_cadence": "irregular",
                "median_gap_days": 4.2,
                "parse_rate": 0.55,
                "range_parse_rate": 1.0,
                "range_like_cell_share": 0.0,
                "confidence": 0.4,
            }
        ],
        "mismatch_note": "",
        "aggregate_confidence": 0.4,
        "guided_date_granularity": "daily",
        "summary": "messy→date_paid_media: cadence≈irregular",
    }
    items = build_date_inference_approval_items(pkg)
    assert len(items) == 1
    assert items[0].get("options") and len(items[0]["options"]) >= 2
    assert "messy" in (items[0].get("question") or "")
    assert any("40%" in str(o.get("label", "")) for o in items[0]["options"])


def test_build_date_inference_approval_items_empty_when_confident():
    pkg = {
        "columns": [
            {
                "column": "d",
                "target_column": "date_paid_media",
                "inferred_cadence": "daily",
                "confidence": 0.95,
            }
        ],
        "aggregate_confidence": 0.95,
        "mismatch_note": "",
    }
    assert build_date_inference_approval_items(pkg) == []


def test_infer_date_range_pair_heuristic_from_headers_only():
    """No template date mapping needed — Start/End headers + parseable dates."""
    df = pd.DataFrame(
        {
            "Start Date": ["1/13/2023", "1/15/2023", "1/15/2023", "1/15/2023"],
            "End Date": ["2/28/2023", "3/4/2023", "2/26/2023", "2/28/2023"],
            "Spend": [428.74, 440.64, 432.19, 443.32],
        }
    )
    pair = infer_date_range_pair_heuristic(df)
    assert pair is not None
    assert pair["start_date_col"] == "Start Date"
    assert pair["end_date_col"] == "End Date"
    assert pair.get("inference_method") == "header_parseable_columns"


def test_infer_date_range_column_pair_two_mapped_dates():
    df = pd.DataFrame(
        {
            "Flight Start": pd.to_datetime(["2024-01-01", "2024-01-08", "2024-01-15"]),
            "Flight End": pd.to_datetime(["2024-01-07", "2024-01-14", "2024-01-21"]),
            "Spend": [700.0, 700.0, 700.0],
        }
    )
    template = {
        "x_scope": {"date_columns": ["period_start", "period_end"], "metrics": ["spend"]},
        "properties": {
            "period_start": {"type": "string", "format": "date"},
            "period_end": {"type": "string", "format": "date"},
            "spend": {"type": "number"},
        },
    }
    mappings = [
        {"source_column": "Flight Start", "target_column": "period_start", "decision": "Keep"},
        {"source_column": "Flight End", "target_column": "period_end", "decision": "Keep"},
        {"source_column": "Spend", "target_column": "spend", "decision": "Keep"},
    ]
    pair = infer_date_range_column_pair(df, mappings, template)
    assert pair is not None
    assert pair["start_date_col"] == "Flight Start"
    assert pair["end_date_col"] == "Flight End"
    assert pair["confidence"] >= 0.55
    assert pair["median_span_days"] == 7.0


def test_build_observations_includes_inferred_range_pair():
    df = pd.DataFrame(
        {
            "d1": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]),
            "d2": pd.to_datetime(["2024-01-10", "2024-02-10", "2024-03-10", "2024-04-10"]),
            "m": [1.0, 2.0, 3.0, 4.0],
        }
    )
    template = {
        "x_scope": {"date_columns": ["a", "b"], "metrics": ["m"]},
        "properties": {"a": {"type": "date"}, "b": {"type": "date"}, "m": {"type": "number"}},
    }
    mappings = [
        {"source_column": "d1", "target_column": "a", "decision": "Keep"},
        {"source_column": "d2", "target_column": "b", "decision": "Keep"},
        {"source_column": "m", "target_column": "m", "decision": "Keep"},
    ]
    out = build_date_column_observations_for_planning(df, mappings, template, guided_date_granularity="weekly")
    assert out.get("inferred_date_range_pair")
    assert "two-column range" in (out.get("summary") or "")


def test_range_granularity_no_mismatch_when_span_pair_and_daily_end_column():
    """Guided 'range' is span semantics; ~daily gaps on an end column are normal with a start/end pair."""
    df = pd.DataFrame(
        {
            "Start Date": pd.date_range("2024-01-01", periods=40, freq="D"),
            "End Date": pd.date_range("2024-01-01", periods=40, freq="D") + pd.Timedelta(days=40),
        }
    )
    template = {
        "x_scope": {"date_columns": ["a", "b"], "metrics": []},
        "properties": {"a": {"type": "date"}, "b": {"type": "date"}},
    }
    mappings = [
        {"source_column": "Start Date", "target_column": "a", "decision": "Keep"},
        {"source_column": "End Date", "target_column": "b", "decision": "Keep"},
    ]
    out = build_date_column_observations_for_planning(df, mappings, template, guided_date_granularity="range")
    assert out.get("inferred_date_range_pair")
    assert float(out["inferred_date_range_pair"].get("confidence") or 0) >= 0.55
    assert not (out.get("mismatch_note") or "").strip()


def test_build_date_inference_approval_items_adds_mismatch_row():
    pkg = {
        "columns": [
            {
                "column": "d",
                "target_column": "date_paid_media",
                "inferred_cadence": "daily",
                "median_gap_days": 1.0,
                "parse_rate": 1.0,
                "range_parse_rate": 1.0,
                "range_like_cell_share": 0.0,
                "confidence": 0.95,
            }
        ],
        "aggregate_confidence": 0.95,
        "guided_date_granularity": "weekly",
        "mismatch_note": "Guided Setup says weekly but data looks daily.",
    }
    items = build_date_inference_approval_items(pkg)
    assert len(items) == 1
    assert items[0].get("options")
    assert any("Guided Setup" in str(o.get("label", "")) for o in items[0]["options"])


def test_aggregate_confidence_ignores_unparsed_text_uid_columns():
    """UID hierarchy targets (publisher/channel) must not zero out aggregate when date is daily."""
    df = pd.DataFrame(
        {
            "date": pd.date_range("2023-06-01", periods=10, freq="D"),
            "publisher": ["A", "B", "A", "B", "A", "B", "A", "B", "A", "B"],
            "channel": ["x", "y", "x", "y", "x", "y", "x", "y", "x", "y"],
        }
    )
    template = {
        "x_scope": {
            "uid_hierarchy": ["date", "publisher", "channel"],
            "metrics": ["impressions"],
        },
        "properties": {
            "date": {"type": "string", "format": "date"},
            "publisher": {"type": "string"},
            "channel": {"type": "string"},
            "impressions": {"type": "number"},
        },
    }
    mappings = [
        {"source_column": "date", "target_column": "date", "decision": "Keep"},
        {"source_column": "publisher", "target_column": "publisher", "decision": "Keep"},
        {"source_column": "channel", "target_column": "channel", "decision": "Keep"},
    ]
    out = build_date_column_observations_for_planning(df, mappings, template, guided_date_granularity="")
    assert out["aggregate_confidence"] == 1.0
    assert build_date_inference_approval_items(out) == []

