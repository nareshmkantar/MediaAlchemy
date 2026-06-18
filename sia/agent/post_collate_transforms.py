"""Run transform tools deferred until after multi-source union/collation."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from sia.tools.tool_validator import sort_tool_calls_by_pipeline_stage, validate_tool_call
from sia.tools.transformation_tools import TransformationTools

logger = logging.getLogger(__name__)

# Keep in sync with sia.agent.nodes.POST_COLLATE_DEFERRED_GRAIN_TOOLS
POST_COLLATE_DEFERRED_GRAIN_TOOLS = frozenset(
    {
        "transform.expand_period_to_daily",
        "transform.infer_granularity_expand_to_daily",
        "transform.date_range_to_weekly",
        "transform.expand_date_range_to_daily",
        "transform.expand_date_range_to_weekly",
        "transform.aggregate_weekly",
    }
)


def _deferred_tool_dedupe_key(tool: str, params: Dict[str, Any]) -> Tuple[str, str]:
    """Canonical JSON key for deferred-tool deduplication."""
    from sia.agent.target_template_utils import sanitize_aggregate_weekly_params

    p = dict(params or {})
    if tool == "transform.aggregate_weekly":
        p = sanitize_aggregate_weekly_params(p)
        gb = sorted(
            {str(x).strip().lower() for x in (p.get("group_by_cols") or []) if str(x).strip()}
        )
        if gb:
            p["group_by_cols"] = gb
        rules = p.get("metric_rules")
        if isinstance(rules, dict):
            p["metric_rules"] = {
                str(k): str(v) for k, v in sorted(rules.items(), key=lambda kv: str(kv[0]).lower())
            }
    return tool, json.dumps(p, sort_keys=True, default=str)


def _merge_aggregate_weekly_deferred_params(param_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Union group_by + metric_rules across per-source deferred weekly steps."""
    from sia.agent.target_template_utils import sanitize_aggregate_weekly_params

    merged: Dict[str, Any] = {}
    group_seen: set[str] = set()
    group_order: List[str] = []
    metric_rules: Dict[str, str] = {}
    for raw in param_list:
        p = sanitize_aggregate_weekly_params(dict(raw or {}))
        if not merged:
            merged = dict(p)
        else:
            if not merged.get("date_col") and p.get("date_col"):
                merged["date_col"] = p.get("date_col")
        for col in list(p.get("group_by_cols") or []):
            name = str(col or "").strip()
            key = name.lower()
            if name and key not in group_seen:
                group_seen.add(key)
                group_order.append(name)
        for m, rule in (p.get("metric_rules") or {}).items():
            if m is not None:
                metric_rules[str(m)] = str(rule)
    if group_order:
        merged["group_by_cols"] = group_order
    if metric_rules:
        merged["metric_rules"] = metric_rules
    merged.setdefault("drop_original_date", True)
    return sanitize_aggregate_weekly_params(merged)


