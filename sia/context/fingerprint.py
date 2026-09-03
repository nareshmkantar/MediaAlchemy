"""Context artifact fingerprinting (layout + file + sheet scope)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from sia.storage.fingerprint import compute_fingerprint


def _compact_layout_blocks(
    job: Dict[str, Any],
    *,
    source_id: str,
    sheet_name: Optional[str],
) -> List[Dict[str, Any]]:
    layout_registry = list(job.get("layout_registry") or [])
    rows = [
        block
        for block in layout_registry
        if isinstance(block, dict) and str(block.get("source_id") or "") == str(source_id)
        and (not sheet_name or block.get("sheet_name") in {None, sheet_name})
    ]
    compact: List[Dict[str, Any]] = []
    for block in sorted(rows, key=lambda row: str(row.get("block_id") or "")):
        coords = block.get("coordinates") if isinstance(block.get("coordinates"), dict) else block
        if not isinstance(coords, dict):
            coords = {}
        compact.append(
            {
                "block_id": block.get("block_id"),
                "decision": block.get("decision"),
                "category": block.get("block_category") or block.get("category"),
                "start_row": coords.get("start_row"),
                "end_row": coords.get("end_row"),
                "start_col": coords.get("start_col"),
                "end_col": coords.get("end_col"),
            }
        )
    return compact


def layout_fingerprint_payload(
    job: Dict[str, Any],
    *,
    source_id: str,
    sheet_name: Optional[str],
) -> Dict[str, Any]:
    """Stable payload for demarcation / layout changes."""
    scope_registry = job.get("source_scope_registry") if isinstance(job.get("source_scope_registry"), dict) else {}
    scoped = scope_registry.get(str(source_id)) if isinstance(scope_registry, dict) else None
    if not isinstance(scoped, dict):
        scoped = job.get("scoped_source") if isinstance(job.get("scoped_source"), dict) else {}
    return {
        "sheet_name": str(sheet_name or "").strip(),
        "source_id": str(source_id or "").strip(),
        "layout_blocks": _compact_layout_blocks(job, source_id=source_id, sheet_name=sheet_name),
        "scope": {
            "header_row": scoped.get("header_row"),
            "scope_type": scoped.get("scope_type"),
            "analysis_bounds": scoped.get("analysis_bounds"),
        },
    }


def compute_source_context_fingerprint(
    job: Dict[str, Any],
    *,
    source_id: str,
    sheet_name: Optional[str],
    source_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Fingerprint inputs that invalidate cached context snippets / interpreted context."""
    sm = dict(source_metadata or {})
    file_path = str(sm.get("file_path") or job.get("file_path") or "").strip()
    file_token = file_path
    if file_path:
        try:
            path = Path(file_path)
            if path.is_file():
                stat = path.stat()
                file_token = f"{file_path}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            file_token = file_path
    return compute_fingerprint(
        file_token,
        layout_fingerprint_payload(job, source_id=source_id, sheet_name=sheet_name),
    )
