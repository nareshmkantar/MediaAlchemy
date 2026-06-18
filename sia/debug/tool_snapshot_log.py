"""Log tool executions with CSV snapshots (same pattern as execute_tools in nodes)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import pandas as pd

if TYPE_CHECKING:
    from sia.debug.llm_observer import LLMObserver


def _records_head(df: pd.DataFrame, limit: int) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    clean = df.where(pd.notnull(df), None)
    return clean.head(limit).to_dict(orient="records")


def _snapshot_filename(tool_name: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(tool_name))
    return f"{int(time.time() * 1000)}_{safe}.csv"


def log_dataframe_tool_to_observer(
    observer: Optional["LLMObserver"],
    tool_name: str,
    params: Dict[str, Any],
    result: str,
    df: Optional[pd.DataFrame],
    *,
    success: bool = True,
    duration_ms: float = 0.0,
    input_preview: Optional[Dict[str, Any]] = None,
    output_preview_extra: Optional[Dict[str, Any]] = None,
    pipeline_stage: Optional[str] = None,
) -> None:
    """Write optional CSV snapshot under ``runtime/snapshots`` and append to observer."""
    if observer is None or not getattr(observer, "enabled", True):
        return

    output_preview: Dict[str, Any] = {}
    snapshot_path: Optional[str] = None

    if df is not None and isinstance(df, pd.DataFrame):
        sample_rows = _records_head(df, 12)
        output_preview = {
            "shape": str(df.shape),
            "rows": int(len(df)),
            "columns": int(len(df.columns)),
            "column_order": list(df.columns),
            "sample": sample_rows,
        }
        if len(df) > 0:
            try:
                root = Path(__file__).resolve().parent.parent.parent
                snapshot_dir = str(root / "runtime" / "snapshots")
                os.makedirs(snapshot_dir, exist_ok=True)
                filename = _snapshot_filename(tool_name)
                filepath = os.path.join(snapshot_dir, filename)
                df.to_csv(filepath, index=False)
                snapshot_path = filename
            except Exception:
                pass
    if output_preview_extra:
        output_preview = {**(output_preview or {}), **output_preview_extra}

    ps = pipeline_stage
    if ps is None and str(tool_name).startswith("collation."):
        ps = "consolidation"

    observer.log_tool_execution(
        tool_name=tool_name,
        params=params or {},
        result=result,
        success=success,
        duration_ms=duration_ms,
        input_preview=input_preview or {},
        output_preview=output_preview,
        snapshot_path=snapshot_path,
        pipeline_stage=ps,
    )
