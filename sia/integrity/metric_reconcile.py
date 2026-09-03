"""Post-tool metric reconciliation and catastrophic integrity checks."""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from sia.agent.target_template_utils import normalize_target_template

METRIC_TOLERANCE_PCT = float(os.environ.get("INTEGRITY_METRIC_TOLERANCE_PCT", "0.001"))
METRIC_TOLERANCE_ABS = float(os.environ.get("INTEGRITY_METRIC_TOLERANCE_ABS", "0.01"))
ROW_LOSS_PAUSE_PCT = float(os.environ.get("INTEGRITY_ROW_LOSS_PAUSE_PCT", "0.80"))

AGGREGATE_SUM_TOOLS = frozenset(
    {
        "transform.aggregate_weekly",
        "transform.date_range_to_weekly",
        "transform.expand_date_range_to_weekly",
        "collation.aggregate_duplicate_keys",
    }
)

_INTEGRITY_CHECKPOINT_TITLES = {
    "zero_rows": "Critical: all rows removed",
    "mass_row_loss": "Large data loss detected",
    "metric_column_wiped": "Metric column removed or emptied",
    "unpivot_shrink": "Unpivot produced fewer rows than expected",
    "aggregate_sum_mismatch": "Aggregate totals do not reconcile",
}


def sums_close(
    before: float,
    after: float,
    tol_pct: float = METRIC_TOLERANCE_PCT,
    tol_abs: float = METRIC_TOLERANCE_ABS,
) -> bool:
    if before == after:
        return True
    if abs(before) < tol_abs and abs(after) < tol_abs:
        return True
    denom = max(abs(before), abs(after), tol_abs)
    return abs(before - after) / denom <= tol_pct


def _sum_numeric(df: pd.DataFrame, col: str) -> Optional[float]:
    if col not in df.columns:
        return None
    series = pd.to_numeric(df[col], errors="coerce")
    if series.notna().sum() == 0:
        return None
    return float(series.sum(skipna=True))


def template_metric_columns_in_frame(state: Dict[str, Any], df: pd.DataFrame) -> List[str]:
    tpl = normalize_target_template(state.get("target_template") or {})
    scope = tpl.get("x_scope") if isinstance(tpl.get("x_scope"), dict) else {}
    metrics = [str(x) for x in (scope.get("metrics") or []) if str(x).strip()]
    lower_map = {str(c).strip().lower(): c for c in df.columns if str(c).strip()}
    present: List[str] = []
    for m in metrics:
        resolved = lower_map.get(m.lower())
        if resolved and resolved not in present:
            present.append(resolved)
    return present


def _aggregate_sum_columns(params: Dict[str, Any], state: Dict[str, Any], df: pd.DataFrame) -> List[str]:
    """Columns that should preserve global sum after aggregation."""
    metric_rules = params.get("metric_rules")
    if isinstance(metric_rules, dict) and metric_rules:
        sum_cols: List[str] = []
        lower_present = {str(c).strip().lower(): c for c in df.columns}
        for col, rule in metric_rules.items():
            rule_str = str(rule or "sum").strip().lower()
            if rule_str in ("sum", ""):
                resolved = lower_present.get(str(col).strip().lower())
                if resolved:
                    sum_cols.append(resolved)
        if sum_cols:
            return sum_cols

    value_cols = params.get("value_cols")
    if isinstance(value_cols, list) and value_cols:
        from sia.tools.transformation_tools import TransformationTools

        resolved, _ = TransformationTools._resolve_columns(df, value_cols)
        return [str(c) for c in resolved if str(c) in df.columns]

    return template_metric_columns_in_frame(state, df)


