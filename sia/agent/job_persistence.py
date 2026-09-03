"""Durable job + HITL persistence (Phase B/C) backed by SQLite metadata store."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from sia.agent.json_safe import dumps as json_safe_dumps

logger = logging.getLogger(__name__)

_METADATA_DB_ENV = "SCHEMA_AGENT_METADATA_DB"
_DEFAULT_DB_PATH = Path("runtime/metadata.db")

# Job fields persisted in jobs.metadata_json (Phase B).
JOB_BODY_KEYS: Set[str] = {
    "filename",
    "file_path",
    "sheets",
    "data_files",
    "template_path",
    "status",
    "created_at",
    "steps",
    "schema",
    "data_preview",
    "total_rows",
    "trace",
    "requires_review",
    "review_reason",
    "review_status",
    "rejection_reason",
    "output_files",
    "source_registry",
    "source_scope_registry",
    "layout_registry",
    "mapping_registry",
    "business_rules_registry",
    "approved_file_relationships",
    "relationship_proposals",
    "user_notes",
    "current_ux_stage",
    "ux_source_progress",
    "relationships_gate_complete",
    "processing_heartbeat_at",
    "scoped_source",
    "mapping_source_id",
    "mapping_sheet",
    "context_packet",
    "resolved_planner_decisions",
    "demarcation_batch_proposals",
    "layout_complexity_by_source",
    "layout_standardize_by_source",
    "mapping_matrix_draft",
    "context_artifact_cache",
    "overall_confidence",
    "source_traces",
    "cancellation_reason",
    "destructive_approved",
    "approved_tool_indices",
    "error_details",
    "corrections",
    "debug_events",
    "llm_traces",
    "tool_executions",
    # Upload registration + Column shaping (must survive nav / restart)
    "enterprise_info",
    "hierarchy_registry",
    "hierarchy_register_complete",
    "mixed_grain_acknowledged",
    "column_standardize_registry",
    "custom_mapping_metrics",
    "source_context_by_source",
    "ux_stepper_summary",
}

HITL_AUX_KEYS = (
    "review_queue",
    "pending_deletions",
    "pending_demarcations",
    "pending_schema_mappings",
)

_PENDING_STATE_DROP_KEYS = frozenset(
    {
        "grid",
        "dataframe",
        "clean_dataframe",
        "current_df",
        "scoped_dataframe",
        "normalized_dataframe",
        "last_valid_checkpoint",
        "llm_client",
        "hitl_manager",
    }
)


def metadata_db_path() -> Optional[Path]:
    raw = (os.environ.get(_METADATA_DB_ENV) or "").strip()
    if raw:
        return Path(raw)
    return _DEFAULT_DB_PATH if os.environ.get("SCHEMA_AGENT_PERSIST_JOBS", "").lower() in ("1", "true", "yes") else None


def persistence_enabled() -> bool:
    return metadata_db_path() is not None


def open_metadata_store():
    path = metadata_db_path()
    if path is None:
        return None
    try:
        from sia.storage.repositories import MetadataStore

        store = MetadataStore(path)
        return store
    except Exception as exc:
        logger.warning("Job persistence disabled — metadata store failed to open: %s", exc)
        return None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_dataframe(val: Any) -> bool:
    try:
        import pandas as pd

        return isinstance(val, pd.DataFrame)
    except ImportError:  # pragma: no cover
        return False


def _json_ready_pending_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Strip heavy/opaque agent objects before JSON round-trip (no deepcopy)."""
    out: Dict[str, Any] = {}
    for key, val in state.items():
        if key in _PENDING_STATE_DROP_KEYS:
            continue
        if val is None:
            out[key] = None
            continue
        if _is_dataframe(val):
            continue
        if is_dataclass(val):
            out[key] = asdict(val)
            continue
        to_dict = getattr(val, "to_dict", None)
        if callable(to_dict):
            try:
                out[key] = to_dict()
                continue
            except Exception:
                logger.debug("skip pending_state key %s: to_dict failed", key)
                continue
        if isinstance(val, (dict, list, str, int, float, bool)):
            out[key] = val
            continue
        logger.debug("skip pending_state key %s: opaque type %s", key, type(val).__name__)
    return out


def _sanitize_pending_state(state: Any) -> Dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    slim = _json_ready_pending_state(state)
    try:
        import json

        return json.loads(json_safe_dumps(slim))
    except Exception as exc:
        logger.warning("_sanitize_pending_state json round-trip failed: %s", exc)
        return slim


def _checkpoint_to_record(checkpoint_id: str, checkpoint: Dict[str, Any]) -> "HITLCheckpointRecord":
    from sia.storage.repositories import HITLCheckpointRecord

    cp_type = str(checkpoint.get("type") or checkpoint.get("checkpoint_type") or "unknown")
    job_id = str(checkpoint.get("job_id") or "")
    payload = {
        "checkpoint": dict(checkpoint),
        "pending_state": _sanitize_pending_state(checkpoint.get("pending_state")),
    }
    if payload["checkpoint"].get("pending_state") is not None:
        payload["checkpoint"]["pending_state"] = payload["pending_state"]
    ps = checkpoint.get("pending_state") if isinstance(checkpoint.get("pending_state"), dict) else {}
    source_id = (
        ps.get("source_id")
        or (ps.get("scoped_source") or {}).get("source_id")
        or (checkpoint.get("trigger_data") or {}).get("source_id")
    )
    return HITLCheckpointRecord(
        checkpoint_id=str(checkpoint_id),
        job_id=job_id,
        source_id=str(source_id) if source_id else None,
        stage=cp_type,
        status=str(checkpoint.get("status") or "pending"),
        payload=payload,
        created_at=str(checkpoint.get("created_at") or _utcnow()),
        updated_at=_utcnow(),
    )


