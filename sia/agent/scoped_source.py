from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# Multi-level header heuristic (see plan: blanks/duplicates on row 1 + sub-row text/numeric profile)
_ML_HEADER_BLANK_RATIO_TRIGGER = 0.22
_ML_SUBHEADER_TEXT_RATIO = 0.55
_ML_SUBHEADER_MIN_FILL_RATIO = 0.25

from sia.models.cell import VisualGrid


KEEP_DECISIONS = {"keep", "approved", "main data"}
CONTEXT_DECISIONS = {"context", "metadata", "use as context"}
DISCARD_DECISIONS = {"discard", "ignore", "noise"}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_header_cell(val: Any) -> str:
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none"):
        return ""
    return s


def _row_blank_ratio(series: pd.Series) -> float:
    n = len(series)
    if n == 0:
        return 1.0
    blanks = sum(1 for v in series if _normalize_header_cell(v) == "")
    return blanks / n


def _row_has_duplicate_non_empty_labels(series: pd.Series) -> bool:
    counts: Dict[str, int] = {}
    for v in series:
        t = _normalize_header_cell(v)
        if not t:
            continue
        key = t.casefold()
        counts[key] = counts.get(key, 0) + 1
    return any(c > 1 for c in counts.values())


def _row_text_numeric_filled(series: pd.Series) -> Tuple[int, int, int]:
    """text_like_count, numeric_like_count, filled_count."""
    text_c = num_c = 0
    filled = 0
    for v in series:
        t = _normalize_header_cell(v)
        if not t:
            continue
        filled += 1
        try:
            float(t.replace(",", "").replace("$", "").replace("%", ""))
            num_c += 1
        except (ValueError, TypeError):
            text_c += 1
    return text_c, num_c, filled


def _is_probable_subheader_row(series: pd.Series, width: int) -> bool:
    # Sub-header rows are label-only; any numeric-like cell implies data (first row false positive).
    text_c, num_c, filled = _row_text_numeric_filled(series)
    if filled == 0:
        return False
    if num_c > 0:
        return False
    text_ratio = text_c / filled
    fill_ratio = filled / max(width, 1)
    return text_ratio >= _ML_SUBHEADER_TEXT_RATIO and fill_ratio >= _ML_SUBHEADER_MIN_FILL_RATIO


def _multi_level_header_candidate(row_top: pd.Series) -> bool:
    return (
        _row_blank_ratio(row_top) >= _ML_HEADER_BLANK_RATIO_TRIGGER
        or _row_has_duplicate_non_empty_labels(row_top)
    )


def _ffill_merged_header_row(row: pd.Series) -> pd.Series:
    """
    Simulate Excel horizontal merges: empty cells inherit the last non-empty label to the left.
    """
    cells: List[Any] = []
    for v in row:
        t = _normalize_header_cell(v)
        cells.append(t if t else pd.NA)
    ser = pd.Series(cells, index=row.index, dtype=object)
    return ser.ffill().fillna("")


def _merge_two_header_rows_to_labels(
    row_top: pd.Series, row_bottom: pd.Series, sep: str = "_"
) -> Tuple[List[str], List[bool]]:
    """Returns column labels and per-column ``derived`` (True only when two non-empty levels were joined)."""
    top_ff = _ffill_merged_header_row(row_top)
    names: List[str] = []
    derived: List[bool] = []
    n = len(top_ff)
    for i in range(n):
        a = _normalize_header_cell(top_ff.iloc[i])
        # Sub-header row: do not forward-fill horizontally; blank means "no sub-label" for that column.
        b = _normalize_header_cell(row_bottom.iloc[i])
        if a and b:
            names.append(f"{a}{sep}{b}")
            derived.append(True)
        elif a:
            names.append(a)
            derived.append(False)
        elif b:
            names.append(b)
            derived.append(False)
        else:
            names.append(f"Unnamed_{i}")
            derived.append(False)
    return names, derived


