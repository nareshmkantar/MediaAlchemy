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
