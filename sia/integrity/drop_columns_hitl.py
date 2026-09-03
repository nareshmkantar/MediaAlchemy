"""Smart HITL for planner-initiated transform.drop_columns (skip user-discarded cols)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from sia.agent.context_packet import KEEP_DECISIONS, mapping_is_excluded
from sia.agent.target_template_utils import (
    normalize_target_template,
    pre_transform_target_columns,
    primary_target_columns,
)
from sia.tools.transformation_tools import DeletionPreview, TransformationTools


def user_excluded_column_names(state: Dict[str, Any]) -> Set[str]:
    """Source column names the user already marked Discard in schema mapping."""
    excluded: Set[str] = set()
    for mapping in list(state.get("approved_mappings") or []):
        if not isinstance(mapping, dict):
            continue
        if not mapping_is_excluded(mapping):
            continue
        src = str(mapping.get("source_column") or mapping.get("column_name") or "").strip()
        if src:
            excluded.add(src.lower())

    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    summary = cp.get("approved_mapping_summary") if isinstance(cp.get("approved_mapping_summary"), dict) else {}
    for col in list(summary.get("excluded_columns") or []):
        name = str(col or "").strip()
        if name:
            excluded.add(name.lower())

    return excluded


def _template_metric_names(state: Dict[str, Any]) -> Set[str]:
    tpl = normalize_target_template(state.get("target_template") or {})
    scope = tpl.get("x_scope") if isinstance(tpl.get("x_scope"), dict) else {}
    return {str(x).strip().lower() for x in (scope.get("metrics") or []) if str(x).strip()}


def _mapped_keep_source_names(state: Dict[str, Any]) -> Set[str]:
    keep_sources: Set[str] = set()
    for mapping in list(state.get("approved_mappings") or []):
        if not isinstance(mapping, dict):
            continue
        decision = str(mapping.get("decision", "")).strip().lower()
        src = str(mapping.get("source_column") or "").strip()
        tgt = str(mapping.get("target_column") or "").strip()
        if not src or not tgt or tgt.lower() == "no match":
            continue
        if decision in KEEP_DECISIONS:
            keep_sources.add(src.lower())
    return keep_sources


def repair_drop_columns_params(state: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Strip protected template/mapped targets from drop_columns params."""
    out = dict(params or {})
    cols = out.get("columns")
    if not isinstance(cols, list):
        return out
    tpl = state.get("target_template") or {}
    protected: Set[str] = set()
    if isinstance(tpl, dict) and tpl:
        norm_tpl = normalize_target_template(tpl)
        protected.update(str(c) for c in primary_target_columns(norm_tpl))
        protected.update(str(c) for c in pre_transform_target_columns(norm_tpl))
    for item in list(state.get("approved_mappings") or []):
        if not isinstance(item, dict):
            continue
        tgt = str(item.get("target_column") or "").strip()
        if tgt:
            protected.add(tgt)
    prot_lower = {str(x).strip().lower() for x in protected if str(x).strip()}
    filtered = [
        str(c)
        for c in cols
        if str(c).strip() and str(c).strip().lower() not in prot_lower
    ]
    out["columns"] = filtered
    return out


def _resolve_drop_column_names(df: pd.DataFrame, params: Dict[str, Any]) -> List[str]:
    cols = params.get("columns")
    if not isinstance(cols, list) or df is None or df.empty:
        return []
    resolved, _ = TransformationTools._resolve_columns(df, cols)
    return [str(c) for c in resolved if str(c).strip()]


def _column_has_meaningful_numeric_total(df: pd.DataFrame, col: str, min_abs: float = 0.01) -> bool:
    if col not in df.columns:
        return False
    series = pd.to_numeric(df[col], errors="coerce")
    if series.notna().sum() == 0:
        return False
    return abs(float(series.sum(skipna=True))) >= min_abs


def drop_columns_requires_hitl(
    state: Dict[str, Any],
    params: Dict[str, Any],
    df: Optional[pd.DataFrame],
) -> Tuple[bool, List[str]]:
    if df is None or df.empty:
        return False, []

    # Use planner params as-is for HITL (repair strips protected cols at execution only).
    to_drop = _resolve_drop_column_names(df, params)
    if not to_drop:
        return False, []

    user_excluded = user_excluded_column_names(state)
    template_metrics = _template_metric_names(state)
    mapped_keep = _mapped_keep_source_names(state)

    sensitive: List[str] = []
    for col in to_drop:
        key = col.strip().lower()
        if key in user_excluded:
            continue
        if key in template_metrics:
            sensitive.append(col)
            continue
        if key in mapped_keep:
            sensitive.append(col)
            continue
        if _column_has_meaningful_numeric_total(df, col):
            sensitive.append(col)

    return bool(sensitive), sensitive


def generate_drop_columns_hitl_preview(
    df: pd.DataFrame,
    tool_name: str,
    params: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[DeletionPreview]:
    requires, sensitive = drop_columns_requires_hitl(state, params, df)
    if not requires:
        return None

    to_drop = _resolve_drop_column_names(df, params)
    present = [c for c in to_drop if c in df.columns]
    if not present:
        return None

    sample = df[present].head(10).to_dict(orient="records")
    return DeletionPreview(
        tool_name=tool_name,
        tool_description="Drop columns not already excluded by schema mapping",
        rows_to_delete=[],
        columns_to_delete=present,
        sample_deleted_data=sample,
        full_deleted_data=df[present].copy(),
        reason=(
            f"Planner drop_columns would remove {len(present)} column(s) including "
            f"sensitive: {sensitive[:5]}"
        ),
        impact_summary=f"Will remove {len(present)} column(s): {', '.join(present[:8])}",
    )