def _merge_header_row_range_to_labels(
    df_slice: pd.DataFrame, start_rel: int, end_rel: int, sep: str = "_"
) -> Tuple[List[str], List[bool]]:
    """
    Merge consecutive header rows (inclusive) into one label per column.
    Each row is forward-filled first (merged parent cells). ``derived`` is True only when
    two or more non-empty levels contribute (not when a sub-row cell is blank and only the parent remains).
    """
    width = len(df_slice.columns)
    names: List[str] = []
    derived: List[bool] = []
    n_rows = len(df_slice)
    start_rel = max(0, min(start_rel, n_rows - 1))
    end_rel = max(start_rel, min(end_rel, n_rows - 1))
    # Forward-fill only the top header row (Excel horizontal merges). Lower rows use raw cells per column.
    row_series: List[pd.Series] = []
    for r in range(start_rel, end_rel + 1):
        ser = df_slice.iloc[r]
        if r == start_rel:
            row_series.append(_ffill_merged_header_row(ser))
        else:
            row_series.append(
                pd.Series(
                    [_normalize_header_cell(ser.iloc[j]) for j in range(width)],
                    index=ser.index,
                    dtype=object,
                )
            )
    for j in range(width):
        parts: List[str] = []
        for fr in row_series:
            t = _normalize_header_cell(fr.iloc[j])
            if t:
                parts.append(t)
        if not parts:
            names.append(f"Unnamed_{j}")
            derived.append(False)
        else:
            names.append(sep.join(parts))
            derived.append(len(parts) >= 2)
    return names, derived


def _make_unique_columns(
    columns: List[Any],
    derived_flags: Optional[List[bool]] = None,
) -> Tuple[List[str], List[bool]]:
    unique: List[str] = []
    seen: Dict[str, int] = {}
    out_derived: List[bool] = []
    for idx, column in enumerate(columns):
        base = str(column) if pd.notna(column) and str(column).strip() else f"Unnamed: {idx}"
        dflag = (
            bool(derived_flags[idx])
            if derived_flags is not None and idx < len(derived_flags)
            else False
        )
        if base not in seen:
            seen[base] = 0
            unique.append(base)
            out_derived.append(dflag)
        else:
            seen[base] += 1
            unique.append(f"{base}.{seen[base]}")
            out_derived.append(dflag)
    return unique, out_derived


def _get_block_coordinates(block: Dict[str, Any]) -> Dict[str, int]:
    coords = block.get("coordinates", block) if isinstance(block, dict) else {}
    start_row = _safe_int(coords.get("start_row", coords.get("header_row", 0)))
    end_row = _safe_int(coords.get("end_row", coords.get("data_end_row", start_row)))
    start_col = _safe_int(coords.get("start_col", coords.get("col_start", 0)))
    end_col = _safe_int(coords.get("end_col", coords.get("col_end", start_col)))
    header_row = _safe_int(coords.get("header_row", start_row))
    out: Dict[str, Any] = {
        "start_row": start_row,
        "end_row": end_row,
        "start_col": start_col,
        "end_col": end_col,
        "header_row": header_row,
    }
    hm = coords.get("header_mode")
    if hm is not None:
        s = str(hm).strip().lower()
        if s in ("single", "multi"):
            out["header_mode"] = s
    if coords.get("header_row_end") is not None:
        try:
            out["header_row_end"] = int(coords["header_row_end"])
        except (TypeError, ValueError):
            pass
    return out


