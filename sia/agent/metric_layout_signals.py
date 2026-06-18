"""Deterministic detection of block-level / merged-metric layouts for structure analysis."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

_SPEND_LIKE = re.compile(
    r"(spend|spends|budget|cost|investment|amount|value|fee|media\s*cost)",
    re.I,
)


def _cell_text(grid: Any, row: int, col: int) -> str:
    cell = grid.get_cell(row, col)
    if cell is None or getattr(cell, "is_empty", lambda: True)():
        return ""
    return str(getattr(cell, "value", "") or "").strip()


def _is_numeric_text(text: str) -> bool:
    if not text:
        return False
    try:
        float(str(text).replace(",", "").replace("$", "").strip())
        return True
    except (TypeError, ValueError):
        return False


def _header_labels(grid: Any, header_row: int = 0, max_cols: int = 80) -> List[str]:
    labels: List[str] = []
    for c in range(min(int(grid.total_cols), max_cols)):
        labels.append(_cell_text(grid, header_row, c) or f"col_{c}")
    return labels


def _segments_by_group_column(
    grid: Any,
    group_col: int,
    *,
    data_start_row: int = 1,
) -> List[Tuple[int, int]]:
    """
    Group consecutive data rows with the same value in ``group_col`` (typically Date).

    Media sheets repeat the same date on multiple sub-rows (Channel/Category breakdown);
    block-sparse spend must be evaluated within these groups, not per physical row.
    """
    rows = int(grid.total_rows)
    if rows <= data_start_row:
        return []

    segs: List[Tuple[int, int]] = []
    seg_start = data_start_row
    prev_key: Optional[str] = None
    for r in range(data_start_row, rows):
        key = _cell_text(grid, r, group_col) or ""
        if prev_key is None:
            prev_key = key
            continue
        if key != prev_key:
            segs.append((seg_start, r - 1))
            seg_start = r
            prev_key = key
    segs.append((seg_start, rows - 1))
    return segs


def detect_metric_layout_from_grid(
    grid: Any,
    *,
    header_row: int = 0,
    anchor_col: int = 0,
    parent_rate_threshold: float = 0.45,
    child_sparse_threshold: float = 0.30,
) -> Dict[str, Any]:
    """
    Scan a VisualGrid for influencer-style layouts:
    - dimensions sparse (filled on block parent rows)
    - metrics (especially spend/budget) only on parent rows within multi-row blocks
    """
    rows = int(getattr(grid, "total_rows", 0) or 0)
    cols = int(getattr(grid, "total_cols", 0) or 0)
    if rows < 3 or cols < 2:
        return {
            "grouped_rows_likely": False,
            "block_sparse_metrics": [],
            "sparse_dimension_columns": [],
            "merged_ranges_count": len(getattr(grid, "merged_ranges", None) or []),
            "notes": ["Grid too small for metric layout scan"],
        }

    labels = _header_labels(grid, header_row=header_row, max_cols=cols)
    data_start = header_row + 1
    segments = _segments_by_group_column(
        grid, anchor_col, data_start_row=data_start
    )
    multi_row_segments = [(s, e) for (s, e) in segments if (e - s) >= 1]

    block_sparse_metrics: List[Dict[str, Any]] = []
    sparse_dimension_columns: List[Dict[str, Any]] = []

    for c in range(1, cols):
        col_vals: List[Optional[float]] = []
        for r in range(rows):
            t = _cell_text(grid, r, c)
            if _is_numeric_text(t):
                try:
                    col_vals.append(float(str(t).replace(",", "").replace("$", "")))
                except (TypeError, ValueError):
                    col_vals.append(None)
            else:
                col_vals.append(None)
        arr = np.array([np.nan if v is None else v for v in col_vals], dtype=float)
        numeric_rate = float(np.isfinite(arr).mean())
        label = labels[c] if c < len(labels) else f"col_{c}"

        if numeric_rate < 0.04:
            blank_ratio = 1.0 - numeric_rate
            if blank_ratio >= 0.15:
                sparse_dimension_columns.append(
                    {
                        "column_index": c,
                        "column_label": label,
                        "blank_ratio": round(blank_ratio, 4),
                        "role_hint": "dimension_or_label",
                    }
                )
            continue

        if not multi_row_segments:
            continue

        partial_segments = 0
        mean_fill = 0.0
        fill_rates: List[float] = []
        for s, e in multi_row_segments:
            seg = arr[s : e + 1]
            seg_len = len(seg)
            if seg_len < 2:
                continue
            fill = float(np.isfinite(seg).sum()) / seg_len
            fill_rates.append(fill)
            # Block-sparse: some rows have the metric, most rows do not (gaps under same date group)
            if 0.0 < fill < 0.95:
                partial_segments += 1
        if not fill_rates:
            continue
        mean_fill = float(sum(fill_rates) / len(fill_rates))
        partial_rate = float(partial_segments / max(len(multi_row_segments), 1))

        spend_like = bool(_SPEND_LIKE.search(label))
        if partial_rate >= parent_rate_threshold and mean_fill <= max(child_sparse_threshold, 0.55):
            block_sparse_metrics.append(
                {
                    "column_index": c,
                    "column_label": label,
                    "grain": "block_header_total",
                    "partial_segment_rate": round(partial_rate, 3),
                    "mean_segment_fill_rate": round(mean_fill, 3),
                    "overall_numeric_rate": round(numeric_rate, 3),
                    "spend_like": spend_like,
                    "recommended_tool": "transform.expand_grouped_block",
                    "allocation_required": True,
                }
            )
        elif numeric_rate < 0.85 and (1.0 - numeric_rate) >= 0.15:
            sparse_dimension_columns.append(
                {
                    "column_index": c,
                    "column_label": label,
                    "blank_ratio": round(1.0 - numeric_rate, 4),
                    "role_hint": "sparse_non_metric",
                }
            )

    block_sparse_metrics.sort(
        key=lambda x: (not x.get("spend_like"), -float(x.get("partial_segment_rate") or 0))
    )
    sparse_dimension_columns = sparse_dimension_columns[:12]
    grouped_rows_likely = bool(block_sparse_metrics) or any(
        float(d.get("blank_ratio") or 0) >= 0.25 for d in sparse_dimension_columns
    )

    merged = list(getattr(grid, "merged_ranges", None) or [])
    notes: List[str] = []
    if block_sparse_metrics:
        names = ", ".join(
            str(m.get("column_label")) for m in block_sparse_metrics[:4]
        )
        notes.append(
            f"Block-level metrics detected ({names}): values appear on section parent rows "
            "with blanks on continuation rows — requires expand_grouped_block or allocate_block_metric, "
            "not fill_merged on spend."
        )
    if merged:
        notes.append(f"Excel merged_ranges={len(merged)} — pass merged_metric_ranges into expand/allocate tools.")

    return {
        "grouped_rows_likely": grouped_rows_likely,
        "block_sparse_metrics": block_sparse_metrics[:8],
        "sparse_dimension_columns": sparse_dimension_columns,
        "merged_ranges_count": len(merged),
        "merged_ranges_preview": [str(x) for x in merged[:6]],
        "date_group_segment_count": len(segments),
        "multi_row_segment_count": len([x for x in segments if x[1] > x[0]]),
        "notes": notes,
    }


def enrich_structure_analysis_with_metric_signals(
    analysis: Dict[str, Any],
    signals: Dict[str, Any],
    *,
    grid_cols: Optional[int] = None,
) -> Dict[str, Any]:
    """Merge deterministic metric-layout signals into LLM structure output."""
    if not isinstance(analysis, dict):
        analysis = {}
    out = dict(analysis)
    out["metric_layout_signals"] = signals

    if signals.get("grouped_rows_likely"):
        assumptions = list(out.get("assumptions") or [])
        note = (
            "Deterministic scan: grouped-row / block-sparse metric layout — "
            "allocate block-level spend/budget across child rows before aggregation."
        )
        if note not in assumptions:
            assumptions.append(note)
        out["assumptions"] = assumptions

        uncertainty = list(out.get("uncertainty") or [])
        if signals.get("block_sparse_metrics") and not any(
            isinstance(u, dict) and u.get("topic") == "block_metric_allocation"
            for u in uncertainty
        ):
            cols = [m.get("column_label") for m in signals["block_sparse_metrics"][:3]]
            uncertainty.append(
                {
                    "topic": "block_metric_allocation",
                    "detail": f"Metrics {cols} need block allocation (parent-row totals).",
                    "confidence": 0.85,
                }
            )
        out["uncertainty"] = uncertainty

    tables = list(out.get("tables") or [])
    if signals.get("grouped_rows_likely") and tables:
        for tbl in tables:
            if not isinstance(tbl, dict):
                continue
            if not isinstance(tbl.get("hierarchy"), dict):
                tbl["hierarchy"] = {
                    "type": "grouped_rows",
                    "evidence": "metric_layout_signals",
                    "block_sparse_metrics": signals.get("block_sparse_metrics") or [],
                }
            elif str((tbl.get("hierarchy") or {}).get("type") or "").strip().lower() != "grouped_rows":
                hnotes = tbl.get("hierarchy_notes")
                if not isinstance(hnotes, list):
                    hnotes = []
                hnotes.append(
                    "metric_layout_signals suggest grouped_rows with block metrics"
                )
                tbl["hierarchy_notes"] = hnotes
        out["tables"] = tables
    elif signals.get("grouped_rows_likely") and not tables:
        out["tables"] = [
            {
                "label": "Grouped data block",
                "coordinates": {
                    "header_row": 0,
                    "data_start_row": 1,
                    "data_end_row": max(0, int(signals.get("date_group_segment_count") or 1)),
                    "col_start": 0,
                    "col_end": max(0, int(grid_cols or 0) - 1),
                },
                "table_shape": "flat",
                "hierarchy": {
                    "type": "grouped_rows",
                    "evidence": "metric_layout_signals_only",
                    "block_sparse_metrics": signals.get("block_sparse_metrics") or [],
                },
            }
        ]
        out.setdefault("assumptions", []).append(
            "Implicit table created from metric layout scan (LLM returned no tables)."
        )

    return out