def merge_deferred_post_collate_tool_lists(by_source: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Stable merge of per-source deferred tool calls; dedupe by tool + params JSON."""
    seen: set[Tuple[str, str]] = set()
    out: List[Dict[str, Any]] = []
    aggregate_params: List[Dict[str, Any]] = []
    for sid in sorted(by_source.keys()):
        for tc in by_source.get(sid) or []:
            if not isinstance(tc, dict):
                continue
            tool = str(tc.get("tool") or "").strip()
            if not tool:
                continue
            if tool == "transform.aggregate_weekly":
                aggregate_params.append(dict(tc.get("params") or {}))
                continue
            key = _deferred_tool_dedupe_key(tool, dict(tc.get("params") or {}))
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(tc))
    if aggregate_params:
        merged_aw = _merge_aggregate_weekly_deferred_params(aggregate_params)
        out.append({"tool": "transform.aggregate_weekly", "params": merged_aw})
    return sort_tool_calls_by_pipeline_stage(out)


def _run_deferred_grain_tool(
    df: pd.DataFrame,
    tool_name: str,
    params: Dict[str, Any],
):
    """Execute one deferred grain tool on a collated dataframe."""
    from sia.agent.target_template_utils import sanitize_aggregate_weekly_params

    p = sanitize_aggregate_weekly_params(dict(params or {}))
    if tool_name == "transform.aggregate_weekly":
        kwargs: Dict[str, Any] = {
            "date_col": p.get("date_col"),
            "group_by_cols": p.get("group_by_cols"),
            "metric_rules": p.get("metric_rules"),
            "value_cols": p.get("value_cols"),
            "drop_original_date": bool(p.get("drop_original_date", True)),
            "row_filters": p.get("row_filters"),
        }
        if "week_start_col" in p:
            kwargs["week_start_col"] = p.get("week_start_col")
        if p.get("week_end_col"):
            kwargs["week_end_col"] = p.get("week_end_col")
        return TransformationTools.aggregate_weekly(df, **kwargs)
    if tool_name == "transform.infer_granularity_expand_to_daily":
        return TransformationTools.infer_granularity_expand_to_daily(
            df,
            date_col=p.get("date_col"),
            value_cols=list(p.get("value_cols") or []),
            id_cols=p.get("id_cols"),
            date_column=str(p.get("date_column") or "calendar_date"),
            min_confidence=float(p.get("min_confidence", 0.35)),
            row_filters=p.get("row_filters"),
        )
    if tool_name == "transform.expand_period_to_daily":
        return TransformationTools.expand_period_to_daily(
            df,
            date_col=p.get("date_col"),
            value_cols=list(p.get("value_cols") or []),
            input_granularity=str(p.get("input_granularity") or "monthly"),
            id_cols=p.get("id_cols"),
            date_column=str(p.get("date_column") or "calendar_date"),
            row_filters=p.get("row_filters"),
        )
    if tool_name in (
        "transform.date_range_to_weekly",
        "transform.expand_date_range_to_daily",
        "transform.expand_date_range_to_weekly",
    ):
        return TransformationTools.date_range_to_weekly(
            df,
            start_date_col=p.get("start_date_col"),
            end_date_col=p.get("end_date_col"),
            value_cols=list(p.get("value_cols") or []),
            id_cols=p.get("id_cols"),
            granularity=str(p.get("granularity") or "weekly"),
            week_start_col=p.get("week_start_col"),
            week_end_col=p.get("week_end_col"),
            days_in_period_col=str(p.get("days_in_period_col") or "days_in_week"),
            date_column=str(p.get("date_column") or "calendar_date"),
            row_filters=p.get("row_filters"),
        )
    return None


def _layout_repair_state(
    target_template: Optional[Dict[str, Any]],
    approved_mappings: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    return {
        "target_template": target_template or {},
        "approved_mappings": list(approved_mappings or []),
        "context_packet": {
            "target_template": target_template or {},
            "approved_mappings": list(approved_mappings or []),
        },
    }


def apply_combined_frame_final_layout(
    df: Optional[pd.DataFrame],
    *,
    target_template: Optional[Dict[str, Any]] = None,
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
    observer: Optional[Any] = None,
) -> Tuple[Optional[pd.DataFrame], List[Dict[str, Any]]]:
    """Reorder + sort the collated frame to template uid order (ignores LLM partial column_order)."""
    trace_bits: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return df, trace_bits

    from sia.agent.nodes import _repair_final_layout_tool_params
    from sia.agent.target_template_utils import normalize_target_template

    tpl = normalize_target_template(target_template)
    if not tpl:
        return df, trace_bits

    try:
        from sia.debug.tool_snapshot_log import log_dataframe_tool_to_observer
    except Exception:
        log_dataframe_tool_to_observer = None  # type: ignore[assignment,misc]

    repair_state = _layout_repair_state(tpl, approved_mappings)
    work = df

    for tool_name, runner in (
        ("transform.reorder_columns", TransformationTools.reorder_columns_layout),
        ("transform.sort_rows", TransformationTools.sort_rows),
    ):
        params = _repair_final_layout_tool_params(
            repair_state,
            tool_name,
            {},
            work,
        )
        rows_before = len(work) if work is not None else 0
        t0 = time.time()
        if tool_name == "transform.reorder_columns":
            res = runner(work, column_order=list(params.get("column_order") or []))
        else:
            res = runner(
                work,
                sort_columns=list(params.get("sort_columns") or []),
                ascending=bool(params.get("ascending", True)),
            )
        dur_ms = (time.time() - t0) * 1000.0
        rows_after = len(work) if work is not None else 0
        ok = bool(res.success)
        if ok:
            work = res.data
            rows_after = len(work) if work is not None else 0
        trace_bits.append(
            {
                "step": "post_collate_transform",
                "tool": tool_name,
                "ok": ok,
                "message": (res.message or "")[:500],
                "rows_before": rows_before,
                "rows_after": rows_after,
            }
        )
        if log_dataframe_tool_to_observer and observer is not None:
            log_dataframe_tool_to_observer(
                observer,
                tool_name,
                params,
                (res.message or "")[:500] if ok else str(res.message or res)[:500],
                work if ok else work,
                success=ok,
                duration_ms=dur_ms,
                input_preview={"rows_before": rows_before, "post_collate_final_layout": True},
                output_preview_extra={
                    "rows_before": rows_before,
                    "rows_after": rows_after,
                    "row_delta": int(rows_after) - int(rows_before),
                },
                pipeline_stage="final_layout",
            )
    return work, trace_bits


def apply_deferred_post_collate_transforms(
    df: Optional[pd.DataFrame],
    deferred: List[Dict[str, Any]],
    *,
    observer: Optional[Any] = None,
    target_template: Optional[Dict[str, Any]] = None,
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
    apply_final_layout: bool = True,
) -> Tuple[Optional[pd.DataFrame], List[Dict[str, Any]]]:
    """
    Apply deferred grain transforms on the collated frame after duplicate_check.

    Runs tools in pipeline stage order (daily expansion before weekly rollup).
    When ``apply_final_layout`` is true, finishes with template reorder + sort on the
    combined frame (same repair rules as per-source ``execute_tools``).
    """
    trace_bits: List[Dict[str, Any]] = []
    if df is None:
        return df, trace_bits

    try:
        from sia.debug.tool_snapshot_log import log_dataframe_tool_to_observer
    except Exception:
        log_dataframe_tool_to_observer = None  # type: ignore[assignment,misc]

    work = df
    grain_only = [
        dict(tc)
        for tc in (deferred or [])
        if isinstance(tc, dict)
        and str(tc.get("tool") or "").strip() in POST_COLLATE_DEFERRED_GRAIN_TOOLS
    ]
    ordered = sort_tool_calls_by_pipeline_stage(grain_only)
    if not ordered and not apply_final_layout:
        return work, trace_bits
    for raw in ordered:
        tc = dict(raw)
        vr = validate_tool_call(tc)
        if not vr.valid:
            msg = f"Deferred tool validation failed: {vr.errors}"
            logger.warning(msg)
            trace_bits.append({"step": "post_collate_transform", "tool": tc.get("tool"), "ok": False, "message": msg})
            continue
        name = vr.normalized_tool_name or ""
        if name not in POST_COLLATE_DEFERRED_GRAIN_TOOLS:
            trace_bits.append(
                {
                    "step": "post_collate_transform",
                    "tool": name,
                    "ok": False,
                    "message": f"Unsupported deferred tool {name!r}",
                }
            )
            continue
        p = dict(vr.normalized_params or {})
        rows_before = len(work) if work is not None else 0
        t0 = time.time()
        runner = _run_deferred_grain_tool(work, name, p)
        dur_ms = (time.time() - t0) * 1000.0
        if runner is None:
            trace_bits.append(
                {
                    "step": "post_collate_transform",
                    "tool": name,
                    "ok": False,
                    "message": f"No runner for deferred tool {name!r}",
                }
            )
            continue
        res = runner
        rows_after = len(work) if work is not None else 0
        if res.success:
            work = res.data
            rows_after = len(work) if work is not None else 0
            trace_bits.append(
                {
                    "step": "post_collate_transform",
                    "tool": name,
                    "ok": True,
                    "message": (res.message or "")[:500],
                    "rows_before": rows_before,
                    "rows_after": rows_after,
                }
            )
        else:
            trace_bits.append(
                {
                    "step": "post_collate_transform",
                    "tool": name,
                    "ok": False,
                    "message": (res.message or str(res))[:500],
                    "rows_before": rows_before,
                    "rows_after": rows_after,
                }
            )
        if log_dataframe_tool_to_observer and observer is not None:
            log_dataframe_tool_to_observer(
                observer,
                name,
                p,
                (res.message or "")[:500] if res.success else str(res.message or res)[:500],
                work if res.success else work,
                success=bool(res.success),
                duration_ms=dur_ms,
                input_preview={"rows_before": rows_before, "deferred_post_collate": True},
                output_preview_extra={
                    "rows_before": rows_before,
                    "rows_after": rows_after,
                    "row_delta": int(rows_after) - int(rows_before),
                },
                pipeline_stage="consolidation",
            )
    if apply_final_layout:
        work, layout_trace = apply_combined_frame_final_layout(
            work,
            target_template=target_template,
            approved_mappings=approved_mappings,
            observer=observer,
        )
        trace_bits.extend(layout_trace)
    return work, trace_bits
