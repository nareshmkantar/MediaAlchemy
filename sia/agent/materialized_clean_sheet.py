"""
Materialized "clean template" workbooks for downstream LLM stages.

After Guided Setup (demarcation bounds) and semantic mapping, we persist a
narrow Excel file (scoped slice + mapping/business-rule context applied) so
structure analysis and planning see less noise than the raw upload.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from sia.agent.scoped_source import build_scoped_source, load_scoped_dataframe

logger = logging.getLogger(__name__)

MATERIALIZED_SHEET_NAME = "Clean"
CLEANED_ROOT = Path(__file__).resolve().parent.parent.parent / "runtime" / "cleaned_templates"


def _sanitize_token(value: str) -> str:
    text = re.sub(r"[^\w.\-]+", "_", str(value or "").strip(), flags=re.UNICODE)
    text = text.strip("._") or "source"
    return text[:120]


def _mappings_for_source(job: Dict[str, Any], source_id: str) -> List[Dict[str, Any]]:
    sid = str(source_id or "")
    return [
        dict(m)
        for m in (job.get("mapping_registry") or [])
        if isinstance(m, dict) and str(m.get("source_id") or "") == sid
    ]


def _scoped_source_for_job(job: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    reg = (job.get("source_scope_registry") or {}).get(source_id) or {}
    if isinstance(reg, dict) and reg:
        return dict(reg)
    legacy = job.get("scoped_source") or {}
    return dict(legacy) if isinstance(legacy, dict) else {}


def drop_discarded_mapping_columns(
    df: pd.DataFrame,
    approved_mappings: List[Dict[str, Any]],
) -> pd.DataFrame:
    """
    Remove columns the user marked Discard in semantic mapping.

    We intentionally do **not** rename to template targets here: the mapping registry
    still refers to original ``source_column`` names, and ``resolve_mapping`` applies
    the full mapping context again when the graph runs on this workbook.
    """
    result = df.copy()
    discards: List[str] = []
    for mapping in approved_mappings or []:
        if not isinstance(mapping, dict):
            continue
        decision = str(mapping.get("decision", "")).strip().lower()
        source_column = mapping.get("source_column")
        if decision == "discard" and source_column and source_column in result.columns:
            discards.append(str(source_column))
    if discards:
        result = result.drop(columns=discards, errors="ignore")
    return result


def build_clean_dataframe(
    file_path: str,
    sheet_name: str,
    scoped_source: Optional[Dict[str, Any]],
    approved_mappings: List[Dict[str, Any]],
) -> Tuple[Optional[pd.DataFrame], Optional[Dict[str, Any]]]:
    """Load scoped slice from the upload, then drop discarded columns (noise reduction only)."""
    try:
        prepared_df, resolved_scope = load_scoped_dataframe(
            str(file_path),
            sheet_name,
            scoped_source,
        )
    except Exception as exc:
        logger.warning("Materialize: load_scoped_dataframe failed: %s", exc)
        return None, None

    if prepared_df is None or prepared_df.empty:
        return None, resolved_scope

    cleaned = drop_discarded_mapping_columns(prepared_df, approved_mappings)
    return cleaned, resolved_scope


def write_materialized_workbook(job_id: str, source_id: str, df: pd.DataFrame) -> Path:
    out_dir = CLEANED_ROOT / _sanitize_token(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_sanitize_token(source_id)}_clean.xlsx"
    # Single-sheet workbook used as the synthetic "upload" for downstream graph entry.
    df.to_excel(path, sheet_name=MATERIALIZED_SHEET_NAME, index=False)
    return path


def scoped_source_for_materialized_workbook(
    sheet_name: str,
    nrows: int,
    ncols: int,
) -> Dict[str, Any]:
    """Full-sheet scope over the materialized workbook (no demarcation sub-crop)."""
    return build_scoped_source(
        sheet_name=sheet_name,
        total_rows=max(int(nrows or 0), 1),
        total_cols=max(int(ncols or 0), 1),
        blocks=None,
        user_selected_header_row=0,
    )


def materialize_one_source(job: Dict[str, Any], job_id: str, source_id: str) -> Optional[Dict[str, Any]]:
    """Build and write a cleaned template for one source. Returns metadata dict or None."""
    source = None
    for item in job.get("source_registry") or []:
        if isinstance(item, dict) and str(item.get("source_id")) == str(source_id):
            source = item
            break
    if not source:
        logger.warning("Materialize: source %s not in registry for job %s", source_id, job_id)
        return None

    file_path = str(source.get("file_path") or job.get("file_path") or "")
    sheet_name = str(source.get("sheet_name") or (job.get("scoped_source") or {}).get("sheet_name") or "")
    if not file_path or not sheet_name:
        logger.warning("Materialize: missing file_path or sheet_name for %s", source_id)
        return None

    mappings = _mappings_for_source(job, source_id)
    if not mappings:
        logger.info("Materialize: skip %s — no mapping_registry rows yet", source_id)
        return None

    scoped = _scoped_source_for_job(job, source_id)

    df, _resolved = build_clean_dataframe(
        file_path,
        sheet_name,
        scoped,
        mappings,
    )
    if df is None or df.empty:
        logger.warning("Materialize: empty dataframe for %s", source_id)
        return None

    path = write_materialized_workbook(job_id, source_id, df)
    merged_metric_ranges = list((_resolved or {}).get("merged_metric_ranges") or [])
    meta = {
        "path": str(path),
        "sheet_name": MATERIALIZED_SHEET_NAME,
        "rows": int(len(df)),
        "cols": int(len(df.columns)),
        "source_sheet": sheet_name,
        "source_id": str(source_id),
        "source_workbook_path": file_path,
        "merged_metric_ranges": merged_metric_ranges,
    }
    logger.info(
        "Materialize: wrote %s (%s rows × %s cols) for job=%s source=%s",
        path,
        meta["rows"],
        meta["cols"],
        job_id,
        source_id,
    )
    return meta


def refresh_materialized_clean_templates(
    job: Dict[str, Any],
    job_id: str,
    source_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Refresh on-disk cleaned templates and ``job['materialized_clean_templates']``.

    Returns a small summary dict for logging/HTTP responses.
    """
    if not isinstance(job, dict):
        return {"updated": [], "errors": ["invalid job"]}

    targets: List[str] = []
    if source_ids:
        targets = [str(s) for s in source_ids if s]
    else:
        seen = set()
        for m in job.get("mapping_registry") or []:
            if isinstance(m, dict) and m.get("source_id"):
                sid = str(m["source_id"])
                if sid not in seen:
                    seen.add(sid)
                    targets.append(sid)

    updated: List[str] = []
    errors: List[str] = []
    templates: Dict[str, Any] = dict(job.get("materialized_clean_templates") or {})

    for sid in targets:
        try:
            meta = materialize_one_source(job, job_id, sid)
            if meta:
                templates[sid] = meta
                updated.append(sid)
        except Exception as exc:
            err = f"{sid}: {exc}"
            errors.append(err)
            logger.warning("Materialize failed: %s", err)

    job["materialized_clean_templates"] = templates
    return {"updated": updated, "errors": errors, "templates": templates}