def check_aggregate_sum_reconciliation(
    before_df: pd.DataFrame,
    after_df: pd.DataFrame,
    tool_name: str,
    params: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    cols = _aggregate_sum_columns(params, state, before_df)
    if not cols:
        return None

    mismatches: List[Dict[str, Any]] = []
    for col in cols:
        if col not in before_df.columns:
            continue
        b = _sum_numeric(before_df, col)
        if b is None:
            continue
        after_col = _resolve_col(after_df, col)
        a = _sum_numeric(after_df, after_col) if after_col else None
        if a is None or not sums_close(b, a):
            mismatches.append({"column": col, "expected": b, "actual": a})

    if not mismatches:
        return None

    parts = [
        f"{m['column']}: expected sum {m['expected']:.4g}, got {m['actual']}"
        for m in mismatches
    ]
    return {
        "type": "integrity_violation",
        "subtype": "aggregate_sum_mismatch",
        "tool": tool_name,
        "item": "metric_reconciliation",
        "confidence": 0.2,
        "details": f"Aggregate sum mismatch after {tool_name}: " + "; ".join(parts),
        "checks": mismatches,
        "requires_hitl": True,
    }


def _column_has_non_blank_values(series: pd.Series) -> bool:
    for value in series:
        if pd.isna(value):
            continue
        if str(value).strip():
            return True
    return False


def _lower_col_map(df: pd.DataFrame) -> Dict[str, str]:
    return {str(c).strip().lower(): str(c) for c in df.columns if str(c).strip()}


def _resolve_col(df: pd.DataFrame, name: str) -> Optional[str]:
    """Resolve a column name in *df* (exact match, then case-insensitive)."""
    if name is None or df is None or df.empty:
        return None
    s = str(name).strip()
    if not s:
        return None
    if s in df.columns:
        return s
    return _lower_col_map(df).get(s.lower())


def _template_metric_keys(state: Dict[str, Any]) -> List[str]:
    tpl = normalize_target_template(state.get("target_template") or {})
    scope = tpl.get("x_scope") if isinstance(tpl.get("x_scope"), dict) else {}
    return [str(x) for x in (scope.get("metrics") or []) if str(x).strip()]


def _rename_targets_for_source(params: Dict[str, Any], source_col: str) -> List[str]:
    """Target column names declared for *source_col* in a rename mapping."""
    mapping = (
        params.get("mapping")
        or params.get("column_mapping")
        or params.get("name_mapping")
        or params.get("column_map")
        or params.get("rename_map")
        or {}
    )
    if not isinstance(mapping, dict):
        return []
    src_lower = str(source_col).strip().lower()
    targets: List[str] = []
    for raw_src, raw_tgt in mapping.items():
        if str(raw_src).strip().lower() != src_lower:
            continue
        tgt = str(raw_tgt).strip()
        if tgt and tgt not in targets:
            targets.append(tgt)
    return targets


def _approved_mappings(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    rows = list(state.get("approved_mappings") or cp.get("approved_mappings") or [])
    return [r for r in rows if isinstance(r, dict)]


def _metric_identity_keys(name: str) -> set:
    """Case-insensitive metric identity, including simple singular/plural (spend/spends)."""
    s = str(name or "").strip().lower()
    if not s:
        return set()
    keys = {s}
    if s.endswith("s") and len(s) > 1:
        keys.add(s[:-1])
    else:
        keys.add(f"{s}s")
    return keys


def _names_match_metric(left: str, right: str) -> bool:
    return bool(_metric_identity_keys(left) & _metric_identity_keys(right))


def _metric_name_variants(name: str) -> List[str]:
    s = str(name or "").strip()
    if not s:
        return []
    out: List[str] = []
    seen: set = set()
    for key in _metric_identity_keys(s):
        for cand in (key, key.upper(), key.capitalize()):
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
    if s not in seen:
        out.insert(0, s)
    return out


def _before_column_for_metric(
    state: Dict[str, Any],
    before_df: pd.DataFrame,
    metric_key: str,
) -> Optional[str]:
    """Locate a before-frame column that carries *metric_key* (template, mapping, or fuzzy)."""
    for variant in _metric_name_variants(metric_key):
        resolved = _resolve_col(before_df, variant)
        if resolved:
            return resolved
    for item in _approved_mappings(state):
        tgt = str(item.get("target_column") or "").strip()
        if not tgt or tgt.lower() in {"no match", "none"}:
            continue
        if not _names_match_metric(tgt, metric_key):
            continue
        src = str(item.get("source_column") or "").strip()
        for candidate in (src, tgt):
            resolved = _resolve_col(before_df, candidate)
            if resolved:
                return resolved
    for col in before_df.columns:
        if _names_match_metric(str(col), metric_key):
            return str(col)
    return None


def _after_columns_for_metric(
    before_col: str,
    metric_key: str,
    after_df: pd.DataFrame,
    *,
    state: Optional[Dict[str, Any]] = None,
    norm_tool: str = "",
    params: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Candidate columns in *after_df* that may carry the same metric as *before_col*."""
    params = params or {}
    state = state or {}
    seen: set = set()
    candidates: List[str] = []

    def _add(name: str) -> None:
        if not name:
            return
        resolved = _resolve_col(after_df, name)
        if resolved and resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

    for name in _metric_name_variants(before_col) + _metric_name_variants(metric_key):
        _add(name)
    for item in _approved_mappings(state):
        tgt = str(item.get("target_column") or "").strip()
        if not tgt or tgt.lower() in {"no match", "none"}:
            continue
        if not _names_match_metric(tgt, metric_key):
            continue
        src = str(item.get("source_column") or "").strip()
        _add(tgt)
        _add(src)
    if norm_tool == "transform.rename":
        for tgt in _rename_targets_for_source(params, before_col):
            _add(tgt)
        for item in _approved_mappings(state):
            src = str(item.get("source_column") or "").strip()
            for tgt in _rename_targets_for_source(params, src):
                _add(tgt)
    for col in after_df.columns:
        if _names_match_metric(str(col), metric_key) or _names_match_metric(str(col), before_col):
            _add(str(col))
    return candidates


def _metric_has_meaningful_values(df: pd.DataFrame, col: str) -> bool:
    total = _sum_numeric(df, col)
    if total is not None and abs(total) >= METRIC_TOLERANCE_ABS:
        return True
    if col in df.columns and _column_has_non_blank_values(df[col]):
        return True
    return False


def _metric_columns_wiped(
    before_df: pd.DataFrame,
    after_df: pd.DataFrame,
    metric_cols: List[str],
    *,
    state: Optional[Dict[str, Any]] = None,
    norm_tool: str = "",
    params: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return template metric columns that had data before the tool but not after.

    Matching is case-insensitive and rename-aware so ``Impressions`` → ``IMPRESSIONS``
    does not false-positive as a wipe.
    """
    del metric_cols  # metrics resolved from template keys + before frame
    state = state or {}
    params = params or {}
    wiped: List[str] = []
    metric_keys = _template_metric_keys(state)
    if not metric_keys:
        return wiped

    for metric_key in metric_keys:
        before_col = _before_column_for_metric(state, before_df, metric_key)
        if before_col is None:
            continue
        if not _metric_has_meaningful_values(before_df, before_col):
            continue

        after_candidates = _after_columns_for_metric(
            before_col,
            metric_key,
            after_df,
            state=state,
            norm_tool=norm_tool,
            params=params,
        )
        if not after_candidates:
            wiped.append(before_col)
            continue

        preserved = any(_metric_has_meaningful_values(after_df, col) for col in after_candidates)
        if not preserved:
            wiped.append(before_col)

    return wiped


def check_post_tool_integrity(
    before_df: Optional[pd.DataFrame],
    after_df: Optional[pd.DataFrame],
    tool_name: str,
    params: Dict[str, Any],
    state: Dict[str, Any],
    *,
    rows_before: int,
    rows_after: int,
    is_destructive_fn: Callable[[str], bool],
    norm_tool: str,
) -> Optional[Dict[str, Any]]:
    """Return an integrity violation dict if the job should pause for HITL."""
    if before_df is None or after_df is None:
        return None

    if rows_after == 0 and rows_before > 0:
        return {
            "type": "integrity_violation",
            "subtype": "zero_rows",
            "tool": tool_name,
            "item": "row_count",
            "confidence": 0.1,
            "details": f"CRITICAL: Tool {tool_name} deleted ALL data ({rows_before} -> 0 rows).",
            "rows_before": rows_before,
            "rows_after": rows_after,
            "requires_hitl": True,
        }

    if "unpivot" in tool_name.lower() and rows_after < rows_before:
        return {
            "type": "integrity_violation",
            "subtype": "unpivot_shrink",
            "tool": tool_name,
            "item": "row_count",
            "confidence": 0.3,
            "details": (
                f"Unpivot tool {tool_name} resulted in FEWER rows "
                f"({rows_before} -> {rows_after}). Possible misconfiguration."
            ),
            "rows_before": rows_before,
            "rows_after": rows_after,
            "requires_hitl": True,
        }

    if is_destructive_fn(tool_name) and rows_before > 0:
        loss_ratio = (rows_before - rows_after) / rows_before
        if loss_ratio > ROW_LOSS_PAUSE_PCT:
            return {
                "type": "integrity_violation",
                "subtype": "mass_row_loss",
                "tool": tool_name,
                "item": "data_loss",
                "confidence": 0.5,
                "details": (
                    f"Destructive tool {tool_name} removed {loss_ratio * 100:.1f}% of data. "
                    "Verify if this was intended."
                ),
                "rows_before": rows_before,
                "rows_after": rows_after,
                "pct_removed": round(loss_ratio * 100, 1),
                "requires_hitl": True,
            }

    metric_cols = template_metric_columns_in_frame(state, before_df)
    if norm_tool != "transform.rename":
        wiped = _metric_columns_wiped(
            before_df,
            after_df,
            metric_cols,
            state=state,
            norm_tool=norm_tool,
            params=params,
        )
        if wiped:
            return {
                "type": "integrity_violation",
                "subtype": "metric_column_wiped",
                "tool": tool_name,
                "item": "metric_columns",
                "confidence": 0.15,
                "details": (
                    f"Tool {tool_name} removed or emptied metric column(s): {', '.join(wiped)}"
                ),
                "columns_affected": wiped,
                "requires_hitl": True,
            }

    if norm_tool in AGGREGATE_SUM_TOOLS:
        return check_aggregate_sum_reconciliation(
            before_df, after_df, tool_name, params, state
        )

    return None


def build_integrity_review_checkpoint(
    violation: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Checkpoint payload for Review queue when post-tool integrity guardrails fire."""
    subtype = str(violation.get("subtype") or "integrity")
    severity = "critical" if subtype in {"zero_rows", "metric_column_wiped"} else "high"
    if subtype == "mass_row_loss":
        severity = "high"
    return {
        "checkpoint_id": f"integrity_{uuid.uuid4().hex[:12]}",
        "checkpoint_type": "checksum_failure",
        "title": _INTEGRITY_CHECKPOINT_TITLES.get(subtype, "Data integrity review"),
        "description": str(violation.get("details") or "Processing paused for data integrity review."),
        "severity": severity,
        "trigger_reason": str(violation.get("details") or "integrity_review"),
        "trigger_data": {
            "integrity_violation": violation,
            "tool": violation.get("tool"),
            "subtype": subtype,
        },
        "available_actions": ["approve", "investigate", "cancel"],
        "recommended_action": "investigate",
        "current_step": str(state.get("current_step") or "execute_tools"),
        "iteration": int(state.get("iteration") or 0),
        "confidence": float(violation.get("confidence") or 0.3),
        "created_at": datetime.now().isoformat(),
        "resolved": False,
    }


def integrity_violation_to_hitl_state(
    violation: Dict[str, Any],
    iteration_data: Optional[pd.DataFrame],
    state: Dict[str, Any],
    *,
    tools_history_slice: List[Dict[str, Any]],
    deferred_post_collate: List[Dict[str, Any]],
    warnings: List[str],
    low_confidence_items: List[Dict[str, Any]],
    tool_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Build execute_tools early-return payload for integrity HITL pause."""
    merged_low_conf = list(low_confidence_items) + [violation]
    if tool_index is not None:
        violation = dict(violation)
        violation["tool_index"] = int(tool_index)
    checkpoint = build_integrity_review_checkpoint(violation, state)
    if tool_index is not None:
        checkpoint["trigger_data"] = dict(checkpoint.get("trigger_data") or {})
        checkpoint["trigger_data"]["tool_index"] = int(tool_index)
    prior_checkpoints = [
        cp for cp in (state.get("hitl_checkpoints") or [])
        if isinstance(cp, dict) and not cp.get("resolved", False)
    ]
    return {
        "current_df": iteration_data,
        "current_frame": {
            "rows": len(iteration_data) if iteration_data is not None else 0,
            "cols": len(iteration_data.columns) if iteration_data is not None else 0,
        },
        "iteration": state["iteration"],
        "hitl_pending_approval": True,
        "hitl_pause_type": "integrity_review",
        "integrity_violations": [violation],
        "integrity_resume_after_tool_index": tool_index,
        "integrity_pause_tool": violation.get("tool"),
        "hitl_checkpoints": prior_checkpoints + [checkpoint],
        "low_confidence_items": merged_low_conf,
        "requires_review": True,
        "review_reason": violation.get("details", "Data integrity review required"),
        "warnings": warnings,
        "message": violation.get("details", "Paused for data integrity review"),
        "tools_history": tools_history_slice,
        "deferred_post_collate_tools": deferred_post_collate,
        "trace_steps": [
            {
                "step": "hitl_pause",
                "reason": "integrity_review",
                "tool": violation.get("tool"),
                "subtype": violation.get("subtype"),
            }
        ],
    }
