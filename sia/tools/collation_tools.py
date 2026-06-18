"""Deterministic merge helpers used by the parent collation LangGraph."""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from sia.agent.target_template_utils import collation_merge_column_key_order


def reorder_columns(df: pd.DataFrame, column_order: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Reorder columns; append any columns not listed at the end (stable)."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    order = [c for c in (column_order or []) if c in df.columns]
    rest = [c for c in df.columns if c not in order]
    return df.loc[:, order + rest].copy()


def drop_duplicate_rows(
    df: pd.DataFrame,
    subset: Optional[Sequence[str]] = None,
    keep: str = "first",
) -> pd.DataFrame:
    """Drop duplicate rows; ``subset`` defaults to all columns."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    use_subset = list(subset) if subset else None
    if use_subset:
        use_subset = [c for c in use_subset if c in df.columns]
        if not use_subset:
            use_subset = None
    return df.drop_duplicates(subset=use_subset, keep=keep, ignore_index=True)


def aggregate_duplicate_keys(
    df: pd.DataFrame,
    group_keys: Sequence[str],
    sum_columns: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """Group by ``group_keys``; sum numeric measures, ``first`` for other columns."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    keys = [k for k in group_keys if k in df.columns]
    if not keys:
        return df.copy()

    work = df.copy()
    for c in list(work.columns):
        if c in keys:
            continue
        s = work[c]
        if pd.api.types.is_bool_dtype(s):
            continue
        if pd.api.types.is_numeric_dtype(s):
            continue
        if not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
            continue
        nn = int(s.notna().sum())
        if nn == 0:
            continue
        coerced = pd.to_numeric(s, errors="coerce")
        if int((coerced.notna() & s.notna()).sum()) < max(1, int(0.90 * nn)):
            continue
        work[c] = coerced

    if sum_columns:
        sum_cols = [c for c in sum_columns if c in work.columns and c not in keys]
    else:
        sum_cols = [
            c
            for c in work.columns
            if c not in keys and pd.api.types.is_numeric_dtype(work[c]) and not pd.api.types.is_bool_dtype(work[c])
        ]
    others = [c for c in work.columns if c not in keys and c not in sum_cols]
    agg: Dict[str, str] = {c: "sum" for c in sum_cols}
    agg.update({c: "first" for c in others})
    if not agg:
        return work.drop_duplicates(subset=keys, keep="first", ignore_index=True)
    return work.groupby(keys, dropna=False).agg(agg).reset_index()


def _timestamp_fingerprint_repr(ts: pd.Timestamp) -> str:
    """Stable calendar-ish representation for hashing join-key values."""
    ts = pd.Timestamp(ts)
    try:
        if ts.normalize() == ts:
            return ts.strftime("%Y-%m-%d")
    except Exception:
        pass
    return ts.isoformat()


def _canonical_key_scalar_for_fingerprint(v: Any) -> Any:
    """Normalize join-key scalars so preview and full collate share the same group_id hash."""
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    try:
        if isinstance(v, (float, np.floating)) and np.isnan(float(v)):
            return None
    except (TypeError, ValueError):
        pass
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(v, (np.integer, int)) and not isinstance(v, bool):
        iv = int(v)
        if 35000 <= iv <= 60000:
            dt = pd.to_datetime(iv, unit="d", origin="1899-12-30", errors="coerce")
            if pd.notna(dt):
                return _timestamp_fingerprint_repr(pd.Timestamp(dt))
        return iv

    if isinstance(v, (float, np.floating)) and not isinstance(v, bool):
        xf = float(v)
        if np.isnan(xf):
            return None
        if abs(xf - round(xf)) < 1e-9:
            iv = int(round(xf))
            if 35000 <= iv <= 60000:
                dt = pd.to_datetime(iv, unit="d", origin="1899-12-30", errors="coerce")
                if pd.notna(dt):
                    return _timestamp_fingerprint_repr(pd.Timestamp(dt))
            return iv
        return xf

    if isinstance(v, str):
        s = v.strip()
        if not s:
            return ""
        ts = pd.to_datetime(s, errors="coerce", dayfirst=False, utc=False)
        if pd.notna(ts):
            return _timestamp_fingerprint_repr(pd.Timestamp(ts))
        return s

    if isinstance(v, pd.Timestamp):
        return _timestamp_fingerprint_repr(v)

    if isinstance(v, datetime.datetime):
        return _timestamp_fingerprint_repr(pd.Timestamp(v))

    if isinstance(v, datetime.date):
        return v.isoformat()

    if isinstance(v, np.datetime64):
        ts = pd.Timestamp(v)
        if pd.isna(ts):
            return None
        return _timestamp_fingerprint_repr(ts)

    if hasattr(v, "item"):
        try:
            it = v.item()
        except Exception:
            it = v
        if it is not v:
            return _canonical_key_scalar_for_fingerprint(it)

    return str(v)


def _duplicate_group_fingerprint(row: pd.Series, keys: List[str]) -> str:
    payload: Dict[str, Any] = {}
    for k in keys:
        if k not in row.index:
            continue
        payload[k] = _canonical_key_scalar_for_fingerprint(row[k])
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _subgroup_is_full_row_duplicate(sub: pd.DataFrame, keys: List[str]) -> bool:
    """True when all non-key, non-boolean columns are constant within the group."""
    for c in sub.columns:
        if c in keys:
            continue
        # resolve_duplicate_key_groups injects row order; it must not affect full-row equality.
        if c == "__stack_order__":
            continue
        if pd.api.types.is_bool_dtype(sub[c].dtype):
            continue
        if int(sub[c].nunique(dropna=True)) > 1:
            return False
    return True


def resolve_duplicate_key_groups(
    df: pd.DataFrame,
    group_keys: Sequence[str],
    decisions: Dict[str, Any],
    *,
    default_exact: str = "treat_duplicate",
    default_partial: str = "keep_both",
) -> pd.DataFrame:
    """Resolve overlapping join-key rows using per-group analyst decisions.

    * **exact** (full-row match): ``treat_duplicate`` keeps first row in stack order;
      ``treat_separate`` keeps all rows.
    * **partial** (key match, differing measures): ``keep_source_1`` / ``keep_source_2``
      keep one stacked row; ``keep_both`` sums numeric measures (same as aggregate).
    """
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    keys = [k for k in group_keys if k in df.columns]
    if not keys:
        return df.copy()

    work = df.copy().reset_index(drop=True)
    work["__stack_order__"] = work.index
    decisions = dict(decisions or {})
    parts: List[pd.DataFrame] = []

    for _key, sub in work.groupby(keys, dropna=False):
        if len(sub) < 2:
            parts.append(sub)
            continue
        sub = sub.sort_values("__stack_order__")
        gid = _duplicate_group_fingerprint(sub.iloc[0], keys)
        entry = decisions.get(gid)
        if isinstance(entry, dict):
            category = str(entry.get("category") or "").strip()
            action = str(entry.get("action") or "").strip()
        else:
            category = action = ""
        if category not in ("exact", "partial") or not action:
            if _subgroup_is_full_row_duplicate(sub, keys):
                category = "exact"
                action = default_exact if default_exact in ("treat_duplicate", "treat_separate") else "treat_duplicate"
            else:
                category = "partial"
                action = (
                    default_partial
                    if default_partial in ("keep_source_1", "keep_source_2", "keep_both")
                    else "keep_both"
                )
        if category == "exact":
            if action == "treat_separate":
                parts.append(sub)
            else:
                parts.append(sub.head(1))
        else:
            if action == "keep_source_1":
                parts.append(sub.head(1))
            elif action == "keep_source_2":
                parts.append(sub.tail(1))
            elif action == "keep_both":
                sub_agg = sub.drop(columns=["__stack_order__"], errors="ignore")
                parts.append(aggregate_duplicate_keys(sub_agg, group_keys=keys, sum_columns=None))
            else:
                parts.append(sub.head(1))

    out = pd.concat(parts, ignore_index=True, sort=False)
    if "__stack_order__" in out.columns:
        out = out.drop(columns=["__stack_order__"])
    return out


def execute_collation_tool(name: str, df: pd.DataFrame, params: Dict[str, Any]) -> pd.DataFrame:
    """Dispatch merge tool by id (``collation.*``)."""
    if df is None:
        return pd.DataFrame()
    n = str(name or "").strip()
    p = params or {}
    if n == "collation.reorder_columns":
        return reorder_columns(df, p.get("column_order"))
    if n == "collation.drop_duplicate_rows":
        return drop_duplicate_rows(df, subset=p.get("subset"), keep=str(p.get("keep") or "first"))
    if n == "collation.aggregate_duplicate_keys":
        return aggregate_duplicate_keys(
            df,
            group_keys=list(p.get("group_keys") or []),
            sum_columns=p.get("sum_columns"),
        )
    if n == "collation.resolve_duplicate_key_groups":
        return resolve_duplicate_key_groups(
            df,
            list(p.get("group_keys") or []),
            dict(p.get("decisions") or {}),
            default_exact=str(p.get("default_exact") or "treat_duplicate"),
            default_partial=str(p.get("default_partial") or "keep_both"),
        )
    return df


def collation_ordered_columns_for_merge(
    df: pd.DataFrame,
    template: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Prefer target-template: date, other uid dimensions, supporting columns, then metrics; then remaining columns."""
    if df is None or df.empty:
        return []
    if template and isinstance(template, dict):
        preferred = collation_merge_column_key_order(template)
        if preferred:
            have = list(df.columns)
            lower = {str(c).lower(): c for c in have}
            ordered: List[str] = []
            seen: set[str] = set()
            for name in preferred:
                pick = name if name in have else lower.get(str(name).strip().lower())
                if pick and pick not in seen:
                    seen.add(pick)
                    ordered.append(pick)
            for c in have:
                if c not in seen:
                    ordered.append(c)
            return ordered
    return canonical_column_order(df)


def canonical_column_order(df: pd.DataFrame) -> List[str]:
    """Stable union order: known metrics last, rest sorted."""
    if df is None or df.empty:
        return []
    cols = list(df.columns)
    preferred_tail = [
        "impressions",
        "clicks",
        "spend",
        "cost",
        "conversions",
        "views",
        "value",
    ]
    head = sorted(c for c in cols if c not in preferred_tail)
    tail = [c for c in preferred_tail if c in cols]
    return head + tail