def resolve_processing_workbook(
    job: Dict[str, Any],
    source_id: Optional[str],
    original_file_path: str,
    original_sheet: str,
    original_scoped_source: Optional[Dict[str, Any]],
) -> Tuple[str, str, Dict[str, Any], bool]:
    """
    Pick the workbook the LangGraph pipeline should load.

    Returns (file_path, sheet_name, scoped_source, used_materialized).
    """
    sid = str(source_id or "")
    try:
        from sia.agent.multi_block_sheet import is_block_virtual_source_id

        if is_block_virtual_source_id(sid):
            return original_file_path, original_sheet, dict(original_scoped_source or {}), False
    except Exception:
        pass
    templates = job.get("materialized_clean_templates") or {}
    meta = templates.get(sid) if sid else None
    if not meta or not isinstance(meta, dict):
        return original_file_path, original_sheet, dict(original_scoped_source or {}), False

    path = Path(str(meta.get("path") or ""))
    if not path.is_file():
        logger.warning("Materialize: missing file %s — falling back to raw upload", path)
        return original_file_path, original_sheet, dict(original_scoped_source or {}), False

    sheet = str(meta.get("sheet_name") or MATERIALIZED_SHEET_NAME)
    rows = int(meta.get("rows") or 0)
    cols = int(meta.get("cols") or 0)
    if rows <= 0 or cols <= 0:
        logger.warning("Materialize: invalid shape metadata for %s — fallback", sid)
        return original_file_path, original_sheet, dict(original_scoped_source or {}), False

    scoped = scoped_source_for_materialized_workbook(sheet, rows, cols)
    orig = dict(original_scoped_source or {})
    merged = list(
        orig.get("merged_metric_ranges")
        or meta.get("merged_metric_ranges")
        or []
    )
    if merged:
        scoped["merged_metric_ranges"] = merged
    scoped["source_workbook_path"] = original_file_path
    scoped["source_sheet_name"] = original_sheet
    return str(path), sheet, scoped, True