def _classify_blocks(blocks: Optional[List[Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    main_blocks: List[Dict[str, Any]] = []
    context_blocks: List[Dict[str, Any]] = []

    for block in blocks or []:
        decision = str(block.get("decision", "")).strip().lower()
        category = str(block.get("category", "")).strip().lower()

        normalized = dict(block)
        normalized["coordinates"] = _get_block_coordinates(block)

        # An explicit user decision always overrides the AI-proposed category.
        # (Clicking "Use as Metadata"/"Ignore" only changes `decision`; the
        # original `category` stays "Main Data", so category must not win here.)
        if decision in CONTEXT_DECISIONS:
            context_blocks.append(normalized)
        elif decision in DISCARD_DECISIONS:
            continue
        elif decision in KEEP_DECISIONS:
            main_blocks.append(normalized)
        # Fall back to the AI category only when there is no recognized decision.
        elif category == "main data":
            main_blocks.append(normalized)
        elif category in {"context", "metadata", "footnotes", "footnote"}:
            context_blocks.append(normalized)

    return main_blocks, context_blocks


def _resolve_union_bounds(
    blocks: List[Dict[str, Any]],
    total_rows: Optional[int] = None,
    total_cols: Optional[int] = None,
) -> Dict[str, int]:
    if not blocks:
        max_row = max((total_rows or 1) - 1, 0)
        max_col = max((total_cols or 1) - 1, 0)
        return {
            "start_row": 0,
            "end_row": max_row,
            "start_col": 0,
            "end_col": max_col,
        }

    return {
        "start_row": min(b["coordinates"]["start_row"] for b in blocks),
        "end_row": max(b["coordinates"]["end_row"] for b in blocks),
        "start_col": min(b["coordinates"]["start_col"] for b in blocks),
        "end_col": max(b["coordinates"]["end_col"] for b in blocks),
    }


def _overlap_ratio(a: Dict[str, int], b: Dict[str, int]) -> float:
    left = max(a["start_col"], b["start_col"])
    right = min(a["end_col"], b["end_col"])
    overlap = max(0, right - left + 1)
    width_a = max(1, a["end_col"] - a["start_col"] + 1)
    width_b = max(1, b["end_col"] - b["start_col"] + 1)
    return overlap / max(width_a, width_b)


def _infer_header_row_from_context(main_blocks: List[Dict[str, Any]], context_blocks: List[Dict[str, Any]]) -> Optional[int]:
    if not main_blocks or not context_blocks:
        return None

    main_bounds = _resolve_union_bounds(main_blocks)
    main_start_row = main_bounds["start_row"]
    best_header: Optional[int] = None

    for block in context_blocks:
        c = block.get("coordinates", {})
        c_start = _safe_int(c.get("start_row"))
        c_end = _safe_int(c.get("end_row"))
        c_height = c_end - c_start + 1
        contiguous_above = c_end + 1 == main_start_row
        similar_span = _overlap_ratio(c, main_bounds) >= 0.9
        compact_header_band = c_height <= 2
        if contiguous_above and similar_span and compact_header_band:
            if best_header is None or c_start < best_header:
                best_header = c_start

    return best_header


def build_scoped_source(
    sheet_name: Optional[str],
    total_rows: Optional[int] = None,
    total_cols: Optional[int] = None,
    blocks: Optional[List[Dict[str, Any]]] = None,
    user_selected_header_row: Optional[int] = None,
) -> Dict[str, Any]:
    main_blocks, context_blocks = _classify_blocks(blocks)
    analysis_bounds = _resolve_union_bounds(main_blocks, total_rows=total_rows, total_cols=total_cols)
    inferred_header_row = _infer_header_row_from_context(main_blocks, context_blocks)

    if user_selected_header_row is not None:
        header_row = _safe_int(user_selected_header_row)
    elif inferred_header_row is not None:
        header_row = inferred_header_row
    elif main_blocks:
        header_row = min(b["coordinates"]["header_row"] for b in main_blocks)
    else:
        header_row = 0

    # If a context/header band was inferred directly above main data, include it in scope.
    if header_row < analysis_bounds["start_row"]:
        analysis_bounds["start_row"] = header_row

    scope_type = "demarcated_table" if main_blocks else "full_sheet"
    source_rows = max(analysis_bounds["end_row"] - analysis_bounds["start_row"] + 1, 0)
    source_cols = max(analysis_bounds["end_col"] - analysis_bounds["start_col"] + 1, 0)

    return {
        "sheet_name": sheet_name,
        "scope_type": scope_type,
        "requires_extraction": bool(main_blocks),
        "header_row": header_row,
        "sheet_frame": {
            "rows": total_rows,
            "cols": total_cols,
        },
        "analysis_bounds": analysis_bounds,
        "source_frame": {
            "rows": source_rows,
            "cols": source_cols,
        },
        "main_blocks": main_blocks,
        "context_blocks": context_blocks,
    }


def layout_crop_origin(scoped_source: Optional[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    """
    When ``load_file`` crops the working grid to ``analysis_bounds``, return the
    absolute sheet (row, col) that maps to index 0 in that grid. Otherwise ``None``.
    """
    if not isinstance(scoped_source, dict):
        return None
    bounds = scoped_source.get("absolute_bounds") or scoped_source.get("analysis_bounds")
    if not isinstance(bounds, dict):
        return None
    # ``absolute_bounds`` is set only when the working grid was cropped in load_file.
    if not scoped_source.get("absolute_bounds") and not scoped_source.get("requires_extraction"):
        return None
    return (
        _safe_int(bounds.get("start_row", 0), 0),
        _safe_int(bounds.get("start_col", 0), 0),
    )


def header_row_absolute(scoped_source: Optional[Dict[str, Any]]) -> Optional[int]:
    """Approved header row in absolute sheet coordinates (demarcation / scoped_source)."""
    if not isinstance(scoped_source, dict):
        return None
    if scoped_source.get("header_row") is not None:
        return _safe_int(scoped_source.get("header_row"), 0)
    for block in scoped_source.get("main_blocks") or []:
        if not isinstance(block, dict):
            continue
        original = block.get("original_coordinates")
        if isinstance(original, dict) and original.get("header_row") is not None:
            return _safe_int(original.get("header_row"), 0)
        coords = block.get("coordinates", block) if isinstance(block.get("coordinates"), dict) else {}
        if coords.get("header_row") is not None:
            rel = _safe_int(coords.get("header_row"), 0)
            bounds = scoped_source.get("absolute_bounds") or scoped_source.get("analysis_bounds") or {}
            row_off = _safe_int(bounds.get("start_row", 0), 0) if isinstance(bounds, dict) else 0
            return rel + row_off
    bounds = scoped_source.get("analysis_bounds")
    if isinstance(bounds, dict):
        return _safe_int(bounds.get("start_row", 0), 0)
    return None


def analysis_bounds_absolute(scoped_source: Optional[Dict[str, Any]]) -> Dict[str, int]:
    scoped = scoped_source if isinstance(scoped_source, dict) else {}
    bounds = scoped.get("absolute_bounds") or scoped.get("analysis_bounds") or {}
    if not isinstance(bounds, dict):
        bounds = {}
    return {
        "start_row": _safe_int(bounds.get("start_row", 0), 0),
        "end_row": _safe_int(bounds.get("end_row", bounds.get("start_row", 0)), 0),
        "start_col": _safe_int(bounds.get("start_col", 0), 0),
        "end_col": _safe_int(bounds.get("end_col", bounds.get("start_col", 0)), 0),
    }


def resolve_layout_extract_params(
    params: Optional[Dict[str, Any]],
    scoped_source: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Build ``layout.extract`` indices for the scoped working grid.

    Planner / structure-analysis params may set ``header_row`` to the first data row
    (e.g. TikTok). Prefer demarcation ``scoped_source.header_row`` and crop origin.
    """
    bounds = analysis_bounds_absolute(scoped_source)
    origin = layout_crop_origin(scoped_source)
    row_off, col_off = origin if origin is not None else (0, 0)

    header_abs = header_row_absolute(scoped_source)
    if header_abs is None and isinstance(params, dict) and params.get("header_row") is not None:
        header_abs = _safe_int(params.get("header_row"), 0) + row_off

    out: Dict[str, Any] = {
        "start_row": max(0, bounds["start_row"] - row_off),
        "end_row": max(0, bounds["end_row"] - row_off),
        "start_col": max(0, bounds["start_col"] - col_off),
        "end_col": max(0, bounds["end_col"] - col_off),
    }
    if header_abs is not None:
        out["header_row"] = max(0, header_abs - row_off)
    elif isinstance(params, dict) and params.get("header_row") is not None:
        out["header_row"] = max(0, _safe_int(params.get("header_row"), 0) - row_off)
    else:
        out["header_row"] = out["start_row"]

    return out


def rebase_layout_extract_params(
    params: Optional[Dict[str, Any]],
    scoped_source: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map absolute sheet indices in ``layout.extract`` params to the scoped grid."""
    return resolve_layout_extract_params(params, scoped_source)


def rebase_layout_stack_params(
    params: Optional[Dict[str, Any]],
    scoped_source: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Rebase ``layout.stack`` header_row and per-block coords to the scoped grid."""
    out = dict(params or {})
    origin = layout_crop_origin(scoped_source)
    if origin is None:
        return out
    row_off, col_off = origin
    if "header_row" in out and out["header_row"] is not None:
        out["header_row"] = max(0, _safe_int(out["header_row"], 0) - row_off)
    blocks = out.get("blocks")
    if not isinstance(blocks, list):
        return out
    rebased: List[Any] = []
    for block in blocks:
        if not isinstance(block, dict):
            rebased.append(block)
            continue
        nb = dict(block)
        for key in (
            "start_row",
            "end_row",
            "row_start",
            "row_end",
            "header_row",
            "data_start_row",
            "data_end_row",
        ):
            if key in nb and nb[key] is not None:
                nb[key] = max(0, _safe_int(nb[key], 0) - row_off)
        for key in ("start_col", "end_col", "col_start", "col_end"):
            if key in nb and nb[key] is not None:
                nb[key] = max(0, _safe_int(nb[key], 0) - col_off)
        rebased.append(nb)
    out["blocks"] = rebased
    return out


def rebase_layout_grid_tool_params(
    tool_name: str,
    params: Optional[Dict[str, Any]],
    scoped_source: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Rebase grid-indexed layout tool params after ``apply_scoped_source_to_grid``."""
    norm = str(tool_name or "").strip().lower()
    if norm in ("layout.extract", "extract_data_block", "get_data_block", "extract_table", "xls.layout.extract"):
        return rebase_layout_extract_params(params, scoped_source)
    if norm in ("layout.stack", "stack_tables", "merge_blocks", "xls.layout.stack"):
        return rebase_layout_stack_params(params, scoped_source)
    return dict(params or {})


def _rebase_blocks(blocks: List[Dict[str, Any]], row_offset: int, col_offset: int) -> List[Dict[str, Any]]:
    rebased: List[Dict[str, Any]] = []
    for block in blocks:
        coords = dict(block.get("coordinates", {}))
        rc: Dict[str, Any] = {
            "start_row": coords.get("start_row", 0) - row_offset,
            "end_row": coords.get("end_row", 0) - row_offset,
            "start_col": coords.get("start_col", 0) - col_offset,
            "end_col": coords.get("end_col", 0) - col_offset,
            "header_row": coords.get("header_row", 0) - row_offset,
        }
        if coords.get("header_row_end") is not None:
            try:
                rc["header_row_end"] = int(coords["header_row_end"]) - row_offset
            except (TypeError, ValueError):
                pass
        if coords.get("header_mode") is not None:
            rc["header_mode"] = coords["header_mode"]
        rebased.append({
            **block,
            "coordinates": rc,
            "original_coordinates": coords,
        })
    return rebased


def apply_scoped_source_to_grid(
    grid: VisualGrid,
    scoped_source: Optional[Dict[str, Any]],
) -> Tuple[VisualGrid, Dict[str, Any]]:
    base_scope = build_scoped_source(
        sheet_name=grid.sheet_name,
        total_rows=grid.total_rows,
        total_cols=grid.total_cols,
        blocks=(scoped_source or {}).get("main_blocks") or (scoped_source or {}).get("blocks"),
        user_selected_header_row=(scoped_source or {}).get("header_row"),
    )

    if scoped_source:
        base_scope.update({k: v for k, v in scoped_source.items() if k not in {"main_blocks", "context_blocks", "analysis_bounds", "source_frame", "sheet_frame"}})
        if scoped_source.get("context_blocks"):
            base_scope["context_blocks"] = scoped_source["context_blocks"]

    if not base_scope.get("requires_extraction"):
        return grid, base_scope

    bounds = base_scope["analysis_bounds"]
    scoped_grid = grid.get_subgrid(
        bounds["start_row"],
        bounds["end_row"],
        bounds["start_col"],
        bounds["end_col"],
    )

    active_scope = {
        **base_scope,
        "absolute_bounds": bounds,
        "main_blocks": _rebase_blocks(base_scope.get("main_blocks", []), bounds["start_row"], bounds["start_col"]),
        "source_frame": {
            "rows": scoped_grid.total_rows,
            "cols": scoped_grid.total_cols,
        },
    }
    return scoped_grid, active_scope


def _load_raw_dataframe(file_path: str, sheet_name: Optional[str]) -> pd.DataFrame:
    if str(file_path).lower().endswith(".csv"):
        return pd.read_csv(file_path, header=None)
    return pd.read_excel(file_path, sheet_name=sheet_name or 0, header=None)


def load_raw_sheet_dataframe(file_path: str, sheet_name: Optional[str]) -> pd.DataFrame:
    """Load the full sheet (or CSV) with no header row. Prefer reusing one load per (path, sheet) when iterating many blocks."""
    return _load_raw_dataframe(file_path, sheet_name)


_EXCEL_A1_RANGE_RE = re.compile(
    r"^\$?([A-Za-z]+)\$?(\d+):\$?([A-Za-z]+)\$?(\d+)$"
)


def _excel_col_to_index(col_letters: str) -> int:
    """Convert Excel column letters to 0-based index."""
    from openpyxl.utils import column_index_from_string

    return int(column_index_from_string(col_letters)) - 1


def _parse_excel_range_a1(range_str: str) -> Optional[Tuple[int, int, int, int]]:
    """Parse ``A1:C5`` into 0-based inclusive (min_row, max_row, min_col, max_col)."""
    m = _EXCEL_A1_RANGE_RE.match(str(range_str or "").strip())
    if not m:
        return None
    c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    min_row = min(r1, r2) - 1
    max_row = max(r1, r2) - 1
    min_col = min(_excel_col_to_index(c1), _excel_col_to_index(c2))
    max_col = max(_excel_col_to_index(c1), _excel_col_to_index(c2))
    return min_row, max_row, min_col, max_col


def _load_workbook_merged_ranges(
    file_path: str,
    sheet_name: Optional[str],
) -> List[Dict[str, Any]]:
    """Read vertical/horizontal merged ranges from an Excel workbook (empty for CSV)."""
    if str(file_path).lower().endswith(".csv"):
        return []
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        logger.warning("openpyxl unavailable for merged ranges: %s", exc)
        return []

    # merged_cells is not available on ReadOnlyWorksheet — use normal (non-read-only) load.
    wb = None
    try:
        wb = load_workbook(file_path, data_only=True, read_only=False)
    except Exception as exc:
        logger.warning("Could not load workbook for merged ranges: %s", exc)
        return []

    try:
        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
        else:
            ws = None
            for sn in wb.sheetnames:
                if wb[sn].sheet_state == "visible":
                    ws = wb[sn]
                    break
            if ws is None and wb.sheetnames:
                ws = wb[wb.sheetnames[0]]
        if ws is None:
            return []

        merged_ranges_attr = getattr(ws, "merged_cells", None)
        if merged_ranges_attr is None:
            logger.warning(
                "Worksheet %r has no merged_cells attribute; skipping merged range detection.",
                getattr(ws, "title", sheet_name),
            )
            return []

        out: List[Dict[str, Any]] = []
        for mr in merged_ranges_attr.ranges:
            parsed = _parse_excel_range_a1(str(mr))
            if not parsed:
                continue
            min_row, max_row, min_col, max_col = parsed
            rows_spanned = max_row - min_row + 1
            cols_spanned = max_col - min_col + 1
            if rows_spanned < 2 and cols_spanned < 2:
                continue
            top_left = ws.cell(row=min_row + 1, column=min_col + 1).value
            out.append(
                {
                    "range": str(mr),
                    "min_row": min_row,
                    "max_row": max_row,
                    "min_col": min_col,
                    "max_col": max_col,
                    "rows_spanned": rows_spanned,
                    "cols_spanned": cols_spanned,
                    "top_left_value": top_left,
                }
            )
        return out
    except Exception as exc:
        logger.warning("Merged range scan failed (non-fatal): %s", exc)
        return []
    finally:
        if wb is not None:
            wb.close()


def map_merged_ranges_to_prepared_df(
    merged_ranges: List[Dict[str, Any]],
    *,
    bounds: Dict[str, int],
    data_first_row_abs: int,
    column_names: List[str],
) -> List[Dict[str, Any]]:
    """
    Map workbook merged ranges into prepared-dataframe row/column coordinates.

    Returns entries with ``excel_range``, ``column_name``, ``start_row``, ``end_row``
    (0-based, end exclusive) in the prepared dataframe.

    Includes **all** vertical merges (date, dimension labels, spend). Downstream
    ``expand_grouped_block`` uses spend-column spans for allocation and
    non-metric spans to forward-fill dimensions inside Excel merge geometry.
    """
    if not merged_ranges or not column_names:
        return []

    start_col = _safe_int(bounds.get("start_col", 0))
    end_row_bound = _safe_int(bounds.get("end_row", 10**9))
    mapped: List[Dict[str, Any]] = []

    for entry in merged_ranges:
        if not isinstance(entry, dict):
            continue
        min_row = _safe_int(entry.get("min_row"), -1)
        max_row = _safe_int(entry.get("max_row"), -1)
        min_col = _safe_int(entry.get("min_col"), -1)
        max_col = _safe_int(entry.get("max_col"), -1)
        rows_spanned = max(max_row - min_row + 1, 1)
        cols_spanned = max(max_col - min_col + 1, 1)

        if rows_spanned < 2:
            continue
        if min_row > end_row_bound or max_row < data_first_row_abs:
            continue

        col_rel = min_col - start_col
        if col_rel < 0 or col_rel >= len(column_names):
            continue

        seg_start_abs = max(min_row, data_first_row_abs)
        seg_end_abs = min(max_row, end_row_bound)
        if seg_end_abs < seg_start_abs:
            continue

        df_start = seg_start_abs - data_first_row_abs
        df_end = seg_end_abs - data_first_row_abs + 1
        if df_end <= df_start:
            continue

        mapped.append(
            {
                "excel_range": str(entry.get("range") or ""),
                "column_name": str(column_names[col_rel]),
                "start_row": int(df_start),
                "end_row": int(df_end),
                "rows_spanned": int(seg_end_abs - seg_start_abs + 1),
                "cols_spanned": int(cols_spanned),
                "top_left_value": entry.get("top_left_value"),
            }
        )
    return mapped


def reconcile_merged_metric_ranges_to_columns(
    merged_ranges: List[Dict[str, Any]],
    column_names: List[Any],
) -> List[Dict[str, Any]]:
    """
    Keep persisted merge spans when the active workbook has no Excel merges
    (e.g. materialized clean templates). Drops entries whose metric column is gone.
    """
    if not merged_ranges or not column_names:
        return []
    exact = {str(c).strip(): str(c) for c in column_names if c is not None and str(c).strip()}
    by_lower = {k.casefold(): v for k, v in exact.items()}
    out: List[Dict[str, Any]] = []
    for spec in merged_ranges:
        if not isinstance(spec, dict):
            continue
        raw = str(spec.get("column_name") or spec.get("metric_col") or "").strip()
        if not raw:
            continue
        resolved = exact.get(raw) or by_lower.get(raw.casefold())
        if not resolved:
            continue
        try:
            start_row = int(spec.get("start_row", 0))
            end_row = int(spec.get("end_row", 0))
        except (TypeError, ValueError):
            continue
        if end_row <= start_row:
            continue
        out.append(
            {
                **dict(spec),
                "column_name": resolved,
                "start_row": start_row,
                "end_row": end_row,
            }
        )
    return out


def detect_merged_metric_ranges_for_prepared_df(
    file_path: str,
    sheet_name: Optional[str],
    *,
    bounds: Dict[str, int],
    data_first_row_abs: int,
    column_names: List[str],
    prior_ranges: Optional[List[Dict[str, Any]]] = None,
    alternate_workbook_path: Optional[str] = None,
    alternate_sheet_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Map Excel vertical merges onto prepared-dataframe coordinates.

    When the active file has no merges (materialized clean workbook), reuse
    ``prior_ranges`` from the original scoped source if column names still match.
    """
    if str(file_path).lower().endswith(".csv"):
        return reconcile_merged_metric_ranges_to_columns(
            list(prior_ranges or []), column_names
        )

    mapped: List[Dict[str, Any]] = []
    try:
        raw_merged = _load_workbook_merged_ranges(file_path, sheet_name)
        mapped = map_merged_ranges_to_prepared_df(
            raw_merged,
            bounds=bounds,
            data_first_row_abs=int(data_first_row_abs),
            column_names=[str(c) for c in column_names],
        )
    except Exception as exc:
        logger.warning(
            "Merged metric range scan failed for %s (non-fatal): %s",
            file_path,
            exc,
            exc_info=True,
        )

    if not mapped and alternate_workbook_path and not str(alternate_workbook_path).lower().endswith(
        ".csv"
    ):
        try:
            alt_sheet = alternate_sheet_name or sheet_name
            raw_merged = _load_workbook_merged_ranges(alternate_workbook_path, alt_sheet)
            mapped = map_merged_ranges_to_prepared_df(
                raw_merged,
                bounds=bounds,
                data_first_row_abs=int(data_first_row_abs),
                column_names=[str(c) for c in column_names],
            )
        except Exception as exc:
            logger.warning(
                "Alternate merged range scan failed for %s (non-fatal): %s",
                alternate_workbook_path,
                exc,
                exc_info=True,
            )

    if mapped:
        return mapped
    return reconcile_merged_metric_ranges_to_columns(
        list(prior_ranges or []), column_names
    )


def load_scoped_dataframe(
    file_path: str,
    sheet_name: Optional[str],
    scoped_source: Optional[Dict[str, Any]],
    raw_df: Optional[pd.DataFrame] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if raw_df is None:
        raw_df = _load_raw_dataframe(file_path, sheet_name)
    resolved_scope = build_scoped_source(
        sheet_name=sheet_name,
        total_rows=len(raw_df),
        total_cols=len(raw_df.columns),
        blocks=(scoped_source or {}).get("main_blocks") or (scoped_source or {}).get("blocks"),
        user_selected_header_row=(scoped_source or {}).get("header_row"),
    )

    if scoped_source:
        if scoped_source.get("context_blocks"):
            resolved_scope["context_blocks"] = scoped_source["context_blocks"]
        for key in ("scope_type", "requires_extraction"):
            if key in scoped_source:
                resolved_scope[key] = scoped_source[key]

    bounds = resolved_scope["analysis_bounds"]
    scoped_df = raw_df.iloc[
        bounds["start_row"]:bounds["end_row"] + 1,
        bounds["start_col"]:bounds["end_col"] + 1,
    ].copy()

    if scoped_df.empty:
        return pd.DataFrame(), resolved_scope

    mb0 = resolved_scope.get("main_blocks") or []
    user_multi = False
    header_row_abs = _safe_int(resolved_scope.get("header_row", 0))
    header_row_end_abs: Optional[int] = None
    if mb0:
        c0 = mb0[0].get("coordinates") or {}
        mode = str(c0.get("header_mode") or "single").strip().lower()
        if mode == "multi" and c0.get("header_row_end") is not None:
            user_multi = True
            header_row_abs = _safe_int(c0.get("header_row", header_row_abs))
            header_row_end_abs = _safe_int(c0.get("header_row_end"))
            if header_row_end_abs < header_row_abs:
                header_row_end_abs = header_row_abs
            resolved_scope["header_row"] = header_row_abs

    header_row_rel = max(header_row_abs - bounds["start_row"], 0)
    header_row_rel = min(header_row_rel, len(scoped_df) - 1)

    width = len(scoped_df.columns)
    row_top = scoped_df.iloc[header_row_rel]

    use_two_row_header = (
        not user_multi
        and _multi_level_header_candidate(row_top)
        and header_row_rel + 1 < len(scoped_df)
        and _is_probable_subheader_row(scoped_df.iloc[header_row_rel + 1], width)
    )

    data_first_row_abs = bounds["start_row"] + header_row_rel + 1

    if user_multi and header_row_end_abs is not None:
        hre_rel = header_row_end_abs - bounds["start_row"]
        hre_rel = min(max(hre_rel, header_row_rel), len(scoped_df) - 1)
        data_first_row_abs = bounds["start_row"] + hre_rel + 1
        merged_labels, col_derived = _merge_header_row_range_to_labels(
            scoped_df, header_row_rel, hre_rel, sep="_"
        )
        ucols, uderived = _make_unique_columns(merged_labels, col_derived)
        prepared_df = scoped_df.iloc[hre_rel + 1 :].copy()
        prepared_df.columns = ucols
        prepared_df = prepared_df.reset_index(drop=True)
        excel_rows = list(range(bounds["start_row"] + header_row_rel, bounds["start_row"] + hre_rel + 1))
        dmap = {str(ucols[i]): uderived[i] for i in range(len(ucols))}
        header_derivation = {
            "derived": any(uderived),
            "derived_by_column": dmap,
            "format": "user_multi_row",
            "separator": "_",
            "excel_rows_merged_0based": excel_rows,
            "user_selected": True,
            "message": (
                f"Merged Excel rows {excel_rows[0] + 1}–{excel_rows[-1] + 1} into column names (multi-header mode). "
                f"\"Derived\" applies only to columns where multiple header levels were combined."
            ),
        }
        logger.info("User multi-row header merge: %s", header_derivation.get("message"))
    elif use_two_row_header:
        data_first_row_abs = bounds["start_row"] + header_row_rel + 2
        row_sub = scoped_df.iloc[header_row_rel + 1]
        merged_labels, col_derived = _merge_two_header_rows_to_labels(row_top, row_sub, sep="_")
        ucols, uderived = _make_unique_columns(merged_labels, col_derived)
        prepared_df = scoped_df.iloc[header_row_rel + 2:].copy()
        prepared_df.columns = ucols
        prepared_df = prepared_df.reset_index(drop=True)
        excel_r1 = bounds["start_row"] + header_row_rel
        excel_r2 = excel_r1 + 1
        _t_sub, _n_sub, f_sub = _row_text_numeric_filled(row_sub)
        sub_text_ratio = round(_t_sub / f_sub, 4) if f_sub else 0.0
        dmap = {str(ucols[i]): uderived[i] for i in range(len(ucols))}
        header_derivation = {
            "derived": any(uderived),
            "derived_by_column": dmap,
            "format": "row1_row2",
            "separator": "_",
            "excel_rows_merged_0based": [excel_r1, excel_r2],
            "primary_blank_ratio": round(_row_blank_ratio(row_top), 4),
            "subheader_text_ratio": sub_text_ratio,
            "message": (
                f"Merged Excel rows {excel_r1 + 1} and {excel_r2 + 1} into column names where applicable. "
                f"\"Derived\" marks columns whose names combine both rows; single-row labels are not derived."
            ),
        }
        logger.info("Multi-level header merge applied: %s", header_derivation.get("message"))
    else:
        raw_columns = list(scoped_df.iloc[header_row_rel])
        ucols, uderived = _make_unique_columns(raw_columns, None)
        prepared_df = scoped_df.iloc[header_row_rel + 1:].copy()
        prepared_df.columns = ucols
        prepared_df = prepared_df.reset_index(drop=True)
        dmap = {str(ucols[i]): uderived[i] for i in range(len(ucols))}
        header_derivation = {"derived": False, "derived_by_column": dmap}

    resolved_scope["header_derivation"] = header_derivation
    resolved_scope["source_frame"] = {
        "rows": len(scoped_df),
        "cols": len(scoped_df.columns),
    }
    resolved_scope["prepared_frame"] = {
        "rows": len(prepared_df),
        "cols": len(prepared_df.columns),
    }

    prior_merged = list((scoped_source or {}).get("merged_metric_ranges") or [])
    alt_path = str((scoped_source or {}).get("source_workbook_path") or "").strip() or None
    alt_sheet = str((scoped_source or {}).get("source_sheet_name") or "").strip() or None
    resolved_scope["merged_metric_ranges"] = detect_merged_metric_ranges_for_prepared_df(
        file_path,
        sheet_name,
        bounds=bounds,
        data_first_row_abs=int(data_first_row_abs),
        column_names=[str(c) for c in prepared_df.columns],
        prior_ranges=prior_merged,
        alternate_workbook_path=alt_path,
        alternate_sheet_name=alt_sheet,
    )

    return prepared_df, resolved_scope
