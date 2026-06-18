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