def _checkpoint_from_record(record: "HITLCheckpointRecord") -> Dict[str, Any]:
    payload = dict(record.payload or {})
    cp = dict(payload.get("checkpoint") or {})
    if not cp:
        cp = dict(payload)
    if payload.get("pending_state") and not cp.get("pending_state"):
        cp["pending_state"] = payload["pending_state"]
    cp.setdefault("checkpoint_id", record.checkpoint_id)
    cp.setdefault("job_id", record.job_id)
    cp.setdefault("type", record.stage)
    cp.setdefault("status", record.status)
    return cp


def extract_job_body(job: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    for key in JOB_BODY_KEYS:
        if key in job:
            body[key] = job[key]
    body["id"] = str(job.get("id") or job.get("job_id") or "")
    return body


def extract_hitl_aux(
    *,
    review_queue: Dict[str, Any],
    pending_deletions: Dict[str, Any],
    pending_demarcations: Dict[str, Any],
    pending_schema_mappings: Dict[str, Any],
    job_id: str,
) -> Dict[str, Any]:
    aux: Dict[str, Any] = {}
    if job_id in review_queue:
        aux["review_queue"] = review_queue[job_id]
    if job_id in pending_deletions:
        aux["pending_deletions"] = pending_deletions[job_id]
    if job_id in pending_demarcations:
        aux["pending_demarcations"] = pending_demarcations[job_id]
    if job_id in pending_schema_mappings:
        aux["pending_schema_mappings"] = pending_schema_mappings[job_id]
    return aux


def job_to_record(
    job: Dict[str, Any],
    *,
    hitl_aux: Optional[Dict[str, Any]] = None,
) -> "JobRecord":
    from sia.storage.repositories import JobRecord

    job_id = str(job.get("id") or job.get("job_id") or "")
    metadata = extract_job_body(job)
    if hitl_aux:
        metadata["_hitl_aux"] = hitl_aux
    return JobRecord(
        job_id=job_id,
        status=str(job.get("status") or "created"),
        template_path=job.get("template_path"),
        template_version=None,
        active_run_version=0,
        created_at=str(job.get("created_at") or _utcnow()),
        updated_at=_utcnow(),
        metadata=metadata,
    )


def record_to_job(record: "JobRecord") -> Dict[str, Any]:
    meta = dict(record.metadata or {})
    hitl_aux = meta.pop("_hitl_aux", None)
    job = dict(meta)
    job["id"] = record.job_id
    job["status"] = record.status
    if record.template_path is not None:
        job["template_path"] = record.template_path
    if isinstance(hitl_aux, dict):
        job["_hitl_aux"] = hitl_aux
    return job


def persist_job_snapshot(
    store,
    job: Dict[str, Any],
    *,
    review_queue: Optional[Dict[str, Any]] = None,
    pending_deletions: Optional[Dict[str, Any]] = None,
    pending_demarcations: Optional[Dict[str, Any]] = None,
    pending_schema_mappings: Optional[Dict[str, Any]] = None,
) -> None:
    job_id = str(job.get("id") or job.get("job_id") or "")
    if not job_id:
        return
    aux = extract_hitl_aux(
        review_queue=review_queue or {},
        pending_deletions=pending_deletions or {},
        pending_demarcations=pending_demarcations or {},
        pending_schema_mappings=pending_schema_mappings or {},
        job_id=job_id,
    )
    try:
        store.upsert_job(job_to_record(job, hitl_aux=aux or None))
    except Exception as exc:
        logger.warning("persist job %s failed: %s", job_id, exc)


def persist_checkpoint(store, checkpoint_id: str, checkpoint: Dict[str, Any]) -> None:
    try:
        record = _checkpoint_to_record(checkpoint_id, checkpoint)
        store.upsert_hitl_checkpoint(record)
    except Exception as exc:
        logger.warning("persist checkpoint %s failed: %s", checkpoint_id, exc)


def delete_checkpoint(store, checkpoint_id: str) -> None:
    try:
        store.delete_hitl_checkpoint(checkpoint_id)
    except Exception as exc:
        logger.warning("delete checkpoint %s failed: %s", checkpoint_id, exc)


def delete_job_from_store(store, job_id: str) -> None:
    try:
        store.delete_job(job_id)
    except Exception as exc:
        logger.warning("delete job %s from store failed: %s", job_id, exc)


def load_all_jobs(store) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for record in store.list_jobs():
        out.append(record_to_job(record))
    return out


def load_pending_checkpoints(store) -> Dict[str, Dict[str, Any]]:
    pending: Dict[str, Dict[str, Any]] = {}
    try:
        for rec in store.list_pending_hitl_checkpoints():
            cp = _checkpoint_from_record(rec)
            pending[str(rec.checkpoint_id)] = cp
    except Exception as exc:
        logger.warning("load pending checkpoints failed: %s", exc)
    return pending


def apply_hitl_aux_to_manager(manager, job: Dict[str, Any]) -> None:
    aux = job.pop("_hitl_aux", None)
    if not isinstance(aux, dict):
        return
    job_id = str(job.get("id") or "")
    if not job_id:
        return
    rq = aux.get("review_queue")
    if isinstance(rq, dict):
        manager.review_queue[job_id] = rq
    pd = aux.get("pending_deletions")
    if isinstance(pd, dict):
        manager.pending_deletions[job_id] = pd
        if isinstance(pd.get("pending_state"), dict):
            pd["pending_state"] = _sanitize_pending_state(pd["pending_state"])
    dem = aux.get("pending_demarcations")
    if isinstance(dem, dict):
        manager.pending_demarcations[job_id] = dem
    psm = aux.get("pending_schema_mappings")
    if isinstance(psm, dict):
        manager.pending_schema_mappings[job_id] = psm
