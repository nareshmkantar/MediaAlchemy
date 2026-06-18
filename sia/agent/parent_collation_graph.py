"""
Parent LangGraph: run only when multiple per-source frames are collated.

Uses ``approved_file_relationships`` for baseline stack/join (via
``_collate_frames_baseline``), then analyzes the combined table and runs a
small deterministic tool plan (reorder columns, drop vs aggregate duplicates
on approved join keys). This keeps merge semantics observable and extensible;
an LLM planner can later replace :func:`plan_merge_tools_node`.
"""

from __future__ import annotations

import bisect
import operator
import time
from typing import Annotated, Any, Dict, List, NotRequired, Optional, Sequence, Tuple, TypedDict

import pandas as pd
from langgraph.graph import END, StateGraph

from sia.agent.relationships import _collate_frames_baseline, _union_stack_column_intersection
from sia.debug.llm_observer import LLMObserver
from sia.debug.tool_snapshot_log import log_dataframe_tool_to_observer
from sia.tools.collation_tools import (
    _duplicate_group_fingerprint,
    _subgroup_is_full_row_duplicate,
    collation_ordered_columns_for_merge,
    execute_collation_tool,
)


class CollationState(TypedDict, total=False):
    """State for the multi-source collation parent graph (in-memory only)."""

    frames_by_source: Dict[str, pd.DataFrame]
    relationships: List[Dict[str, Any]]
    current_df: Optional[pd.DataFrame]
    merge_diagnostics: Dict[str, Any]
    planned_merge_tools: List[Dict[str, Any]]
    union_column_intersection: List[str]
    trace_steps: Annotated[List[Dict[str, Any]], operator.add]
    warnings: Annotated[List[str], operator.add]
    errors: Annotated[List[str], operator.add]
    observer: NotRequired[Any]
    target_template: NotRequired[Optional[Dict[str, Any]]]


def _union_join_keys(relationships: Sequence[Dict[str, Any]]) -> List[str]:
    keys: List[str] = []
    seen: set[str] = set()
    for rel in relationships or []:
        if str(rel.get("relationship_kind") or "").lower() != "union":
            continue
        for k in rel.get("join_keys") or []:
            s = str(k).strip()
            if s and s not in seen:
                seen.add(s)
                keys.append(s)
    return keys


def _resolve_key_names_to_columns(keys: Sequence[str], columns: Sequence[str]) -> List[str]:
    """Map declared join key names to actual dataframe columns (case-insensitive)."""
    if not keys or columns is None:
        return []
    col_list = [str(c) for c in columns]
    lower_map = {c.lower(): c for c in col_list}
    out: List[str] = []
    seen: set[str] = set()
    for raw in keys:
        k = str(raw).strip()
        if not k:
            continue
        picked = lower_map.get(k.lower()) or (k if k in col_list else "")
        if not picked or picked in seen:
            continue
        seen.add(picked)
        out.append(picked)
    return out


VALID_UNION_DUPLICATE_MERGE_MODES = frozenset({"auto", "keep_first_per_key", "sum_measures_per_key"})


def normalize_union_duplicate_merge_mode(value: Any) -> str:
    m = str(value or "auto").strip()
    return m if m in VALID_UNION_DUPLICATE_MERGE_MODES else "auto"


def union_duplicate_merge_mode_from_relationships(relationships: Sequence[Dict[str, Any]]) -> str:
    """Return ``duplicate_merge_mode`` from the first union relationship, default ``auto``."""
    for rel in relationships or []:
        if str(rel.get("relationship_kind") or "").lower() != "union":
            continue
        return normalize_union_duplicate_merge_mode(rel.get("duplicate_merge_mode"))
    return "auto"


def resolve_duplicate_merge_action(dup_count: int, numeric_conflict: bool, mode: Any) -> str:
    """Which duplicate-key tool to run: ``none``, ``drop_duplicate_rows``, or ``aggregate_duplicate_keys``."""
    m = normalize_union_duplicate_merge_mode(mode)
    if dup_count <= 0:
        return "none"
    if m == "sum_measures_per_key":
        return "aggregate_duplicate_keys"
    if m == "keep_first_per_key":
        return "drop_duplicate_rows"
    if numeric_conflict:
        return "aggregate_duplicate_keys"
    return "drop_duplicate_rows"


