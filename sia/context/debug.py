"""Debug payloads for per-source context inspection."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sia.agent.context_packet import CONTEXT_PACKET_SCHEMA_VERSION, build_context_packet
from sia.context.fingerprint import compute_source_context_fingerprint, layout_fingerprint_payload
from sia.integrity.context_isolation import local_context_fields, local_scoped_fields


def _registry_row(job: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    for row in job.get("source_registry") or []:
        if isinstance(row, dict) and str(row.get("source_id") or "") == str(source_id):
            return dict(row)
    return {}


def build_source_context_debug_payload(
    job: Dict[str, Any],
    source_id: str,
    *,
    target_template: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """JSON for ``GET /api/debug/<job_id>/context/<source_id>``."""
    reg = _registry_row(job, str(source_id))
    sheet_name = str(reg.get("sheet_name") or "").strip() or None
    packet = build_context_packet(
        job,
        target_template=target_template,
        selected_sheet=sheet_name,
        selected_source_id=str(source_id),
    )
    ic = packet.get("interpreted_context") if isinstance(packet.get("interpreted_context"), dict) else {}
    scoped = local_scoped_fields(packet)
    scoped_rows: List[Dict[str, Any]] = [
        {**sf.to_dict(), "provenance_summary": sf.provenance_summary()} for sf in scoped.values()
    ]
    fingerprint = compute_source_context_fingerprint(
        job,
        source_id=str(source_id),
        sheet_name=sheet_name,
        source_metadata=reg,
    )
    cache_entry = dict((job.get("context_artifact_cache") or {}).get(str(source_id)) or {})
    return {
        "job_id": str(job.get("id") or ""),
        "source_id": str(source_id),
        "sheet_name": sheet_name,
        "schema_version": packet.get("_schema_version") or CONTEXT_PACKET_SCHEMA_VERSION,
        "context_fingerprint": fingerprint,
        "artifact_cache": {
            "hit": bool(cache_entry) and cache_entry.get("fingerprint") == fingerprint,
            "fingerprint": cache_entry.get("fingerprint"),
            "snippets_ref": cache_entry.get("snippets"),
            "interpreted_ref": cache_entry.get("interpreted"),
        },
        "lineage": packet.get("lineage") or {},
        "local_context": local_context_fields(packet),
        "scoped_fields": scoped_rows,
        "interpreted_context": {
            "fields": dict(ic.get("fields") or {}),
            "scoped_fields": dict(ic.get("scoped_fields") or {}),
            "assumptions": list(ic.get("assumptions") or [])[:12],
            "evidence": list(ic.get("evidence") or [])[:24],
        },
        "context_block_snippets": list(packet.get("context_block_snippets") or [])[:12],
        "layout_fingerprint": layout_fingerprint_payload(
            job, source_id=str(source_id), sheet_name=sheet_name
        ),
    }
