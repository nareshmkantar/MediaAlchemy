"""Tests for block-sparse metric layout detection."""

from sia.agent.metric_layout_signals import (
    detect_metric_layout_from_grid,
    enrich_structure_analysis_with_metric_signals,
)
from sia.models.cell import Cell, CellStyle, VisualGrid


def _build_grouped_spend_grid() -> VisualGrid:
    """
    Mimic influencer layout: date in col0, channel, category, spend (parent only), impressions dense.
    """
    header = ["Date", "Channel", "Category", "Spend", "Impressions"]
    rows = [
        header,
        ["2024-01-14", "TV Channel 1", "TV", "", "14402"],
        ["2024-01-14", "TV Channel 1", "Social", "5010", "300050"],
        ["2024-01-14", "TV Channel 2", "TV", "", "205"],
        ["2024-01-14", "TV Channel 2", "Social", "", "584483"],
        ["2024-01-15", "TV Channel 1", "TV", "5010", "10614"],
    ]
    cells = []
    for r, row in enumerate(rows):
        cells.append(
            [
                Cell(value=row[c], row=r, col=c, style=CellStyle())
                for c in range(len(row))
            ]
        )
    return VisualGrid(
        cells=cells,
        sheet_name="S",
        total_rows=len(rows),
        total_cols=len(header),
    )


def test_detect_block_sparse_spend_column():
    grid = _build_grouped_spend_grid()
    signals = detect_metric_layout_from_grid(grid)
    assert signals["grouped_rows_likely"] is True
    metrics = signals["block_sparse_metrics"]
    assert metrics
    spend_hits = [m for m in metrics if "spend" in str(m.get("column_label", "")).lower()]
    assert spend_hits
    assert spend_hits[0].get("grain") == "block_header_total"
    assert float(spend_hits[0].get("mean_segment_fill_rate") or 1) < 0.6
    assert spend_hits[0].get("allocation_required") is True


def test_enrich_adds_grouped_rows_hierarchy():
    grid = _build_grouped_spend_grid()
    signals = detect_metric_layout_from_grid(grid)
    analysis = {"tables": [{"label": "Main", "table_shape": "flat"}], "confidence": 0.7}
    out = enrich_structure_analysis_with_metric_signals(
        analysis, signals, grid_cols=grid.total_cols
    )
    assert out["metric_layout_signals"]["grouped_rows_likely"] is True
    assert out["tables"][0].get("hierarchy", {}).get("type") == "grouped_rows"


def _build_flat_daily_grid() -> VisualGrid:
    """CP-07 style: one date per row, publisher + spend + impressions all filled."""
    header = ["Date", "Publisher", "Spend", "Impressions"]
    rows = [header]
    publishers = ["SiteA", "SiteB", "SiteC"]
    for i in range(18):
        rows.append(
            [
                f"2025-03-{i + 1:02d}",
                publishers[i % 3],
                str(100 + i * 10),
                str(1000 + i * 100),
            ]
        )
    cells = [
        [Cell(value=row[c], row=r, col=c, style=CellStyle()) for c in range(len(row))]
        for r, row in enumerate(rows)
    ]
    return VisualGrid(
        cells=cells,
        sheet_name="Digital_UK",
        total_rows=len(rows),
        total_cols=len(header),
    )


def test_publisher_not_flagged_sparse_on_flat_daily_grid():
    grid = _build_flat_daily_grid()
    signals = detect_metric_layout_from_grid(grid)
    sparse_labels = [
        str(d.get("column_label") or "") for d in signals.get("sparse_dimension_columns") or []
    ]
    assert "Publisher" not in sparse_labels
    assert not signals.get("block_sparse_metrics")
    assert signals.get("grouped_rows_likely") is False


def _build_flat_monthly_grid_with_zero_revenue() -> VisualGrid:
    """Pinterest-style flat file: repeated Month keys, many revenue cells are numeric 0."""
    header = ["Month", "advertiser_name", "user_country", "revenue", "impressions"]
    rows = [header]
    for i in range(12):
        rows.append(
            [
                "2025-01",
                f"Advertiser {i % 3}",
                "DE",
                0 if i % 2 else 1200.5 + i,  # real zeros, not blanks
                1000 + i * 10,
            ]
        )
    cells = [
        [Cell(value=row[c], row=r, col=c, style=CellStyle()) for c in range(len(row))]
        for r, row in enumerate(rows)
    ]
    return VisualGrid(
        cells=cells,
        sheet_name="Clean",
        total_rows=len(rows),
        total_cols=len(header),
    )


def test_numeric_zero_revenue_is_not_block_sparse():
    """Regression: ``0 or ''`` used to treat zeros as blanks and invent block spend."""
    from sia.agent.metric_layout_signals import _cell_text

    grid = _build_flat_monthly_grid_with_zero_revenue()
    assert _cell_text(grid, 2, 3) == "0"  # first zero row (i=1)
    signals = detect_metric_layout_from_grid(grid)
    assert not signals.get("block_sparse_metrics")
    assert signals.get("grouped_rows_likely") is False