def defaults_for_unlisted_duplicate_groups(mode: Any, has_numeric_conflict: bool) -> Tuple[str, str]:
    """(default_exact_action, default_partial_action) for groups missing from explicit decisions."""
    m = normalize_union_duplicate_merge_mode(mode)
    exactly = "treat_duplicate"
    if m == "keep_first_per_key":
        return exactly, "keep_source_1"
    if m == "sum_measures_per_key":
        return exactly, "keep_both"
    return exactly, "keep_both" if has_numeric_conflict else "keep_source_1"


def union_duplicate_key_group_decisions_from_relationships(relationships: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge per-group decision maps from every union relationship that carries them.

    Earlier union edges with empty maps no longer wipe decisions from later union edges.
    """
    merged: Dict[str, Any] = {}
    for rel in relationships or []:
        if str(rel.get("relationship_kind") or "").lower() != "union":
            continue
        d = rel.get("duplicate_key_group_decisions")
        if not isinstance(d, dict) or not d:
            continue
        for k, v in d.items():
            if not isinstance(v, dict):
                continue
            kk = str(k).strip()
            if not kk:
                continue
            cat = str(v.get("category") or "").strip()
            act = str(v.get("action") or "").strip()
            if cat in ("exact", "partial") and act:
                merged[kk] = {"category": cat, "action": act}
    return merged


def build_suggested_duplicate_key_decisions(
    exact_groups: Sequence[Dict[str, Any]],
    partial_groups: Sequence[Dict[str, Any]],
    mode: Any,
    has_numeric_conflict: bool,
) -> Dict[str, Dict[str, str]]:
    de, dp = defaults_for_unlisted_duplicate_groups(mode, has_numeric_conflict)
    out: Dict[str, Dict[str, str]] = {}
    for g in exact_groups:
        gid = str(g.get("group_id") or "").strip()
        if gid:
            ae = de if de in ("treat_duplicate", "treat_separate") else "treat_duplicate"
            out[gid] = {"category": "exact", "action": ae}
    for g in partial_groups:
        gid = str(g.get("group_id") or "").strip()
        if gid:
            pa = dp if dp in ("keep_source_1", "keep_source_2", "keep_both") else "keep_both"
            out[gid] = {"category": "partial", "action": pa}
    return out


def enumerate_duplicate_key_groups_for_review(
    stacked: pd.DataFrame,
    keys: List[str],
    *,
    union_break_before_row: Sequence[int],
    source_labels: Sequence[str],
    max_each: int = 25,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split duplicate-key groups into full-row matches vs partial (measure mismatch) for HITL UI."""
    use = [k for k in keys if k in stacked.columns]
    if not use or stacked is None or stacked.empty:
        return [], []
    breaks_sorted = sorted(int(x) for x in (union_break_before_row or []) if int(x) > 0)
    labels = [str(x) for x in (source_labels or [])]

    def source_label_for_pos(pos: int) -> str:
        bi = bisect.bisect_right(breaks_sorted, int(pos))
        if labels and 0 <= bi < len(labels):
            return labels[bi]
        if labels:
            return labels[min(bi, len(labels) - 1)]
        return f"Source {bi + 1}"

    exact: List[Dict[str, Any]] = []
    partial: List[Dict[str, Any]] = []
    for _, sub in stacked.groupby(use, dropna=False):
        if len(sub) < 2:
            continue
        sub_sorted = sub.sort_index()
        gid = _duplicate_group_fingerprint(sub_sorted.iloc[0], use)
        full = _subgroup_is_full_row_duplicate(sub_sorted, use)
        rows_payload: List[Dict[str, Any]] = []
        for j in range(min(6, len(sub_sorted))):
            pos = int(sub_sorted.index[j])
            sr = sub_sorted.iloc[j]
            row_d = {str(c): sr[c] for c in stacked.columns}
            row_d["_stack_position"] = pos
            row_d["_source_index"] = int(bisect.bisect_right(breaks_sorted, pos)) + 1
            row_d["_source_label"] = source_label_for_pos(pos)
            rows_payload.append(row_d)
        key_disp = " | ".join(f"{k}={sub_sorted.iloc[0][k]}" for k in use)
        entry: Dict[str, Any] = {"group_id": gid, "key_display": key_disp, "rows": rows_payload}
        if full:
            if len(exact) < max_each:
                exact.append(entry)
        else:
            if len(partial) < max_each:
                partial.append(entry)
    return exact, partial


def _column_is_measure_for_conflict(df: pd.DataFrame, c: str) -> bool:
    """Whether a non-key column is treated as a measure when scanning duplicate-key groups."""
    if c not in df.columns:
        return False
    s = df[c]
    if pd.api.types.is_bool_dtype(s):
        return False
    if pd.api.types.is_numeric_dtype(s):
        return True
    if pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
        nn = int(s.notna().sum())
        if nn == 0:
            return False
        coerced = pd.to_numeric(s, errors="coerce")
        parsable = int((coerced.notna() & s.notna()).sum())
        return parsable >= max(1, int(0.90 * nn))
    return False


def _measure_values_for_conflict_compare(s: pd.Series) -> Optional[pd.Series]:
    """Numeric values used to detect measure disagreement (handles number-like strings)."""
    if pd.api.types.is_bool_dtype(s.dtype):
        return None
    if pd.api.types.is_numeric_dtype(s.dtype):
        return pd.to_numeric(s, errors="coerce")
    if pd.api.types.is_object_dtype(s.dtype) or pd.api.types.is_string_dtype(s.dtype):
        return pd.to_numeric(s, errors="coerce")
    return None


def _numeric_measure_conflict_columns(df: pd.DataFrame, keys: List[str]) -> List[str]:
    """Non-key columns where duplicate key groups disagree on measure values.

    Booleans are ignored (often file flags). Object/string columns count as measures only when
    most values parse as numbers (e.g. cost read as text from Excel) so key clashes on metrics
    are not mislabeled as safe full-row duplicates.
    """
    use = [k for k in keys if k in df.columns]
    if not use or df.empty or len(df) < 2:
        return []
    measure_cols: List[str] = []
    for c in df.columns:
        if c in use:
            continue
        if _column_is_measure_for_conflict(df, c):
            measure_cols.append(str(c))

    conflicting: set[str] = set()
    for c in measure_cols:
        for _, sub in df.groupby(use, dropna=False):
            if len(sub) < 2:
                continue
            v = _measure_values_for_conflict_compare(sub[c])
            if v is None or int(v.notna().sum()) < 2:
                continue
            if int(v.nunique(dropna=True)) > 1:
                conflicting.add(c)
                break
    return sorted(conflicting)


def _needs_aggregate_on_keys(df: pd.DataFrame, keys: List[str]) -> bool:
    """True when duplicate key groups disagree on any compared measure (numeric or number-like strings)."""
    return bool(_numeric_measure_conflict_columns(df, keys))


def baseline_collate_node(state: CollationState) -> Dict[str, Any]:
    frames = state.get("frames_by_source") or {}
    rels = list(state.get("relationships") or [])
    nf = dict(frames)
    df = _collate_frames_baseline(nf, rels)
    union_inter = _union_stack_column_intersection(nf, rels)
    step = {
        "step": "parent_baseline_collate",
        "rows": int(len(df)) if df is not None else 0,
        "cols": int(len(df.columns)) if df is not None and not df.empty else 0,
        "sources": list(frames.keys()),
        "union_column_intersection": union_inter,
    }
    obs = state.get("observer")
    if obs:
        t0 = time.time()
        msg = f"rows={step['rows']}; cols={step['cols']}; sources={step.get('sources')!r}"
        if step.get("union_column_intersection"):
            msg += f"; union_column_intersection={step.get('union_column_intersection')!r}"
        log_dataframe_tool_to_observer(
            obs,
            "collation.union_stack",
            {"sources": list(frames.keys())},
            msg,
            df,
            duration_ms=(time.time() - t0) * 1000,
            input_preview={"union_column_intersection": step.get("union_column_intersection")},
        )
    return {
        "current_df": df,
        "trace_steps": [step],
        "union_column_intersection": union_inter,
    }


def diagnose_stacked_union_duplicates(
    df: Optional[pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
    union_column_intersection: Sequence[str],
) -> Dict[str, Any]:
    """Same duplicate-key scan as post-baseline collation (Trace: ``collation.duplicate_check``).

    Used by the relationship-review Union preview so analysts see which columns define
    "duplicate" rows and what will happen before merge tools run.
    """
    rels = list(relationships or [])
    diagnostics: Dict[str, Any] = {
        "union_join_keys": _union_join_keys(rels),
        "column_count": int(len(df.columns)) if df is not None and not df.empty else 0,
        "row_count": int(len(df)) if df is not None else 0,
    }
    if df is None or df.empty:
        diagnostics["duplicate_rows_on_keys"] = 0
        diagnostics["subset_for_dedupe"] = []
        diagnostics["numeric_conflict_columns"] = []
        diagnostics["numeric_conflict_on_duplicate_keys"] = False
        return diagnostics

    keys_from_rel = _resolve_key_names_to_columns(diagnostics["union_join_keys"], list(df.columns))
    intersection = [c for c in (union_column_intersection or []) if c in df.columns]
    keys = keys_from_rel
    if not keys and intersection:
        keys = intersection
    if not keys:
        keys = list(df.columns)
    subset = keys
    dup_count = int(df.duplicated(subset=subset).sum()) if subset else 0
    diagnostics["duplicate_rows_on_keys"] = dup_count
    diagnostics["subset_for_dedupe"] = subset
    conflict_cols = _numeric_measure_conflict_columns(df, subset) if dup_count > 0 else []
    diagnostics["numeric_conflict_columns"] = conflict_cols
    diagnostics["numeric_conflict_on_duplicate_keys"] = bool(dup_count > 0 and len(conflict_cols) > 0)
    return diagnostics


def analyze_merge_node(state: CollationState) -> Dict[str, Any]:
    df = state.get("current_df")
    rels = list(state.get("relationships") or [])
    uci = list(state.get("union_column_intersection") or [])
    diagnostics = diagnose_stacked_union_duplicates(df, rels, uci)
    obs = state.get("observer")
    if obs:
        t0 = time.time()
        dup = diagnostics.get("duplicate_rows_on_keys")
        subset = diagnostics.get("subset_for_dedupe") or []
        conflict = diagnostics.get("numeric_conflict_on_duplicate_keys")
        conflict_cols = diagnostics.get("numeric_conflict_columns") or []
        msg = (
            f"duplicate_rows_on_keys={dup!r}; subset_for_dedupe={subset!r}; "
            f"numeric_conflict_on_duplicate_keys={conflict!r}"
        )
        log_dataframe_tool_to_observer(
            obs,
            "collation.duplicate_check",
            {
                "union_join_keys": diagnostics.get("union_join_keys"),
                "row_count": diagnostics.get("row_count"),
                "column_count": diagnostics.get("column_count"),
            },
            msg,
            df,
            duration_ms=(time.time() - t0) * 1000,
            input_preview={"subset_for_dedupe": subset},
            output_preview_extra={
                "duplicate_rows_on_keys": dup,
                "numeric_conflict_on_duplicate_keys": conflict,
                "numeric_conflict_columns": conflict_cols,
            },
        )
    return {
        "merge_diagnostics": diagnostics,
        "trace_steps": [{"step": "parent_analyze_merge", **{k: diagnostics[k] for k in diagnostics}}],
    }


def plan_merge_tools_node(state: CollationState) -> Dict[str, Any]:
    df = state.get("current_df")
    diag = dict(state.get("merge_diagnostics") or {})
    planned: List[Dict[str, Any]] = []

    if df is not None and not df.empty:
        canon = collation_ordered_columns_for_merge(df, state.get("target_template"))
        if list(df.columns) != canon:
            planned.append(
                {
                    "tool": "collation.reorder_columns",
                    "params": {"column_order": canon},
                }
            )

    subset = list(diag.get("subset_for_dedupe") or [])
    dup_count = int(diag.get("duplicate_rows_on_keys") or 0)
    if dup_count > 0 and subset:
        decisions_map = union_duplicate_key_group_decisions_from_relationships(state.get("relationships") or [])
        if decisions_map:
            mode = union_duplicate_merge_mode_from_relationships(state.get("relationships") or [])
            de, dp = defaults_for_unlisted_duplicate_groups(
                mode,
                bool(diag.get("numeric_conflict_on_duplicate_keys")),
            )
            planned.append(
                {
                    "tool": "collation.resolve_duplicate_key_groups",
                    "params": {
                        "group_keys": subset,
                        "decisions": decisions_map,
                        "default_exact": de,
                        "default_partial": dp,
                    },
                }
            )
        else:
            mode = union_duplicate_merge_mode_from_relationships(state.get("relationships") or [])
            action = resolve_duplicate_merge_action(
                dup_count,
                bool(diag.get("numeric_conflict_on_duplicate_keys")),
                mode,
            )
            if action == "aggregate_duplicate_keys":
                planned.append(
                    {
                        "tool": "collation.aggregate_duplicate_keys",
                        "params": {"group_keys": subset, "sum_columns": None},
                    }
                )
            elif action == "drop_duplicate_rows":
                planned.append(
                    {
                        "tool": "collation.drop_duplicate_rows",
                        "params": {"subset": subset, "keep": "first"},
                    }
                )

    trace = {
        "step": "parent_plan_merge_tools",
        "planned_tool_count": len(planned),
        "tools": [p.get("tool") for p in planned],
        "planned": planned,
    }
    obs = state.get("observer")
    if obs and df is not None:
        if planned:
            for p in planned:
                t0 = time.time()
                tool = str(p.get("tool") or "")
                params = dict(p.get("params") or {})
                log_dataframe_tool_to_observer(
                    obs,
                    "collation.plan_merge",
                    params,
                    f"Planned: {tool}",
                    df,
                    duration_ms=(time.time() - t0) * 1000,
                    input_preview={"planned_tool": tool, "planned_tool_count": len(planned)},
                )
        else:
            t0 = time.time()
            log_dataframe_tool_to_observer(
                obs,
                "collation.plan_merge",
                {},
                "No merge tools planned (no duplicate key rows and column order already canonical)",
                df,
                duration_ms=(time.time() - t0) * 1000,
                output_preview_extra={"planned_tool_count": 0, "planned_tools": []},
            )
    return {"planned_merge_tools": planned, "trace_steps": [trace]}


def execute_merge_tools_node(state: CollationState) -> Dict[str, Any]:
    df = state.get("current_df")
    obs = state.get("observer")
    if df is None:
        if obs:
            log_dataframe_tool_to_observer(
                obs,
                "collation.execute_merge_tools",
                {},
                "skipped=true (no dataframe)",
                None,
                success=False,
            )
        return {"trace_steps": [{"step": "parent_execute_merge_tools", "skipped": True}]}
    planned = list(state.get("planned_merge_tools") or [])
    out = df.copy()
    steps: List[Dict[str, Any]] = []
    if not planned:
        if obs:
            log_dataframe_tool_to_observer(
                obs,
                "collation.execute_merge_tools",
                {},
                "No collation tools executed (plan was empty)",
                out,
                output_preview_extra={"executions": []},
            )
        return {
            "current_df": out,
            "trace_steps": [{"step": "parent_execute_merge_tools", "executions": []}],
        }
    for item in planned:
        tool = str(item.get("tool") or "")
        params = dict(item.get("params") or {})
        before = len(out)
        t0 = time.time()
        out = execute_collation_tool(tool, out, params)
        dur_ms = (time.time() - t0) * 1000
        steps.append(
            {
                "tool": tool,
                "params": params,
                "rows_before": before,
                "rows_after": len(out),
            }
        )
        if obs:
            out_pv: Dict[str, Any] = {"rows_before": before, "rows_after": len(out)}
            try:
                out_pv["row_delta"] = int(len(out)) - int(before)
            except (TypeError, ValueError):
                pass
            log_dataframe_tool_to_observer(
                obs,
                tool,
                params,
                f"rows_before={before} rows_after={len(out)}",
                out,
                duration_ms=dur_ms,
                input_preview={"rows_before": before},
                output_preview_extra=out_pv,
            )
    return {
        "current_df": out,
        "trace_steps": [{"step": "parent_execute_merge_tools", "executions": steps}],
    }


def verify_combined_node(state: CollationState) -> Dict[str, Any]:
    df = state.get("current_df")
    warns: List[str] = []
    if df is None or df.empty:
        warns.append("parent_collation: combined dataframe is empty after merge tools")
    step = {
        "step": "parent_verify_combined",
        "ok": df is not None and not df.empty,
        "rows": int(len(df)) if df is not None else 0,
    }
    obs = state.get("observer")
    if obs:
        t0 = time.time()
        log_dataframe_tool_to_observer(
            obs,
            "collation.verify_combined",
            {},
            f"ok={step['ok']!r}; rows={step['rows']!r}",
            df,
            success=bool(step.get("ok")),
            duration_ms=(time.time() - t0) * 1000,
            output_preview_extra={"ok": step.get("ok"), "rows": step.get("rows")},
        )
    return {"warnings": warns, "trace_steps": [step]}


def create_parent_collation_graph():
    workflow: StateGraph = StateGraph(CollationState)
    workflow.add_node("baseline_collate", baseline_collate_node)
    workflow.add_node("analyze_merge", analyze_merge_node)
    workflow.add_node("plan_merge_tools", plan_merge_tools_node)
    workflow.add_node("execute_merge_tools", execute_merge_tools_node)
    workflow.add_node("verify_combined", verify_combined_node)

    workflow.set_entry_point("baseline_collate")
    workflow.add_edge("baseline_collate", "analyze_merge")
    workflow.add_edge("analyze_merge", "plan_merge_tools")
    workflow.add_edge("plan_merge_tools", "execute_merge_tools")
    workflow.add_edge("execute_merge_tools", "verify_combined")
    workflow.add_edge("verify_combined", END)
    return workflow.compile()


def run_parent_collation_graph(
    norm_frames: Dict[str, pd.DataFrame],
    relationships: List[Dict[str, Any]],
    *,
    target_template: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run the parent collation graph; return df, trace steps, and hierarchical debug events.

    Debug events mirror ``execute_tools`` (CSV snapshots under ``runtime/snapshots``) and are
    merged into ``job['debug_events']`` with ``source_id=__collation__`` by the web layer.
    """
    col_obs = LLMObserver(run_id="collation", model_id="", enabled=True)
    app = create_parent_collation_graph()
    initial: CollationState = {
        "frames_by_source": dict(norm_frames),
        "relationships": list(relationships or []),
        "current_df": None,
        "merge_diagnostics": {},
        "planned_merge_tools": [],
        "union_column_intersection": [],
        "trace_steps": [],
        "warnings": [],
        "errors": [],
        "observer": col_obs,
        "target_template": dict(target_template) if target_template else None,
    }
    with col_obs.trace_span("collation.merge_sources", type="node", metadata={"phase": "collation"}):
        final = app.invoke(initial)
    df = final.get("current_df")
    steps = list(final.get("trace_steps") or [])
    out_df = df if df is not None else pd.DataFrame()
    return out_df, steps, col_obs.get_hierarchical_events()
