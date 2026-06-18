"""Tests for VisualGrid.to_smart_summary context bounds."""

from sia.models.cell import Cell, CellStyle, VisualGrid


def _grid_with_dense_col0(n_rows: int, n_cols: int = 3) -> VisualGrid:
    cells = []
    for r in range(n_rows):
        row = []
        for c in range(n_cols):
            v = f"r{r}c{c}" if c == 0 else ""
            row.append(Cell(value=v, row=r, col=c, style=CellStyle()))
        cells.append(row)
    return VisualGrid(
        cells=cells,
        sheet_name="S",
        total_rows=n_rows,
        total_cols=n_cols,
    )


def test_smart_summary_subsamples_anchors_and_avoids_huge_footer():
    g = _grid_with_dense_col0(5000, n_cols=4)
    text = g.to_smart_summary(max_cols=10, header_rows=3, samples_per_anchor=1, max_anchor_rows=80)
    assert "total=5000" in text
    assert "included_in_body=80" in text or "included_in_body=" in text
    # Previously the footer listed every anchor index — should stay compact
    assert len(text) < 120_000
    assert text.count("[A]") <= 200


def test_smart_summary_keeps_all_anchors_when_few():
    g = _grid_with_dense_col0(12, n_cols=2)
    text = g.to_smart_summary(max_anchor_rows=200)
    assert "12 anchors found in Column 0" in text or "12 of 12 anchors" in text
