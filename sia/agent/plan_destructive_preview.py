"""Preview destructive plan steps before execution (plan review HITL)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from sia.agent.scoped_source import load_scoped_dataframe, resolve_layout_extract_params
from sia.tools.tool_validator import normalize_tool_name
from sia.tools.transformation_tools import (
    DeletionPreview,
    TransformationTools,
    generate_deletion_preview,
    is_tool_destructive,
)

_LAYOUT_EXTRACT = frozenset(
    {
        "layout.extract",
        "extract_data_block",
        "get_data_block",
        "extract_table",
        "xls.layout.extract",
    }
)


def _human_tool_title(tool_name: str) -> str:
    norm, _ = normalize_tool_name(str(tool_name or "").strip())
    if norm == "transform.filter_summaries":
        return "Remove subtotal / total rows"
    if norm == "transform.filter_empty":
        return "Remove empty rows"
    if norm == "transform.drop_columns":
        return "Drop columns"
    if norm == "transform.drop_blank_columns":
        return "Drop blank columns"
    return str(tool_name or "destructive step")


def resolve_plan_preview_dataframe(
    job: Dict[str, Any],
    pending_state: Dict[str, Any],
    *,
    resolve_job_source_fn,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Best-effort dataframe for simulating plan tools before destructive steps."""
    scoped = pending_state.get("scoped_source") or job.get("scoped_source") or {}
    source_id = pending_state.get("source_id") or job.get("mapping_source_id")
    source = resolve_job_source_fn(job, source_id=source_id)
    file_path = str(source.get("file_path") or job.get("file_path") or "")
    sheet_name = source.get("sheet_name") or scoped.get("sheet_name") or job.get("sheet_name")
    if not file_path:
        return None, "No source file path on the job."

    try:
        df, _ = load_scoped_dataframe(file_path, sheet_name, scoped)
        if df is not None and not df.empty:
            return df, f"Scoped sheet preview ({len(df)} rows × {len(df.columns)} columns)"
    except Exception as exc:
        return None, f"Could not load scoped preview data: {exc}"

    preview = list(job.get("data_preview") or [])
    if preview:
        try:
            frame = pd.DataFrame(preview)
            if not frame.empty:
                return frame, f"Job data preview ({len(frame)} rows — sample only)"
        except Exception:
            pass
    return None, "No preview rows available for this sheet."


def apply_tool_for_plan_preview(
    df: pd.DataFrame,
    tool_call: Dict[str, Any],
    scoped_source: Optional[Dict[str, Any]],
) -> pd.DataFrame:
    """Apply a single non-destructive plan step for preview simulation."""
    if df is None or df.empty:
        return df
    norm, _ = normalize_tool_name(str(tool_call.get("tool") or "").strip())
    params = dict(tool_call.get("params") or {})

    if norm in _LAYOUT_EXTRACT:
        resolved = resolve_layout_extract_params(params, scoped_source)
        end_row = int(resolved.get("end_row", max(0, len(df) - 1)))
        end_col = int(resolved.get("end_col", max(0, len(df.columns) - 1)))
        result = TransformationTools.extract_data_block(
            df,
            int(resolved.get("start_row", 0)),
            end_row,
            int(resolved.get("start_col", 0)),
            end_col,
            int(resolved.get("header_row", 0)),
        )
        if result.success and isinstance(result.data, pd.DataFrame) and not result.data.empty:
            return result.data
        return df

    if norm == "transform.rename":
        mapping = params.get("mapping") or params.get("column_mapping") or params.get("name_mapping") or {}
        if isinstance(mapping, dict) and mapping:
            result = TransformationTools.rename_columns(df, mapping)
            if result.success and isinstance(result.data, pd.DataFrame):
                return result.data
        return df

    return df


def _build_removal_visual_sample(
    working_df: pd.DataFrame,
    preview: DeletionPreview,
    tool_name: str,
    *,
    max_rows: int = 24,
) -> Optional[Dict[str, Any]]:
    rows_to_delete = list(preview.rows_to_delete or [])
    if not rows_to_delete and not (preview.sample_deleted_data or []):
        return None

    if preview.sample_deleted_data:
        removed_rows = [dict(r) for r in preview.sample_deleted_data[:max_rows]]
    else:
        try:
            removed_rows = working_df.loc[rows_to_delete[:max_rows]].to_dict(orient="records")
        except Exception:
            removed_rows = []

    if not removed_rows:
        return None

    for row in removed_rows:
        row["Outcome"] = "Would remove if you continue"

    cols = list(removed_rows[0].keys())
    if "Outcome" not in cols:
        cols.append("Outcome")

    norm, _ = normalize_tool_name(str(tool_name or preview.tool_name or ""))
    title = _human_tool_title(norm)
    return {
        "title": f"{title} — rows that would be removed",
        "subtitle": preview.impact_summary or preview.reason or "",
        "columns": cols,
        "rows": removed_rows,
    }


def compute_plan_destructive_impacts(
    job: Dict[str, Any],
    pending_state: Dict[str, Any],
    tool_calls: List[Dict[str, Any]],
    *,
    resolve_job_source_fn,
) -> Dict[str, Any]:
    """Simulate plan steps and build destructive impact previews for plan review UI."""
    calls = [t for t in (tool_calls or []) if isinstance(t, dict)]
    destructive_meta: List[Dict[str, Any]] = []
    for i, tc in enumerate(calls):
        tool = str(tc.get("tool") or "")
        if is_tool_destructive(tool):
            destructive_meta.append(
                {
                    "tool": tool,
                    "step_index": i,
                    "title": _human_tool_title(tool),
                    "description": str(tc.get("description") or "").strip(),
                }
            )

    if not destructive_meta:
        return {
            "preview_available": False,
            "preview_basis": "No destructive tools in this plan.",
            "destructive_tools": [],
            "impacts": [],
        }

    df, basis = resolve_plan_preview_dataframe(
        job, pending_state, resolve_job_source_fn=resolve_job_source_fn
    )
    if df is None or df.empty:
        return {
            "preview_available": False,
            "preview_basis": basis,
            "destructive_tools": destructive_meta,
            "impacts": [],
        }

    scoped = pending_state.get("scoped_source") or job.get("scoped_source") or {}
    working = df.copy()
    impacts: List[Dict[str, Any]] = []

    for i, tc in enumerate(calls):
        tool = str(tc.get("tool") or "")
        if is_tool_destructive(tool):
            preview = generate_deletion_preview(working, tc)
            if preview is None:
                continue
            payload = preview.to_dict()
            payload["plan_step_index"] = i
            payload["step_title"] = _human_tool_title(tool)
            payload["rows_before"] = len(working)
            delete_n = len(preview.rows_to_delete or [])
            col_n = len(preview.columns_to_delete or [])
            payload["rows_after"] = max(0, len(working) - delete_n)
            payload["visual_sample"] = _build_removal_visual_sample(working, preview, tool)
            payload["has_removals"] = bool(delete_n or col_n)
            impacts.append(payload)
        working = apply_tool_for_plan_preview(working, tc, scoped)

    return {
        "preview_available": True,
        "preview_basis": basis,
        "destructive_tools": destructive_meta,
        "impacts": impacts,
    }
