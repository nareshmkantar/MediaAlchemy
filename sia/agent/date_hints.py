"""
Heuristic date interpretation hints for Guided Setup and mapping propose.

Suggests ``date_shape`` and source ``date_granularity`` from a dataframe sample so the UI
can pre-fill choices; analysts can override via ``/api/context/source``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from sia.agent.date_column_inference import (
    infer_date_cadence_fast,
    infer_date_range_column_pair,
    infer_date_range_pair_heuristic,
)


def compute_date_hints_for_dataframe(
    df: Optional[pd.DataFrame],
    *,
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
    target_template: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Return UI-friendly suggestions:

    - ``suggested_date_shape``: ``unknown`` | ``period_span`` | ``single_timestamp`` | ``date_parts``
    - ``suggested_date_granularity``: ``range`` | ``daily`` | ``weekly`` | ``monthly`` | ``quarterly`` | ````
    - ``suggested_range_start_col`` / ``suggested_range_end_col`` when span is likely
    - ``confidence`` 0..1, ``rationale`` short machine reason
    """
    out: Dict[str, Any] = {
        "suggested_date_shape": "unknown",
        "suggested_date_granularity": "",
        "suggested_range_start_col": "",
        "suggested_range_end_col": "",
        "confidence": 0.0,
        "rationale": "",
    }
    if df is None or getattr(df, "empty", True):
        out["rationale"] = "empty_frame"
        return out

    mappings = list(approved_mappings or [])
    pair = None
    if mappings:
        pair = infer_date_range_column_pair(df, mappings, target_template)
    pair = pair or infer_date_range_pair_heuristic(df)
    if pair and float(pair.get("confidence") or 0) >= 0.55:
        out["suggested_date_shape"] = "period_span"
        out["suggested_date_granularity"] = "range"
        out["suggested_range_start_col"] = str(pair.get("start_date_col") or "")
        out["suggested_range_end_col"] = str(pair.get("end_date_col") or "")
        out["confidence"] = float(pair.get("confidence") or 0.0)
        out["rationale"] = str(pair.get("inference_method") or "two_column_span")
        return out

    # Single timestamp cadence from best parseable date-like column
    best: Optional[Dict[str, Any]] = None
    best_col: Optional[str] = None
    for col in list(df.columns)[: min(48, len(df.columns))]:
        ser = df[col]
        if ser is None or not hasattr(ser, "dropna"):
            continue
        parsed = pd.to_datetime(ser, errors="coerce", utc=False, format="mixed")
        rate = float(parsed.notna().mean()) if len(ser) else 0.0
        if rate < 0.65:
            continue
        prof = infer_date_cadence_fast(ser, str(col))
        conf = float(prof.get("confidence") or 0.0)
        if best is None or conf > float(best.get("confidence") or 0):
            best = prof
            best_col = str(col)

    if best is None:
        out["rationale"] = "no_parseable_date_column"
        return out

    cadence = str(best.get("inferred_cadence") or "").strip().lower()
    grain_map = {
        "daily": "daily",
        "weekly": "weekly",
        "monthly": "monthly",
        "quarterly": "quarterly",
    }
    out["suggested_date_shape"] = "single_timestamp"
    out["suggested_date_granularity"] = grain_map.get(cadence, "")
    out["confidence"] = float(best.get("confidence") or 0.0)
    out["rationale"] = f"single_column:{best_col}:{cadence}"
    return out


def normalize_date_shape_for_storage(raw: Optional[str]) -> str:
    s = str(raw or "").strip().lower()
    allowed = {
        "unknown",
        "single_timestamp",
        "period_span",
        "period_text",
        "date_parts",
    }
    return s if s in allowed else "unknown"
