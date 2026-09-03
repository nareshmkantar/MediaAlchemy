"""Layout complexity diagnostics and adapter vs vanilla flood-fill."""

import pandas as pd

from sia.agent.demarcator import StructureDemarcator
from sia.agent.sheet_layout_class import (
    adapter_blocks_from_report,
    detect_layout_complexity,
    format_cell_display,
    should_use_layout_adapter,
)


def _planning_matrix_df() -> pd.DataFrame:
    """Sparse media-plan style grid: month headers, KPI stub, far-right orphan numeric."""
    cols = 12
    rows = 8
    data = [[None] * cols for _ in range(rows)]
    # Title
    data[0][0] = "FOR INTERNAL USE ONLY"
    data[0][5] = "Media plan 2025 | Germany"
    # Month / week headers
    data[1][3] = "January"
    data[1][4] = "January"
    data[1][5] = "February"
    data[1][6] = "February"
    data[2][2] = "Performance indicators"
    data[2][3] = "W1"
    data[2][4] = "W2"
    data[2][5] = "W1"
    data[2][6] = "W2"
    data[2][1] = "Media"
    data[3][0] = "41113"
    data[3][1] = "TV"
    data[3][2] = "GRPs"
    data[3][3] = 75.7
    data[3][4] = 67.1
    data[3][5] = 66.8
    data[3][6] = 70.0
    data[4][0] = "41113"
    data[4][1] = "TV"
    data[4][2] = "Weekly costs"
    data[4][3] = 100
    data[4][4] = 110
    data[4][5] = 90
    data[4][6] = 95
    # Far-right tiny island (the BI20 problem)
    data[6][11] = 4807274.98
    return pd.DataFrame(data)


def test_planning_matrix_is_messy_crosstab_with_kpi_rows():
    report = detect_layout_complexity(_planning_matrix_df(), {"merged_ranges": ["A1:B4", "D2:E2"]})
    assert report["crosstab"] is True
    assert report["axes"]["date_axis"] == "columns"
    assert report["axes"]["time_kind"] == "period_headers"
    assert report["axes"]["metric_name_axis"] == "rows"
    assert report["classification"] in {"semi_structured", "messy"}
    assert should_use_layout_adapter(report) is True
    assert report["sample_records"]
    assert any(r.get("metric") for r in report["sample_records"])
    assert any(r.get("row") is not None and r.get("col") is not None for r in report["sample_records"])


def test_minimap_has_role_overlays_and_occupancy():
    report = detect_layout_complexity(_planning_matrix_df(), {"merged_ranges": ["A1:B4", "D2:E2"]})
    minimap = report.get("minimap") or {}
    assert minimap.get("occupancy")
    assert "1" in minimap["occupancy"]
    assert int(minimap["grid_rows"]) >= 1
    assert int(minimap["grid_cols"]) >= 1
    roles = {o["role"] for o in (minimap.get("overlays") or [])}
    assert {"time", "metrics", "values", "noise", "metadata"} <= roles
    noises = [o for o in minimap["overlays"] if o["role"] == "noise"]
    assert any(o["start_col"] == 11 for o in noises)
    assert any(o.get("start_row") == 0 and o.get("start_col") == 0 for o in noises)
    meta = next(o for o in minimap["overlays"] if o["role"] == "metadata")
    assert meta["end_col"] >= 5
    noise = next(o for o in noises if o["start_col"] == 11)
    assert noise["style"] == "dot"
    merges = minimap.get("merges") or []
    assert any(m.get("start_row") == 0 and m.get("end_col") >= 1 for m in merges)


def test_bare_week_numbers_under_months_count_as_period_headers():
    from datetime import datetime

    cols = 10
    data = [[None] * cols for _ in range(8)]
    data[0][3] = "January"
    data[0][4] = "January"
    data[0][5] = "January"
    data[0][6] = "February"
    data[1][3] = 1
    data[1][4] = 2
    data[1][5] = 3
    data[1][6] = 1
    data[2][3] = datetime(1900, 1, 5)
    data[2][4] = datetime(1900, 1, 12)
    data[2][5] = datetime(1900, 1, 19)
    data[2][6] = datetime(1900, 2, 2)
    data[3][1] = "TV"
    data[3][2] = "GRPs"
    data[3][3] = 75.7
    data[3][4] = 67.1
    data[3][5] = 66.8
    data[3][6] = 70.0
    # Merged Totals header filled down the column must not mark every row as a total
    data[0][8] = "Totals"
    data[3][8] = "Totals"
    data[4][8] = "Totals"
    report = detect_layout_complexity(pd.DataFrame(data), {"merged_ranges": ["D1:F1"]})
    header_rows = report["axes"]["header_rows"]
    assert 0 in header_rows
    assert 1 in header_rows
    assert 2 in header_rows
    time = next(o for o in report["minimap"]["overlays"] if o["role"] == "time")
    assert time["start_row"] <= 1
    assert time["end_row"] >= 1
    values = next(o for o in report["minimap"]["overlays"] if o["role"] == "values")
    assert values["start_row"] >= 3
    assert report["axes"]["total_cols"]
    assert 3 not in (report["axes"].get("total_rows") or [])
    assert 4 not in (report["axes"].get("total_rows") or [])


def test_adapter_emits_one_main_block_not_tiny_noise_cards():
    dem = StructureDemarcator(llm_client=None)
    df = _planning_matrix_df()
    report = detect_layout_complexity(df, {"merged_ranges": ["D2:E2"]})
    blocks = adapter_blocks_from_report(dem, df, report)
    cats = [b["category"] for b in blocks]
    assert "Main Data" in cats
    assert sum(1 for c in cats if c == "Main Data") == 1
    # Far-right 1x1 must not be its own Noise card
    for b in blocks:
        coords = b["coordinates"]
        width = coords["end_col"] - coords["start_col"] + 1
        height = coords["end_row"] - coords["start_row"] + 1
        assert not (b["category"] == "Noise" and width <= 2 and height <= 2)


def test_run_python_phases_uses_adapter_for_matrix():
    dem = StructureDemarcator(llm_client=None)
    out = {}
    blocks = dem.run_python_demarcation_phases(
        _planning_matrix_df(),
        visual_patterns={"merged_ranges": ["D2:E2", "F2:G2"]},
        layout_report_out=out,
    )
    assert out.get("adapter_used") is True
    assert any(b["category"] == "Main Data" for b in blocks)
    assert all(b["category"] != "Noise" for b in blocks)


def test_clean_flat_table_stays_on_connected_components():
    df = pd.DataFrame(
        [
            ["Date", "Channel", "Spend"],
            ["2025-01-01", "TV", 10],
            ["2025-01-02", "TV", 12],
            ["2025-01-03", "OOH", 8],
        ]
    )
    report = detect_layout_complexity(df, {"merged_ranges": []})
    assert report["crosstab"] is False
    assert should_use_layout_adapter(report) is False
    dem = StructureDemarcator(llm_client=None)
    blocks = dem.run_python_demarcation_phases(df, visual_patterns={"merged_ranges": []})
    assert len(blocks) == 1
    assert blocks[0]["category"] == "Main Data"


def test_format_cell_display_percent_and_float_junk():
    assert format_cell_display(0.565, "0.0%") == "56.5%"
    assert format_cell_display(0.343, "0%") == "34.3%"
    assert format_cell_display("34.3%") == "34.3%"
    assert format_cell_display(209.60000000000002) == "209.6"
    assert format_cell_display(75.7) == "75.7"
