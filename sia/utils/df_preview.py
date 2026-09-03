"""Serialize DataFrame previews with stable column order for JSON/UI."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


def preview_column_order(df: pd.DataFrame) -> List[str]:
    """Column names in frame order (stringified)."""
    return [str(c) for c in df.columns]


def dataframe_to_preview_records(
    df: pd.DataFrame,
    *,
    max_rows: Optional[int] = None,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Serialize rows with keys ordered to match ``df.columns``."""
    if df is None or df.empty:
        return [], []

    temp_df = df.head(max_rows) if max_rows is not None else df
    temp_df = temp_df.copy()
    if not temp_df.columns.is_unique:
        new_cols: List[str] = []
        seen: Dict[str, int] = {}
        for col in temp_df.columns:
            col_str = str(col)
            if col_str not in seen:
                seen[col_str] = 0
                new_cols.append(col_str)
            else:
                seen[col_str] += 1
                new_cols.append(f"{col_str}_{seen[col_str]}")
        temp_df.columns = new_cols

    order = preview_column_order(temp_df)
    records: List[Dict[str, Any]] = []
    # Build rows by position so integer column labels (header=None Excel reads) stay aligned.
    for row_idx in range(len(temp_df)):
        safe_row: Dict[str, Any] = {}
        for col_label in temp_df.columns:
            col_key = str(col_label)
            value = temp_df.iloc[row_idx][col_label]
            if isinstance(value, (datetime, date)):
                safe_row[col_key] = value.isoformat()
            elif pd.isna(value):
                safe_row[col_key] = None
            elif hasattr(value, "item"):
                safe_row[col_key] = value.item()
            else:
                safe_row[col_key] = value
        records.append(safe_row)
    return order, records


def _is_integer_column_label(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    token = str(value).strip()
    return token.isdigit()


def looks_like_integer_column_labels(columns: List[Any]) -> bool:
    if not columns:
        return False
    return all(_is_integer_column_label(col) for col in columns)


def _looks_like_header_cell(value: Any) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    token = str(value).strip()
    if not token or token.lower() in {"nan", "none", "null"}:
        return False
    if token.isdigit():
        return False
    try:
        float(token.replace(",", ""))
        return False
    except ValueError:
        return True


def promote_numeric_header_row_for_preview(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """If columns are 0..N and the first row looks like names, use that row as headers.

    Raw-grid snapshots (and some failed extracts) store Excel/CSV cells with RangeIndex
    columns, so the true header sits in row 0. Preview should show those names.
    """
    if df is None or getattr(df, "empty", True) or len(df) < 2:
        return df
    if not looks_like_integer_column_labels(list(df.columns)):
        return df
    first = [df.iloc[0, i] for i in range(len(df.columns))]
    named = sum(1 for value in first if _looks_like_header_cell(value))
    if named < max(1, int(0.6 * len(first))):
        return df
    new_cols: List[str] = []
    seen: Dict[str, int] = {}
    for idx, value in enumerate(first):
        label = str(value).strip() if _looks_like_header_cell(value) else f"Column_{idx}"
        if label not in seen:
            seen[label] = 0
            new_cols.append(label)
        else:
            seen[label] += 1
            new_cols.append(f"{label}_{seen[label]}")
    out = df.iloc[1:].copy()
    out.columns = new_cols
    return out.reset_index(drop=True)


def mapped_preview_column_names(
    columns: List[Any],
    approved_mappings: Optional[List[Any]] = None,
    extra_names: Optional[List[Any]] = None,
) -> List[str]:
    """Keep mapped/selected names that are actually present on the frame."""
    wanted: List[str] = []
    for item in approved_mappings or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target_column") or "").strip()
        source = str(item.get("source_column") or "").strip()
        if target.lower() in {"", "no match", "none"}:
            continue
        if target:
            wanted.append(target)
        if source:
            wanted.append(source)
    for name in extra_names or []:
        token = str(name or "").strip()
        if token:
            wanted.append(token)
    if not wanted:
        return [str(c) for c in columns]
    wanted_l = {name.lower() for name in wanted}
    kept = [c for c in columns if str(c).strip().lower() in wanted_l]
    return [str(c) for c in kept] if kept else [str(c) for c in columns]
