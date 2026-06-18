"""Normalize date-like columns for analyst-facing exports (Excel, CSV, etc.)."""

from __future__ import annotations

import re
import pandas as pd

_DATE_NAME_HINT = re.compile(
    r"(^date$|_date$|date_|^time$|_time$|period|week_start|week_end|as_of|modeling_period)",
    re.IGNORECASE,
)


def normalize_dataframe_dates_for_export(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """
    Format datetime columns and date-like object columns as ``YYYY-MM-DD`` strings.

    Datetime64 columns are always normalized. Object columns are parsed only when the
    column name looks date-related and a majority of non-null values parse as datetimes.
    """
    if df is None:
        return None
    if df.empty:
        return df.copy()

    out = df.copy()
    for col in out.columns:
        col_str = str(col)
        series = out[col]

        if pd.api.types.is_datetime64_any_dtype(series):
            dt = pd.to_datetime(series, errors="coerce", utc=False)
            if getattr(series.dtype, "tz", None) is not None:
                dt = dt.dt.tz_convert("UTC").dt.tz_localize(None)
            formatted = dt.dt.strftime("%Y-%m-%d")
            out[col] = formatted.where(dt.notna(), series)
            continue

        if not _DATE_NAME_HINT.search(col_str):
            continue

        dt = pd.to_datetime(series, errors="coerce", utc=False)
        non_null = series.notna() & (series.astype(str).str.strip() != "")
        if not non_null.any():
            continue
        parsed_ratio = float(dt.notna().sum()) / float(non_null.sum())
        if parsed_ratio < 0.5:
            continue
        formatted = dt.dt.strftime("%Y-%m-%d")
        out[col] = formatted.where(dt.notna(), series)

    return out
