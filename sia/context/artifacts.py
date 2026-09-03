"""Persist and load per-source context artifacts (snippets + interpreted context)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from sia.context.fingerprint import compute_source_context_fingerprint
from sia.storage.artifacts import ArtifactKind, ArtifactRef, ArtifactStore

logger = logging.getLogger(__name__)

_ARTIFACT_ROOT_ENV = "SCHEMA_AGENT_ARTIFACT_ROOT"
_DEFAULT_ARTIFACT_ROOT = Path("runtime/artifacts")


def artifact_store_root() -> Path:
    return Path(os.environ.get(_ARTIFACT_ROOT_ENV, str(_DEFAULT_ARTIFACT_ROOT)))


def get_artifact_store(root: Optional[Path] = None) -> ArtifactStore:
    return ArtifactStore(root or artifact_store_root())


def _cache_bucket(job: Dict[str, Any]) -> Dict[str, Any]:
    cache = job.get("context_artifact_cache")
    if not isinstance(cache, dict):
        cache = {}
        job["context_artifact_cache"] = cache
    return cache


def try_load_cached_context_artifacts(
    job: Dict[str, Any],
    *,
    source_id: str,
    fingerprint: str,
    store: Optional[ArtifactStore] = None,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """Return cached snippets + interpreted_context when fingerprint matches."""
    entry = _cache_bucket(job).get(str(source_id))
    if not isinstance(entry, dict) or str(entry.get("fingerprint") or "") != str(fingerprint):
        return None, None
    artifact_store = store or get_artifact_store()
    try:
        snippets_ref = ArtifactRef(**dict(entry.get("snippets") or {}))
        interpreted_ref = ArtifactRef(**dict(entry.get("interpreted") or {}))
        snippets = artifact_store.load(snippets_ref)
        interpreted = artifact_store.load(interpreted_ref)
        if not isinstance(snippets, list) or not isinstance(interpreted, dict):
            return None, None
        return snippets, interpreted
    except (FileNotFoundError, TypeError, KeyError) as exc:
        logger.debug("context artifact cache miss for %s: %s", source_id, exc)
        return None, None


def persist_context_artifacts(
    job: Dict[str, Any],
    *,
    source_id: str,
    fingerprint: str,
    snippets: List[Dict[str, Any]],
    interpreted_context: Dict[str, Any],
    store: Optional[ArtifactStore] = None,
) -> Dict[str, Any]:
    """Write snippets and interpreted context; record refs on the job."""
    artifact_store = store or get_artifact_store()
    job_id = str(job.get("id") or job.get("job_id") or "unknown")
    sid = str(source_id)
    snippets_ref = artifact_store.save(
        job_id=job_id,
        source_id=sid,
        kind=ArtifactKind.CONTEXT_BLOCK_SNIPPETS,
        payload=list(snippets or []),
        fingerprint=fingerprint,
    )
    interpreted_ref = artifact_store.save(
        job_id=job_id,
        source_id=sid,
        kind=ArtifactKind.INTERPRETED_CONTEXT,
        payload=dict(interpreted_context or {}),
        fingerprint=fingerprint,
    )
    entry = {
        "fingerprint": fingerprint,
        "snippets": snippets_ref.to_dict(),
        "interpreted": interpreted_ref.to_dict(),
    }
    _cache_bucket(job)[sid] = entry
    _record_context_artifact_metadata(job, snippets_ref, interpreted_ref)
    return entry


def _metadata_store_for_job(job: Dict[str, Any]):
    store = job.get("_metadata_store")
    if store is not None:
        return store
    path = os.environ.get("SCHEMA_AGENT_METADATA_DB")
    if not path:
        return None
    try:
        from sia.storage.repositories import ArtifactRecord, MetadataStore

        ms = MetadataStore(path)
        job["_metadata_store"] = ms
        return ms
    except Exception as exc:
        logger.debug("metadata store unavailable: %s", exc)
        return None


def _record_context_artifact_metadata(
    job: Dict[str, Any],
    snippets_ref: ArtifactRef,
    interpreted_ref: ArtifactRef,
) -> None:
    """Mirror context artifact refs into SQLite ``artifacts`` table when configured."""
    ms = _metadata_store_for_job(job)
    if ms is None:
        return
    try:
        from sia.storage.repositories import ArtifactRecord

        for ref in (snippets_ref, interpreted_ref):
            ms.record_artifact(
                ArtifactRecord(
                    artifact_id=f"{ref.job_id}:{ref.source_id}:{ref.kind.value}:{ref.fingerprint}",
                    job_id=ref.job_id,
                    source_id=ref.source_id,
                    kind=ref.kind.value if hasattr(ref.kind, "value") else str(ref.kind),
                    version=ref.version,
                    path=ref.path,
                    format=ref.format,
                    fingerprint=ref.fingerprint,
                    size_bytes=ref.size_bytes,
                )
            )
    except Exception as exc:
        logger.debug("record context artifact metadata failed: %s", exc)


def restore_context_artifact_cache_from_store(job: Dict[str, Any]) -> None:
    """Reload ``context_artifact_cache`` refs from metadata DB after job restart."""
    ms = _metadata_store_for_job(job)
    if ms is None:
        return
    job_id = str(job.get("id") or job.get("job_id") or "")
    if not job_id:
        return
    try:
        from sia.storage.artifacts import ArtifactKind

        bucket = _cache_bucket(job)
        for sid_row in job.get("source_registry") or []:
            if not isinstance(sid_row, dict):
                continue
            sid = str(sid_row.get("source_id") or "").strip()
            if not sid:
                continue
            snippets_rec = ms.find_latest_artifact(
                job_id=job_id,
                source_id=sid,
                kind=ArtifactKind.CONTEXT_BLOCK_SNIPPETS.value,
            )
            interpreted_rec = ms.find_latest_artifact(
                job_id=job_id,
                source_id=sid,
                kind=ArtifactKind.INTERPRETED_CONTEXT.value,
            )
            if snippets_rec is None or interpreted_rec is None:
                continue
            bucket[sid] = {
                "fingerprint": snippets_rec.fingerprint,
                "snippets": {
                    "job_id": job_id,
                    "source_id": sid,
                    "kind": snippets_rec.kind,
                    "version": snippets_rec.version,
                    "fingerprint": snippets_rec.fingerprint,
                    "path": snippets_rec.path,
                    "format": snippets_rec.format,
                    "size_bytes": snippets_rec.size_bytes,
                },
                "interpreted": {
                    "job_id": job_id,
                    "source_id": sid,
                    "kind": interpreted_rec.kind,
                    "version": interpreted_rec.version,
                    "fingerprint": interpreted_rec.fingerprint,
                    "path": interpreted_rec.path,
                    "format": interpreted_rec.format,
                    "size_bytes": interpreted_rec.size_bytes,
                },
            }
    except Exception as exc:
        logger.debug("restore context artifact cache failed: %s", exc)


def persist_finalize_output_frame(
    job: Dict[str, Any],
    *,
    source_id: str,
    frame: Any,
    context_fingerprint: str = "",
) -> Optional[str]:
    """Save post-finalize normalized frame with context fingerprint in metadata."""
    import pandas as pd

    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    artifact_store = get_artifact_store()
    job_id = str(job.get("id") or job.get("job_id") or "unknown")
    sid = str(source_id)
    fp = str(context_fingerprint or compute_source_context_fingerprint(job, source_id=sid) or "frame")
    ref = artifact_store.save(
        job_id=job_id,
        source_id=sid,
        kind=ArtifactKind.NORMALIZED_DATAFRAME,
        payload=frame,
        fingerprint=fp,
    )
    refs = job.get("normalized_frame_artifacts")
    if not isinstance(refs, dict):
        refs = {}
        job["normalized_frame_artifacts"] = refs
    refs[sid] = {**ref.to_dict(), "context_fingerprint": fp}
    ms = _metadata_store_for_job(job)
    if ms is not None:
        try:
            from sia.storage.repositories import ArtifactRecord

            ms.record_artifact(
                ArtifactRecord(
                    artifact_id=f"{job_id}:{sid}:{ArtifactKind.NORMALIZED_DATAFRAME.value}:{ref.version}",
                    job_id=job_id,
                    source_id=sid,
                    kind=ArtifactKind.NORMALIZED_DATAFRAME.value,
                    version=ref.version,
                    path=ref.path,
                    format=ref.format,
                    fingerprint=fp,
                    size_bytes=ref.size_bytes,
                )
            )
        except Exception as exc:
            logger.debug("record normalized frame metadata failed: %s", exc)
    return ref.path


def resolve_source_context_material(
    job: Dict[str, Any],
    *,
    source_id: str,
    sheet_name: Optional[str],
    source_metadata: Dict[str, Any],
    approved_layout: Dict[str, Any],
    build_fresh: Callable[[], Tuple[List[Dict[str, Any]], Dict[str, Any]]],
    store: Optional[ArtifactStore] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str, bool]:
    """
    Load cached context material or build fresh, persist, and return.

    Returns ``(snippets, interpreted_context, fingerprint, from_cache)``.
    """
    fingerprint = compute_source_context_fingerprint(
        job,
        source_id=source_id,
        sheet_name=sheet_name,
        source_metadata=source_metadata,
    )
    cached_snippets, cached_interpreted = try_load_cached_context_artifacts(
        job,
        source_id=source_id,
        fingerprint=fingerprint,
        store=store,
    )
    if cached_snippets is not None and cached_interpreted is not None:
        return cached_snippets, cached_interpreted, fingerprint, True

    snippets, interpreted_context = build_fresh()
    persist_context_artifacts(
        job,
        source_id=source_id,
        fingerprint=fingerprint,
        snippets=snippets,
        interpreted_context=interpreted_context,
        store=store,
    )
    return snippets, interpreted_context, fingerprint, False
