"""
Sheet layout diagnostics for Guided Setup.

Python facts (shape, merges, date/KPI axes, connectivity) run before flood-fill
so flowchart/crosstab sheets become one Main Data region instead of 1-cell islands.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_MONTH_NAMES = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august",
    "sep", "sept", "september", "oct", "october", "nov", "november",
    "dec", "december",
}
_WEEK_RE = re.compile(r"^(w|wk|week)\s*\d+$", re.I)
_KPI_RE = re.compile(
    r"\b(grp|grps|reach|ots|spend|spends|cost|costs|impression|impressions|"
    r"contact|contacts|budget|net\s*reach|potential|cpi|cpm|frequency)\b",
    re.I,
)
_TOTAL_RE = re.compile(r"\b(total|totals|subtotal|grand\s*total|sum)\b", re.I)
_NOTE_RE = re.compile(r"\b(note|notes|remark|comment|internal\s*use|confidential)\b", re.I)


def format_cell_display(value: Any, number_format: str = "") -> str:
    """Excel-like text for the minimap: percents, trimmed floats, no IEEE junk."""
    if value is None:
        return ""
    try:
        if isinstance(value, float) and pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    fmt = str(number_format or "")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
        if "%" in fmt:
            pct = num * 100.0
            rounded = round(pct, 4)
            if abs(rounded - round(rounded)) < 1e-6:
                return f"{int(round(rounded))}%"
            return f"{rounded:.4f}".rstrip("0").rstrip(".") + "%"
        if abs(num - round(num)) < 1e-9 and abs(num) < 1e15:
            return str(int(round(num)))
        return format(num, ".12g")
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        return text[:-2]
    return text


def _cell_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return bool(pd.isna(value))
    except (ValueError, TypeError):
        return False


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return not (isinstance(value, float) and pd.isna(value))
    text = _cell_text(value).replace(",", "").replace("%", "").replace("€", "").replace("$", "")
    if not text:
        return False
    try:
        float(text)
        return True
    except ValueError:
        return False


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _cell_text(value).lower()).strip()


def _looks_month(value: Any) -> bool:
    tok = _token(value)
    if not tok:
        return False
    first = tok.split()[0]
    return first in _MONTH_NAMES or tok in _MONTH_NAMES


def _looks_week(value: Any) -> bool:
    tok = _token(value).replace(" ", "")
    return bool(_WEEK_RE.match(tok)) or bool(re.match(r"^w\d{1,2}$", tok, re.I))


def _looks_week_index(value: Any) -> bool:
    """Month-week numbers (1–5) or ISO week numbers (1–53), including bare 1/2/3 under January."""
    if _looks_week(value):
        return True
    if isinstance(value, bool):
        return False
    number = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and pd.isna(value):
            return False
        if float(value) != int(value):
            return False
        number = int(value)
    else:
        text = _cell_text(value)
        if re.fullmatch(r"[1-9]|[1-4]\d|5[0-3]", text):
            number = int(text)
    return number is not None and 1 <= number <= 53


def _looks_period_date(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (datetime, date)):
        return True
    try:
        if isinstance(value, pd.Timestamp) and not pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    text = _cell_text(value)
    return bool(re.match(r"^\d{4}-\d{1,2}-\d{1,2}", text))


def _looks_kpi(value: Any) -> bool:
    return bool(_KPI_RE.search(_cell_text(value)))


def _looks_total(value: Any) -> bool:
    return bool(_TOTAL_RE.search(_cell_text(value)))


def _nonempty_mask(df: pd.DataFrame) -> List[List[bool]]:
    rows, cols = df.shape
    mask: List[List[bool]] = []
    for r in range(rows):
        row_mask = []
        for c in range(cols):
            row_mask.append(not _is_empty(df.iat[r, c]))
        mask.append(row_mask)
    return mask


def _connected_components(mask: List[List[bool]]) -> List[List[Tuple[int, int]]]:
    if not mask:
        return []
    rows = len(mask)
    cols = len(mask[0]) if rows else 0
    visited = [[False] * cols for _ in range(rows)]
    components: List[List[Tuple[int, int]]] = []
    for row in range(rows):
        for col in range(cols):
            if not mask[row][col] or visited[row][col]:
                continue
            stack = [(row, col)]
            visited[row][col] = True
            component: List[Tuple[int, int]] = []
            while stack:
                cr, cc = stack.pop()
                component.append((cr, cc))
                for nr, nc in ((cr - 1, cc), (cr + 1, cc), (cr, cc - 1), (cr, cc + 1)):
                    if 0 <= nr < rows and 0 <= nc < cols and mask[nr][nc] and not visited[nr][nc]:
                        visited[nr][nc] = True
                        stack.append((nr, nc))
            components.append(component)
    return components


def _data_region(df: pd.DataFrame) -> Optional[Dict[str, int]]:
    rows, cols = df.shape
    first_r, last_r, first_c, last_c = rows, -1, cols, -1
    filled = 0
    for r in range(rows):
        for c in range(cols):
            if _is_empty(df.iat[r, c]):
                continue
            filled += 1
            first_r = min(first_r, r)
            last_r = max(last_r, r)
            first_c = min(first_c, c)
            last_c = max(last_c, c)
    if filled == 0:
        return None
    return {
        "start_row": first_r,
        "end_row": last_r,
        "start_col": first_c,
        "end_col": last_c,
        "rows": last_r - first_r + 1,
        "cols": last_c - first_c + 1,
        "filled": filled,
    }


def _blank_ratios(df: pd.DataFrame) -> Tuple[float, float]:
    rows, cols = df.shape
    if rows == 0 or cols == 0:
        return 0.0, 0.0
    empty_rows = sum(
        1 for r in range(rows) if all(_is_empty(df.iat[r, c]) for c in range(cols))
    )
    empty_cols = sum(
        1 for c in range(cols) if all(_is_empty(df.iat[r, c]) for r in range(rows))
    )
    return round(empty_rows / rows, 3), round(empty_cols / cols, 3)


def _scan_header_time_axis(df: pd.DataFrame) -> Dict[str, Any]:
    rows, cols = df.shape
    scan_rows = min(rows, 16)
    best: Dict[str, Any] = {
        "date_axis": None,
        "time_kind": None,
        "header_rows": [],
        "month_like_count": 0,
        "week_like_count": 0,
    }
    row_stats: List[Dict[str, int]] = []
    for r in range(scan_rows):
        months = weeks = week_idx = dates = 0
        for c in range(cols):
            val = df.iat[r, c]
            if _looks_month(val):
                months += 1
            if _looks_week(val):
                weeks += 1
            if _looks_week_index(val):
                week_idx += 1
            if _looks_period_date(val):
                dates += 1
        row_stats.append({
            "months": months,
            "weeks": weeks,
            "week_idx": week_idx,
            "dates": dates,
        })
        if months >= 3 or weeks >= 3:
            best["date_axis"] = "columns"
            best["time_kind"] = "period_headers"
            best["header_rows"].append(r)
            best["month_like_count"] = max(best["month_like_count"], months)
            best["week_like_count"] = max(best["week_like_count"], weeks)

    if best["header_rows"]:
        month_rows = set(best["header_rows"])
        extra: List[int] = []
        for r, stats in enumerate(row_stats):
            if r in month_rows:
                continue
            if not any(abs(r - mr) <= 3 for mr in month_rows):
                continue
            if stats["week_idx"] >= 3 or stats["weeks"] >= 3 or stats["dates"] >= 3:
                extra.append(r)
                best["week_like_count"] = max(
                    best["week_like_count"], stats["week_idx"], stats["weeks"]
                )
        best["header_rows"] = sorted(set(best["header_rows"]) | set(extra))
        return best

    # Tall table: a column whose values mostly look like dates (ISO / serial-ish strings).
    scan_cols = min(cols, 8)
    date_like_re = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")
    for c in range(scan_cols):
        parsed = nonempty = 0
        for r in range(min(rows, 40)):
            val = df.iat[r, c]
            if _is_empty(val) or _is_numeric(val):
                continue
            nonempty += 1
            text = _cell_text(val)
            if date_like_re.match(text) or _looks_month(val):
                parsed += 1
        if nonempty >= 5 and parsed / max(nonempty, 1) >= 0.6:
            best["date_axis"] = "column"
            best["time_kind"] = "point_in_time"
            best["date_column"] = c
            break
    return best


def _scan_kpi_axis(df: pd.DataFrame, body_start_row: int) -> Dict[str, Any]:
    rows, cols = df.shape
    stub_cols = min(cols, 8)
    best_col = None
    best_hits = 0
    for c in range(stub_cols):
        hits = 0
        for r in range(body_start_row, rows):
            if _looks_kpi(df.iat[r, c]):
                hits += 1
        if hits > best_hits:
            best_hits = hits
            best_col = c
    if best_col is not None and best_hits >= 2:
        return {
            "metric_name_axis": "rows",
            "metric_column": best_col,
            "kpi_hits": best_hits,
        }
    header_hits = 0
    header_row = None
    for r in range(min(rows, 12)):
        hits = sum(1 for c in range(cols) if _looks_kpi(df.iat[r, c]))
        if hits > header_hits:
            header_hits = hits
            header_row = r
    if header_hits >= 2:
        return {
            "metric_name_axis": "columns",
            "metric_header_row": header_row,
            "kpi_hits": header_hits,
        }
    return {"metric_name_axis": None, "kpi_hits": 0}


def _scan_stub_dimensions(df: pd.DataFrame, body_start_row: int, metric_col: Optional[int]) -> Dict[str, Any]:
    rows, cols = df.shape
    stub: List[int] = []
    limit = min(cols, 6)
    for c in range(limit):
        if metric_col is not None and c == metric_col:
            continue
        text_n = num_n = 0
        for r in range(body_start_row, min(rows, body_start_row + 40)):
            val = df.iat[r, c]
            if _is_empty(val):
                continue
            if _is_numeric(val):
                num_n += 1
            else:
                text_n += 1
        if text_n >= 2 and text_n >= num_n:
            stub.append(c)
    return {"dimension_axis": "rows" if stub else None, "stub_columns": stub}


def _find_totals(df: pd.DataFrame) -> Dict[str, Any]:
    """Label total columns from headers; do not treat a filled Totals column as a total on every row."""
    rows, cols = df.shape
    total_cols: List[int] = []
    for c in range(cols):
        header_hits = sum(1 for r in range(min(rows, 12)) if _looks_total(df.iat[r, c]))
        if header_hits:
            total_cols.append(c)
    total_col_set = set(total_cols)
    total_rows: List[int] = []
    for r in range(rows):
        if any(
            _looks_total(df.iat[r, c]) and c not in total_col_set
            for c in range(min(cols, 12))
        ):
            total_rows.append(r)
    return {"total_rows": total_rows[:8], "total_cols": total_cols[:8]}


def _looks_comment(value: Any) -> bool:
    return bool(_NOTE_RE.search(_cell_text(value)))


def _title_band_last_col(
    df: pd.DataFrame,
    start_row: int,
    title_end: int,
    start_col: int,
    end_col: int,
) -> int:
    last = start_col
    for r in range(int(start_row), int(title_end) + 1):
        if r >= df.shape[0]:
            break
        for c in range(int(start_col), min(int(end_col) + 1, df.shape[1])):
            if not _is_empty(df.iat[r, c]):
                last = max(last, c)
    return last


def _title_band_end(df: pd.DataFrame, header_rows: Sequence[int]) -> Optional[int]:
    if header_rows:
        end = min(header_rows) - 1
        return end if end >= 0 else None
    rows, cols = df.shape
    end = -1
    for r in range(min(rows, 10)):
        texts = nums = 0
        for c in range(min(cols, 12)):
            val = df.iat[r, c]
            if _is_empty(val):
                continue
            if _is_numeric(val):
                nums += 1
            else:
                texts += 1
        if texts and nums == 0:
            end = r
        elif nums:
            break
    return end


def _tiny_islands(components: List[List[Tuple[int, int]]]) -> List[Dict[str, int]]:
    out = []
    for comp in components:
        rows = [r for r, _ in comp]
        cols = [c for _, c in comp]
        rr = max(rows) - min(rows) + 1
        cc = max(cols) - min(cols) + 1
        if len(comp) <= 2 and rr <= 2 and cc <= 2:
            out.append({
                "start_row": min(rows),
                "end_row": max(rows),
                "start_col": min(cols),
                "end_col": max(cols),
                "cells": len(comp),
            })
    return out


def _sample_records(df: pd.DataFrame, report: Dict[str, Any], limit: int = 12) -> List[Dict[str, Any]]:
    """A few long-form examples from the detected axes (discovery sample, not full extract)."""
    axes = report.get("axes") or {}
    region = report.get("data_region") or {}
    if not region:
        return []
    metric_col = axes.get("metric_column")
    stub_cols = list(axes.get("stub_columns") or [])
    header_rows = list(axes.get("header_rows") or [])
    period_row = header_rows[-1] if header_rows else None
    body_start = (max(header_rows) + 1) if header_rows else int(region.get("start_row") or 0)
    sc = int(region.get("start_col") or 0)
    ec = int(region.get("end_col") or 0)
    er = int(region.get("end_row") or 0)
    samples: List[Dict[str, Any]] = []
    if axes.get("date_axis") != "columns" or period_row is None:
        return samples
    value_start_col = max(sc, (metric_col + 1) if metric_col is not None else sc + 1)
    for r in range(body_start, er + 1):
        metric = _cell_text(df.iat[r, metric_col]) if metric_col is not None else ""
        if not metric and metric_col is not None:
            continue
        dim_bits = []
        for c in stub_cols:
            t = _cell_text(df.iat[r, c])
            if t:
                dim_bits.append(t)
        for c in range(value_start_col, ec + 1):
            val = df.iat[r, c]
            if not _is_numeric(val):
                continue
            period = _cell_text(df.iat[period_row, c]) if period_row < df.shape[0] else ""
            rec: Dict[str, Any] = {
                "value": _cell_text(val),
                "row": r,
                "col": c,
            }
            if period:
                rec["period"] = period
            if metric:
                rec["metric"] = metric
            if dim_bits:
                rec["dimensions"] = dim_bits[:3]
            samples.append(rec)
            if len(samples) >= limit:
                return samples
    return samples


def _excel_col_letter(index: int) -> str:
    n = int(index) + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"


def _excel_a1_range(start_row: int, end_row: int, start_col: int, end_col: int) -> str:
    return (
        f"{_excel_col_letter(start_col)}{int(start_row) + 1}"
        f":{_excel_col_letter(end_col)}{int(end_row) + 1}"
    )


def _overlay_rect(
    role: str,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    style: str = "band",
) -> Optional[Dict[str, Any]]:
    if end_row < start_row or end_col < start_col:
        return None
    return {
        "role": role,
        "style": style,
        "start_row": int(start_row),
        "end_row": int(end_row),
        "start_col": int(start_col),
        "end_col": int(end_col),
        "excel_range": _excel_a1_range(start_row, end_row, start_col, end_col),
    }


def _tiny_island_cells(tiny: Sequence[Dict[str, Any]]) -> set:
    cells = set()
    for island in tiny or []:
        for r in range(int(island["start_row"]), int(island["end_row"]) + 1):
            for c in range(int(island["start_col"]), int(island["end_col"]) + 1):
                cells.add((r, c))
    return cells


def _last_dense_value_col(
    df: pd.DataFrame,
    body_start: int,
    end_row: int,
    value_start_col: int,
    end_col: int,
    island_cells: set,
) -> int:
    last = value_start_col
    for r in range(body_start, min(end_row + 1, df.shape[0])):
        for c in range(value_start_col, min(end_col + 1, df.shape[1])):
            if (r, c) in island_cells:
                continue
            if _is_numeric(df.iat[r, c]):
                last = max(last, c)
    return last


def _minimap_overlays(df: pd.DataFrame, report: Dict[str, Any]) -> List[Dict[str, Any]]:
    region = report.get("data_region") or {}
    axes = report.get("axes") or {}
    if not region:
        return []
    sr = int(region["start_row"])
    er = int(region["end_row"])
    sc = int(region["start_col"])
    ec = int(region["end_col"])
    header_rows = list(axes.get("header_rows") or [])
    metric_col = axes.get("metric_column")
    stub_cols = [int(c) for c in (axes.get("stub_columns") or [])]
    title_end = axes.get("title_band_end_row")
    total_rows = [int(r) for r in (axes.get("total_rows") or [])]
    total_cols = [int(c) for c in (axes.get("total_cols") or [])]
    tiny = list(report.get("tiny_islands") or [])
    island_cells = _tiny_island_cells(tiny)
    body_start = (max(header_rows) + 1) if header_rows else sr
    if title_end is not None:
        body_start = max(body_start, int(title_end) + 1)
    value_start_col = sc
    if metric_col is not None:
        value_start_col = max(value_start_col, int(metric_col) + 1)
    elif stub_cols:
        value_start_col = max(value_start_col, max(stub_cols) + 1)
    total_col_set = {int(c) for c in total_cols}
    while value_start_col in total_col_set and value_start_col < ec:
        value_start_col += 1
    dense_end_col = _last_dense_value_col(df, body_start, er, value_start_col, ec, island_cells)

    overlays: List[Dict[str, Any]] = []
    meta_end_col = sc
    if title_end is not None and int(title_end) >= sr:
        meta_end_col = _title_band_last_col(df, sr, int(title_end), sc, ec)
        meta = _overlay_rect(
            "metadata",
            sr,
            int(title_end),
            sc,
            meta_end_col,
        )
        if meta:
            overlays.append(meta)

    if axes.get("date_axis") == "columns" and header_rows:
        time_end_col = dense_end_col
        for hr in header_rows:
            if hr >= df.shape[0]:
                continue
            for c in range(value_start_col, min(ec + 1, df.shape[1])):
                if not _is_empty(df.iat[hr, c]):
                    time_end_col = max(time_end_col, c)
        time = _overlay_rect("time", min(header_rows), max(header_rows), value_start_col, time_end_col)
        if time:
            overlays.append(time)
    elif axes.get("date_axis") == "column" and axes.get("date_column") is not None:
        dc = int(axes["date_column"])
        time = _overlay_rect("time", body_start, er, dc, dc)
        if time:
            overlays.append(time)

    if stub_cols:
        dim = _overlay_rect("dimensions", body_start, er, min(stub_cols), max(stub_cols))
        if dim:
            overlays.append(dim)

    if metric_col is not None:
        metrics = _overlay_rect("metrics", body_start, er, int(metric_col), int(metric_col))
        if metrics:
            overlays.append(metrics)
    elif axes.get("metric_name_axis") == "columns" and axes.get("metric_header_row") is not None:
        mhr = int(axes["metric_header_row"])
        metrics = _overlay_rect("metrics", mhr, mhr, value_start_col, dense_end_col)
        if metrics:
            overlays.append(metrics)

    values = _overlay_rect("values", body_start, er, value_start_col, dense_end_col)
    if values:
        overlays.append(values)

    for tr in total_rows:
        tot = _overlay_rect("totals", tr, tr, sc, dense_end_col)
        if tot:
            overlays.append(tot)
    for tc in total_cols:
        tot = _overlay_rect("totals", body_start, er, tc, tc)
        if tot:
            overlays.append(tot)

    noise_keys: set = set()

    def _add_noise(r0: int, r1: int, c0: int, c1: int) -> None:
        key = (int(r0), int(r1), int(c0), int(c1))
        if key in noise_keys:
            return
        noise = _overlay_rect("noise", r0, r1, c0, c1, style="dot")
        if noise:
            noise_keys.add(key)
            overlays.append(noise)

    for r in range(sr, min(er + 1, df.shape[0])):
        for c in range(sc, min(ec + 1, df.shape[1])):
            if _looks_comment(df.iat[r, c]):
                _add_noise(r, r, c, c)
                if len(noise_keys) >= 40:
                    break
        if len(noise_keys) >= 40:
            break

    for island in tiny[:24]:
        comment_island = False
        for r in range(int(island["start_row"]), int(island["end_row"]) + 1):
            if comment_island or r >= df.shape[0]:
                break
            for c in range(int(island["start_col"]), int(island["end_col"]) + 1):
                if c >= df.shape[1]:
                    break
                if _looks_comment(df.iat[r, c]):
                    comment_island = True
                    break
        in_title = (
            not comment_island
            and title_end is not None
            and int(island["end_row"]) <= int(title_end)
            and int(island["end_col"]) <= meta_end_col
        )
        in_core_table = (
            not comment_island
            and int(island["start_col"]) >= value_start_col
            and int(island["end_col"]) <= dense_end_col
            and int(island["start_row"]) >= body_start
            and int(island["end_row"]) <= er
        )
        if in_title or in_core_table:
            continue
        _add_noise(
            int(island["start_row"]),
            int(island["end_row"]),
            int(island["start_col"]),
            int(island["end_col"]),
        )
    return overlays


def _minimap_occupancy(
    df: pd.DataFrame,
    region: Dict[str, int],
    max_w: int = 120,
    max_h: int = 80,
) -> Dict[str, Any]:
    sr = int(region["start_row"])
    er = int(region["end_row"])
    sc = int(region["start_col"])
    ec = int(region["end_col"])
    rows = er - sr + 1
    cols = ec - sc + 1
    gw = max(1, min(cols, max_w))
    gh = max(1, min(rows, max_h))
    bits: List[str] = []
    for gy in range(gh):
        r0 = sr + gy * rows // gh
        r1 = sr + (gy + 1) * rows // gh
        r1 = max(r1, r0 + 1)
        for gx in range(gw):
            c0 = sc + gx * cols // gw
            c1 = sc + (gx + 1) * cols // gw
            c1 = max(c1, c0 + 1)
            filled = False
            for r in range(r0, min(r1, df.shape[0])):
                if filled:
                    break
                for c in range(c0, min(c1, df.shape[1])):
                    if not _is_empty(df.iat[r, c]):
                        filled = True
                        break
            bits.append("1" if filled else "0")
    return {
        "start_row": sr,
        "end_row": er,
        "start_col": sc,
        "end_col": ec,
        "rows": rows,
        "cols": cols,
        "grid_rows": gh,
        "grid_cols": gw,
        "occupancy": "".join(bits),
    }


_A1_RANGE_RE = re.compile(r"^\$*([A-Za-z]+)\$*(\d+)(?::\$*([A-Za-z]+)\$*(\d+))?$")


def _col_letters_to_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        if not ("A" <= ch <= "Z"):
            continue
        n = n * 26 + (ord(ch) - 64)
    return max(0, n - 1)


def _parse_merged_a1(spec: str) -> Optional[Dict[str, int]]:
    match = _A1_RANGE_RE.match(str(spec or "").strip())
    if not match:
        return None
    c1 = _col_letters_to_index(match.group(1))
    r1 = int(match.group(2)) - 1
    if match.group(3):
        c2 = _col_letters_to_index(match.group(3))
        r2 = int(match.group(4)) - 1
    else:
        c2, r2 = c1, r1
    start_row, end_row = min(r1, r2), max(r1, r2)
    start_col, end_col = min(c1, c2), max(c1, c2)
    if end_row == start_row and end_col == start_col:
        return None
    return {
        "start_row": start_row,
        "end_row": end_row,
        "start_col": start_col,
        "end_col": end_col,
    }


def _minimap_merges(
    merged_specs: Sequence[Any],
    region: Dict[str, int],
    limit: int = 2500,
) -> List[Dict[str, int]]:
    out: List[Dict[str, int]] = []
    sr = int(region["start_row"])
    er = int(region["end_row"])
    sc = int(region["start_col"])
    ec = int(region["end_col"])
    for spec in merged_specs or []:
        bbox = None
        if isinstance(spec, str):
            bbox = _parse_merged_a1(spec)
        elif isinstance(spec, dict) and "start_row" in spec:
            bbox = {
                "start_row": int(spec["start_row"]),
                "end_row": int(spec.get("end_row", spec["start_row"])),
                "start_col": int(spec["start_col"]),
                "end_col": int(spec.get("end_col", spec["start_col"])),
            }
        if not bbox:
            continue
        if bbox["end_row"] < sr or bbox["start_row"] > er or bbox["end_col"] < sc or bbox["start_col"] > ec:
            continue
        out.append(bbox)
        if len(out) >= limit:
            break
    return out


def _build_minimap(
    df: pd.DataFrame,
    report: Dict[str, Any],
    merged_specs: Optional[Sequence[Any]] = None,
) -> Optional[Dict[str, Any]]:
    region = report.get("data_region")
    if not region:
        return None
    occupancy = _minimap_occupancy(df, region)
    occupancy["overlays"] = _minimap_overlays(df, report)
    occupancy["merges"] = _minimap_merges(merged_specs or [], region)
    return occupancy


def detect_layout_complexity(
    grid_df: pd.DataFrame,
    visual_patterns: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return flags, score, axis roles, and review roles for Guided Setup."""
    df = grid_df.copy() if isinstance(grid_df, pd.DataFrame) else pd.DataFrame(grid_df)
    rows, cols = df.shape
    vp = visual_patterns if isinstance(visual_patterns, dict) else {}
    merged = vp.get("merged_ranges") or []
    merged_n = len(merged) if isinstance(merged, (list, tuple)) else 0

    region = _data_region(df)
    mask = _nonempty_mask(df)
    components = _connected_components(mask)
    tiny = _tiny_islands(components)
    blank_row_ratio, blank_col_ratio = _blank_ratios(df)
    time_info = _scan_header_time_axis(df)
    header_rows = list(time_info.get("header_rows") or [])
    body_start = (max(header_rows) + 1) if header_rows else (region["start_row"] if region else 0)
    kpi_info = _scan_kpi_axis(df, body_start)
    stub_info = _scan_stub_dimensions(df, body_start, kpi_info.get("metric_column"))
    totals = _find_totals(df)
    title_end = _title_band_end(df, header_rows)

    wide = bool(region and region["cols"] >= max(8, int(region["rows"] * 1.2)))
    long_shape = bool(region and region["rows"] > region["cols"] * 1.5)
    density = 0.0
    if region and region["rows"] * region["cols"] > 0:
        density = round(region["filled"] / (region["rows"] * region["cols"]), 3)

    multi_header = len(header_rows) >= 2
    crosstab = time_info.get("date_axis") == "columns"
    tiny_outlier_islands = len(tiny) >= 1 and (crosstab or len(components) >= 4)
    multiple_blocks = len(components) - len(tiny) > 1

    score = 0
    if merged_n > 0:
        score += 20
    if multiple_blocks:
        score += 15
    if blank_row_ratio > 0.2:
        score += 10
    if blank_col_ratio > 0.15:
        score += 5
    if multi_header:
        score += 20
    if crosstab:
        score += 25
    if tiny_outlier_islands:
        score += 10
    if kpi_info.get("metric_name_axis") == "rows":
        score += 15

    if score < 20:
        classification = "structured"
        sheet_type = "flat_table"
    elif score < 50:
        classification = "semi_structured"
        sheet_type = "crosstab" if crosstab else "semi_structured"
    else:
        classification = "messy"
        sheet_type = "planning_matrix" if crosstab else "messy_report"

    use_adapter = bool(
        crosstab
        or (classification != "structured" and (multi_header or tiny_outlier_islands or (wide and density < 0.35)))
    )

    axes = {
        **time_info,
        **kpi_info,
        **stub_info,
        "value_cell": "single",
        "title_band_end_row": title_end,
        **totals,
    }

    roles_for_review: Dict[str, Any] = {}
    if title_end is not None:
        roles_for_review["metadata"] = {
            "label": "Title / metadata",
            "axis": "band",
            "detail": (
                f"Rows {region['start_row'] + 1}–{int(title_end) + 1} above the table"
                if region
                else f"Through row {int(title_end) + 1}"
            ),
        }
    roles_for_review.update({
        "time": {
            "label": "Period / Time",
            "kind": time_info.get("time_kind") or "unknown",
            "axis": time_info.get("date_axis") or "unknown",
            "detail": (
                f"Period headers on Excel row(s) {', '.join(str(r + 1) for r in header_rows)}"
                if header_rows
                else (
                    f"Date values in column {axes.get('date_column', 0) + 1}"
                    if time_info.get("date_axis") == "column"
                    else "Not detected"
                )
            ),
        },
        "dimensions": {
            "label": "Dimensions",
            "axis": stub_info.get("dimension_axis") or "unknown",
            "detail": (
                "Stub columns "
                + ", ".join(str(c + 1) for c in (stub_info.get("stub_columns") or []))
                if stub_info.get("stub_columns")
                else "Not detected"
            ),
        },
        "metrics": {
            "label": "Metrics",
            "axis": kpi_info.get("metric_name_axis") or "unknown",
            "detail": (
                f"KPI names in column {int(kpi_info['metric_column']) + 1}"
                if kpi_info.get("metric_column") is not None
                else (
                    f"KPI names in header row {(kpi_info.get('metric_header_row') or 0) + 1}"
                    if kpi_info.get("metric_name_axis") == "columns"
                    else "Not detected"
                )
            ),
        },
        "values": {
            "label": "Values",
            "cell": "single",
            "detail": (
                f"Numeric body in {region['rows']}×{region['cols']} data region"
                if region
                else "Not detected"
            ),
        },
        "totals": {
            "label": "Totals",
            "detail": (
                f"{len(totals['total_rows'])} labeled total row(s), {len(totals['total_cols'])} total column(s)"
                if (totals["total_rows"] or totals["total_cols"])
                else "None detected"
            ),
        },
        "noise": {
            "label": "Comments / noise",
            "detail": (
                f"{len(tiny)} disconnected island(s); comment/note cells marked on the map"
                if tiny
                else "Comment/note cells and far-right outliers on the map"
            ),
        },
    })

    reasons = []
    if merged_n:
        reasons.append(f"{merged_n} merged range(s)")
    if crosstab:
        reasons.append("repeated month/week headers")
    if multi_header:
        reasons.append("multi-row header band")
    if kpi_info.get("metric_name_axis") == "rows":
        reasons.append("KPI labels in a stub column")
    if tiny:
        reasons.append(f"{len(tiny)} disconnected 1–2 cell island(s)")
    if wide and density < 0.4:
        reasons.append(f"wide sparse grid (density {density})")

    report: Dict[str, Any] = {
        "sheet_type": sheet_type,
        "classification": classification,
        "complexity_score": score,
        "shape": "wide" if wide else ("long" if long_shape else "balanced"),
        "merged_cells": merged_n > 0,
        "merged_range_count": merged_n,
        "multi_header": multi_header,
        "multiple_blocks": multiple_blocks,
        "tiny_outlier_islands": tiny_outlier_islands,
        "crosstab": crosstab,
        "blank_row_ratio": blank_row_ratio,
        "blank_col_ratio": blank_col_ratio,
        "density": density,
        "component_count": len(components),
        "tiny_island_count": len(tiny),
        "tiny_islands": tiny[:24],
        "data_region": region,
        "axes": axes,
        "roles_for_review": roles_for_review,
        "reasons": reasons,
        "use_adapter": use_adapter,
        "suggested_transforms": (
            ["flatten_headers", "unpivot_matrix", "fill_merged"]
            if crosstab
            else (["flatten_headers"] if multi_header else [])
        ),
        "sample_records": [],
        "adapter_used": False,
        "minimap": None,
    }
    report["sample_records"] = _sample_records(df, report)
    report["minimap"] = _build_minimap(df, report, merged)
    return report


