"""Stamp-time confidence and ambiguity checks (Phase 7.3)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sia.context.confidence import MIN_CONFIDENCE_TO_STAMP, MIN_CONFIDENCE_TAB_TO_STAMP
from sia.integrity.context_isolation import (
    SOURCE_LOCAL_DIMENSION_COLUMNS,
    local_scoped_fields,
)


def multisheet_job(job: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(job, dict):
        return False
    registry = job.get("source_registry") or []
    sheets = {
        str(r.get("sheet_name") or "").strip()
        for r in registry
        if isinstance(r, dict) and str(r.get("sheet_name") or "").strip()
    }
    if len(sheets) > 1:
        return True
    data_files = job.get("data_files") or []
    if len(data_files) > 1:
        return True
    return len(registry) > 1


def low_confidence_stamp_fields(
    context_packet: Optional[Dict[str, Any]],
    *,
    min_confidence: float = MIN_CONFIDENCE_TO_STAMP,
) -> List[Dict[str, Any]]:
    """Fields that would be stamped but are below the confidence threshold."""
    scoped = local_scoped_fields(context_packet)
    out: List[Dict[str, Any]] = []
    for col in SOURCE_LOCAL_DIMENSION_COLUMNS:
        sf = scoped.get(col)
        if sf is None or not str(sf.value or "").strip():
            continue
        if float(sf.confidence) < float(min_confidence):
            out.append(
                {
                    "field": col,
                    "value": sf.value,
                    "confidence": sf.confidence,
                    "scope": sf.scope,
                    "hop": sf.hop,
                    "evidence_line": sf.evidence_line,
                }
            )
    return out


def evaluate_low_confidence_context(
    context_packet: Optional[Dict[str, Any]],
    job: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Advisory violations when stamp would rely on tab-only / low-confidence fields
    on a multisheet job (Phase 7.4).
    """
    if not multisheet_job(job):
        return []
    scoped = local_scoped_fields(context_packet)
    violations: List[Dict[str, Any]] = []
    for col in SOURCE_LOCAL_DIMENSION_COLUMNS:
        sf = scoped.get(col)
        if sf is None or not str(sf.value or "").strip():
            continue
        scope = str(sf.scope or "")
        row = {
            "field": col,
            "value": sf.value,
            "confidence": sf.confidence,
            "scope": scope,
            "hop": sf.hop,
            "evidence_line": sf.evidence_line,
        }
        if scope == "sheet":
            violations.append(
                {
                    "type": "low_confidence_context",
                    "severity": "advisory",
                    "message": (
                        f"{col}={sf.value!r} comes from tab inference only "
                        f"(confidence {sf.confidence:.2f}) on a multisheet job"
                    ),
                    "evidence": row,
                }
            )
        elif float(sf.confidence) < MIN_CONFIDENCE_TO_STAMP:
            violations.append(
                {
                    "type": "low_confidence_context",
                    "severity": "advisory",
                    "message": (
                        f"{col}={sf.value!r} below stamp threshold "
                        f"(confidence {sf.confidence:.2f}, scope {scope})"
                    ),
                    "evidence": row,
                }
            )
    return violations


def stampable_local_fields(
    context_packet: Optional[Dict[str, Any]],
    *,
    min_confidence: float = MIN_CONFIDENCE_TO_STAMP,
) -> Dict[str, str]:
    """Flat dimension literals safe to stamp at finalize."""
    scoped = local_scoped_fields(context_packet)
    out: Dict[str, str] = {}
    for col in SOURCE_LOCAL_DIMENSION_COLUMNS:
        sf = scoped.get(col)
        if sf is None or not str(sf.value or "").strip():
            continue
        scope = str(sf.scope or "")
        threshold = (
            MIN_CONFIDENCE_TAB_TO_STAMP if scope == "sheet" else float(min_confidence)
        )
        if float(sf.confidence) >= threshold:
            out[col] = str(sf.value).strip()
    return out


def context_ambiguity_requires_hitl(
    context_packet: Optional[Dict[str, Any]],
    job: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when any stamp dimension is below threshold on a multisheet job."""
    if not multisheet_job(job):
        return False
    return bool(low_confidence_stamp_fields(context_packet))
