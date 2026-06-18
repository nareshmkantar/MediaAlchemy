"""
Read-only checks for stacking multiple sources (union / pd.concat).

Used by ``validate.cross_source_union`` and the web API before process-all.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Set

import pandas as pd


def validate_column_sets_for_union(
    column_sets_by_source: Mapping[str, List[str]],
    *,
    key_hints: Optional[List[str]] = None,
    dtype_map_by_source: Optional[Mapping[str, Mapping[str, str]]] = None,
    date_granularity_by_source: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """
    Compare per-source column names (and optional dtypes) to flag concat risk.

    Returns a JSON-serializable report: ``ok`` (no blocking issues), ``warnings``,
    ``intersection``, ``union_columns``, ``sources_missing_intersection``, etc.
    """
    warnings: List[Dict[str, Any]] = []
    if not column_sets_by_source:
        return {
            "ok": False,
            "blocking": True,
            "message": "No column_sets_by_source provided.",
            "warnings": [{"code": "NO_SOURCES", "detail": "Need at least one source column list."}],
        }

    ids = [str(k) for k in column_sets_by_source.keys()]
    sets: Dict[str, Set[str]] = {
        sid: {str(c).strip() for c in cols if str(c).strip()}
        for sid, cols in column_sets_by_source.items()
        if isinstance(cols, (list, tuple))
    }
    non_empty = {k: v for k, v in sets.items() if v}
    if len(non_empty) < 2:
        return {
            "ok": True,
            "blocking": False,
            "message": "Single-source column list — union checks are informational only.",
            "warnings": [],
            "source_ids": ids,
            "column_sets": {k: sorted(v) for k, v in sets.items()},
        }

    sid_list = list(non_empty.keys())
    inter: Set[str] = set.intersection(*(non_empty[s] for s in sid_list))
    union_cols: Set[str] = set.union(*(non_empty[s] for s in sid_list))

    missing_intersection = {sid: sorted(non_empty[sid] - inter) for sid in sid_list if non_empty[sid] - inter}
    if not inter:
        warnings.append(
            {
                "code": "NO_COLUMN_INTERSECTION",
                "severity": "high",
                "detail": "No identical column names across all sources — pd.concat will widen the schema with NaNs.",
            }
        )

    # Key hints: warn if hinted keys missing from intersection
    for hint in key_hints or []:
        h = str(hint).strip()
        if not h:
            continue
        if h not in inter:
            warnings.append(
                {
                    "code": "KEY_HINT_NOT_SHARED",
                    "severity": "medium",
                    "detail": f"Suggested join/union key {h!r} is not in the intersection of source columns.",
                }
            )

    # Dtype mismatches on intersection (string compare only)
    if dtype_map_by_source and inter:
        for col in sorted(inter):
            dtypes = []
            for sid in sid_list:
                mp = dtype_map_by_source.get(sid) or {}
                if isinstance(mp, dict) and col in mp:
                    dtypes.append(str(mp[col]).lower())
            uniq = sorted(set(dtypes))
            if len(uniq) > 1:
                warnings.append(
                    {
                        "code": "DTYPE_MISMATCH",
                        "severity": "medium",
                        "column": col,
                        "detail": f"Inferred dtypes differ across sources: {uniq}",
                    }
                )

    # Grain hints
    if date_granularity_by_source:
        grains = {
            str(sid): str(v).strip().lower()
            for sid, v in date_granularity_by_source.items()
            if str(sid) in non_empty and v
        }
        if len(set(grains.values())) > 1:
            warnings.append(
                {
                    "code": "DATE_GRANULARITY_MISMATCH",
                    "severity": "medium",
                    "detail": f"Sources disagree on date_granularity: {grains}",
                }
            )

    blocking = any(w.get("severity") == "high" for w in warnings)
    return {
        "ok": not blocking,
        "blocking": blocking,
        "message": "Union readiness report (read-only).",
        "source_ids": sid_list,
        "intersection": sorted(inter),
        "union_columns": sorted(union_cols),
        "columns_only_in_source": missing_intersection,
        "warnings": warnings,
    }


def sample_dtypes_from_dataframe(df: pd.DataFrame, *, max_cols: int = 80) -> Dict[str, str]:
    """Lightweight dtype map for validation (string names)."""
    if df is None or df.empty:
        return {}
    out: Dict[str, str] = {}
    for i, col in enumerate(df.columns):
        if i >= max_cols:
            break
        try:
            out[str(col)] = str(df[col].dtype)
        except Exception:
            out[str(col)] = "unknown"
    return out