def should_use_layout_adapter(report: Dict[str, Any]) -> bool:
    return bool(report.get("use_adapter"))


def _cells_in_bbox(df: pd.DataFrame, bbox: Dict[str, int]) -> List[Tuple[int, int]]:
    cells: List[Tuple[int, int]] = []
    for r in range(int(bbox["start_row"]), int(bbox["end_row"]) + 1):
        for c in range(int(bbox["start_col"]), int(bbox["end_col"]) + 1):
            if r < df.shape[0] and c < df.shape[1] and not _is_empty(df.iat[r, c]):
                cells.append((r, c))
    return cells


def adapter_blocks_from_report(
    demarcator: Any,
    grid_df: pd.DataFrame,
    report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Build a small set of demarcation-compatible blocks from diagnostics.
    Tiny islands are not emitted as cards.
    """
    df = grid_df
    region = report.get("data_region")
    if not region:
        return demarcator.extract_candidate_blocks(df)

    axes = report.get("axes") or {}
    title_end = axes.get("title_band_end_row")
    main_start_row = int(region["start_row"])
    if title_end is not None and int(title_end) >= int(region["start_row"]):
        main_start_row = int(title_end) + 1
        main_start_row = min(main_start_row, int(region["end_row"]))

    main_bbox = {
        "start_row": main_start_row,
        "end_row": int(region["end_row"]),
        "start_col": int(region["start_col"]),
        "end_col": int(region["end_col"]),
    }
    header_rows = list(axes.get("header_rows") or [])
    if header_rows:
        main_bbox["header_row"] = int(min(header_rows))
        if len(header_rows) >= 2 or report.get("multi_header"):
            main_bbox["header_mode"] = "multi"
            main_bbox["header_row_end"] = int(max(header_rows))

    blocks: List[Dict[str, Any]] = []
    index = 1
    if title_end is not None and int(title_end) >= int(region["start_row"]):
        meta_end_col = _title_band_last_col(
            df,
            int(region["start_row"]),
            int(title_end),
            int(region["start_col"]),
            int(region["end_col"]),
        )
        meta_bbox = {
            "start_row": int(region["start_row"]),
            "end_row": int(title_end),
            "start_col": int(region["start_col"]),
            "end_col": meta_end_col,
        }
        meta_cells = _cells_in_bbox(df, meta_bbox)
        if meta_cells:
            meta = demarcator._build_candidate_block(df, meta_cells, index=index)
            meta["category"] = "Metadata"
            meta["ai_suggestion"] = "Use as Context"
            meta["name"] = "Title / logo (metadata)"
            meta["summary"] = "Compact text band above the table (logo, confidential mark, last modified)."
            meta["confidence"] = 0.82
            meta["heuristic_confidence"] = 0.82
            meta["python_detection"] = dict(meta.get("python_detection") or {})
            meta["python_detection"]["cleanup_actions"] = list(
                meta["python_detection"].get("cleanup_actions") or []
            ) + ["layout_adapter_title_band"]
            meta["coordinates"] = dict(meta.get("coordinates") or {})
            meta["coordinates"].update(meta_bbox)
            blocks.append(meta)
            index += 1

    main_cells = _cells_in_bbox(df, main_bbox)
    if not main_cells:
        return demarcator.extract_candidate_blocks(df)
    main = demarcator._build_candidate_block(df, main_cells, index=index)
    main["category"] = "Main Data"
    main["ai_suggestion"] = "Keep"
    main["name"] = "Planning matrix (main data)"
    main["summary"] = (
        "Single visual table from layout diagnostics (date/KPI axes). "
        "Tiny disconnected cells were not listed as separate blocks."
    )
    main["confidence"] = 0.88
    main["heuristic_confidence"] = 0.88
    coords = dict(main.get("coordinates") or {})
    coords.update({k: v for k, v in main_bbox.items() if k in ("header_row", "header_mode", "header_row_end")})
    if "header_row" in main_bbox:
        coords["header_row"] = main_bbox["header_row"]
    main["coordinates"] = coords
    pd_det = dict(main.get("python_detection") or {})
    pd_det["header_row_candidate"] = coords.get("header_row", pd_det.get("header_row_candidate"))
    pd_det["multi_header_suggested"] = coords.get("header_mode") == "multi"
    pd_det["cleanup_actions"] = list(pd_det.get("cleanup_actions") or []) + [
        "layout_adapter_union_bbox",
        f"suppressed_tiny_islands:{int(report.get('tiny_island_count') or 0)}",
    ]
    main["python_detection"] = pd_det
    main["layout_roles"] = report.get("roles_for_review")
    blocks.append(main)

    report["adapter_used"] = True
    return blocks
