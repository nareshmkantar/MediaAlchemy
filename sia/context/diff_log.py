"""Context diff log: trace field/value changes across pipeline stages."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import pandas as pd

from sia.integrity.context_isolation import SOURCE_LOCAL_DIMENSION_COLUMNS

MAX_CONTEXT_DIFF_LOG = 200

JobLike = Union[Dict[str, Any], None]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_context_diff_log(job: JobLike) -> List[Dict[str, Any]]:
    if not isinstance(job, dict):
        return []
    log = job.get("context_diff_log")
    if not isinstance(log, list):
        log = []
        job["context_diff_log"] = log
    return log


def append_context_diff(
    job: JobLike,
    entry: Dict[str, Any],
    *,
    max_entries: int = MAX_CONTEXT_DIFF_LOG,
) -> Optional[Dict[str, Any]]:
    """Append one diff row; ring-buffer at ``max_entries``."""
    if not isinstance(job, dict):
        return None
    row = {
        "ts": _now_iso(),
        "stage": str(entry.get("stage") or "").strip(),
        "source_id": str(entry.get("source_id") or "").strip(),
        "field": str(entry.get("field") or "").strip(),
        "before": entry.get("before"),
        "after": entry.get("after"),
        "reason": str(entry.get("reason") or "").strip(),
        "hop": int(entry.get("hop") or 0),
    }
    if entry.get("meta") is not None:
        row["meta"] = entry.get("meta")
    log = ensure_context_diff_log(job)
    log.append(row)
    while len(log) > max(1, int(max_entries)):
        log.pop(0)
    return row


def log_value_change(
    job: JobLike,
    *,
    stage: str,
    source_id: str,
    field: str,
    before: Any,
    after: Any,
    reason: str = "",
    hop: int = 0,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Log a scalar or list change when ``before`` and ``after`` differ."""
    if before == after:
        return None
    if isinstance(before, list) and isinstance(after, list) and before == after:
        return None
    return append_context_diff(
        job,
        {
            "stage": stage,
            "source_id": source_id,
            "field": field,
            "before": before,
            "after": after,
            "reason": reason,
            "hop": hop,
            "meta": meta,
        },
    )


def distinct_column_values(df: Optional[pd.DataFrame], column: str) -> List[str]:
    """Sorted distinct non-empty values for a dimension column."""
    if df is None or getattr(df, "empty", True) or column not in df.columns:
        return []
    return sorted(
        {
            str(v).strip()
            for v in df[column].dropna().unique()
            if str(v).strip()
        }
    )


def log_dimension_column_snapshots(
    job: JobLike,
    *,
    stage: str,
    source_id: str,
    df_before: Optional[pd.DataFrame],
    df_after: Optional[pd.DataFrame],
    columns: Optional[Sequence[str]] = None,
    reason: str = "",
) -> List[Dict[str, Any]]:
    """Record ``distinct(col)`` before/after for dimension stamping or alignment."""
    logged: List[Dict[str, Any]] = []
    cols = list(columns or SOURCE_LOCAL_DIMENSION_COLUMNS)
    for col in cols:
        before = distinct_column_values(df_before, col)
        after = distinct_column_values(df_after, col)
        if before == after:
            continue
        row = log_value_change(
            job,
            stage=stage,
            source_id=source_id,
            field=col,
            before=before,
            after=after,
            reason=reason or f"{stage} dimension snapshot",
            meta={"kind": "dimension_snapshot"},
        )
        if row:
            logged.append(row)
    return logged


def log_scoped_fields_built(
    job: JobLike,
    *,
    source_id: str,
    fields: Mapping[str, Any],
    stage: str = "packet_build",
) -> List[Dict[str, Any]]:
    """Log interpreted/scoped fields when a context packet is built."""
    logged: List[Dict[str, Any]] = []
    for name, value in dict(fields or {}).items():
        val = str(value or "").strip()
        if not val:
            continue
        row = log_value_change(
            job,
            stage=stage,
            source_id=str(source_id),
            field=str(name),
            before=None,
            after=val,
            reason="scoped field built",
        )
        if row:
            logged.append(row)
    return logged


def log_rebind_actions(
    job: JobLike,
    *,
    source_id: str,
    actions: Sequence[str],
    stage: str = "plan_rebind",
) -> List[Dict[str, Any]]:
    """Parse ``rebound field: 'a' → 'b'`` notes or log raw action lines."""
    logged: List[Dict[str, Any]] = []
    for action in actions or []:
        text = str(action or "").strip()
        if not text:
            continue
        field = ""
        before = None
        after = None
        if text.lower().startswith("rebound "):
            body = text[8:].strip()
            if ":" in body:
                field, rest = body.split(":", 1)
                field = field.strip()
                if "→" in rest:
                    parts = rest.split("→", 1)
                    before = parts[0].strip().strip("'\"")
                    after = parts[1].split("(", 1)[0].strip().strip("'\"")
        row = log_value_change(
            job,
            stage=stage,
            source_id=str(source_id),
            field=field or "plan_literal",
            before=before,
            after=after,
            reason=text,
        )
        if row:
            logged.append(row)
    return logged


def filter_context_diff_log(
    job: JobLike,
    *,
    source_id: Optional[str] = None,
    field: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    log = list(ensure_context_diff_log(job))
    if source_id:
        sid = str(source_id).strip()
        log = [r for r in log if str(r.get("source_id") or "") == sid]
    if field:
        fname = str(field).strip().lower()
        log = [r for r in log if str(r.get("field") or "").strip().lower() == fname]
    if stage:
        st = str(stage).strip().lower()
        log = [r for r in log if str(r.get("stage") or "").strip().lower() == st]
    return log[-max(1, int(limit)) :]


def build_debug_export_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    """Support bundle subset for debug JSON download."""
    job_id = job.get("id")
    log_text = None
    try:
        from sia.debug.processing_log import read_processing_log_text

        if job_id:
            log_text = read_processing_log_text(str(job_id))
    except Exception:
        log_text = None
    return {
        "job_id": job_id,
        "filename": job.get("filename"),
        "status": job.get("status"),
        "steps": list(job.get("steps") or []),
        "job_debug_events": list(job.get("job_debug_events") or []),
        "processing_log": log_text,
        "context_diff_log": list(ensure_context_diff_log(job)),
        "source_registry": job.get("source_registry") or [],
        "pipeline_evals": job.get("pipeline_evals"),
        "context_artifact_cache": job.get("context_artifact_cache"),
    }
