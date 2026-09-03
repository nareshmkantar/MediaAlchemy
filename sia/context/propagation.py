"""Scoped field propagation with hop counting and boundary enforcement."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional

from sia.context.boundary_policy import (
    RELATIONSHIP_ENRICHABLE_FIELDS,
    BoundaryDecision,
    BoundaryVerdict,
    ContextBoundaryPolicy,
)
from sia.context.confidence import (
    CONFIDENCE_CROSS_SHEET,
    CONFIDENCE_CROSS_SHEET_APPROVED,
    MAX_CONTEXT_HOPS,
)
from sia.context.scoped_field import ScopedField, merge_scoped_field
from sia.integrity.context_isolation import SOURCE_LOCAL_DIMENSION_COLUMNS

logger = logging.getLogger(__name__)

_CROSS_SHEET_BLOCKED_FIELDS = frozenset(SOURCE_LOCAL_DIMENSION_COLUMNS)


@dataclass
class PropagationResult:
    accepted: bool
    field: Optional[ScopedField] = None
    reason: str = ""
    decision: Optional[BoundaryDecision] = None
    log_entry: Dict[str, Any] = dc_field(default_factory=dict)


def _hop_for_candidate(candidate: ScopedField, *, cross_sheet: bool) -> int:
    """0 = block/sheet-local; 1+ = cross-sheet travel."""
    if cross_sheet:
        return max(int(candidate.hop or 0), 0) + 1
    if candidate.scope == "sheet":
        return max(int(candidate.hop or 0), 1)
    return max(int(candidate.hop or 0), 0)


def _confidence_for_candidate(candidate: ScopedField, *, relationship: bool) -> float:
    if candidate.scope == "cross_sheet":
        return CONFIDENCE_CROSS_SHEET_APPROVED if relationship else CONFIDENCE_CROSS_SHEET
    return float(candidate.confidence)


def _log_propagation_event(job: Optional[Dict[str, Any]], entry: Dict[str, Any]) -> None:
    if not isinstance(job, dict) or not entry:
        return
    try:
        from sia.context.diff_log import append_context_diff

        append_context_diff(
            job,
            {
                "stage": str(entry.get("stage") or "propagate"),
                "source_id": str(entry.get("source_id") or ""),
                "field": str(entry.get("field") or ""),
                "before": entry.get("value") if entry.get("rejected") else None,
                "after": None if entry.get("rejected") else entry.get("value"),
                "reason": str(entry.get("reason") or ""),
                "hop": int(entry.get("hop") or 0),
                "meta": {
                    "rejected": bool(entry.get("rejected")),
                    "from_scope": entry.get("from_scope"),
                    "to_scope": entry.get("to_scope"),
                },
            },
        )
    except Exception:
        pass


def propagate_field(
    target_fields: Dict[str, ScopedField],
    candidate: ScopedField,
    *,
    policy: Optional[ContextBoundaryPolicy] = None,
    target_scope: str = "interpreted",
    active_source_id: str = "",
    relationship: bool = False,
    stage: str = "propagate",
    job: Optional[Dict[str, Any]] = None,
) -> PropagationResult:
    """
    Merge ``candidate`` into ``target_fields`` when boundary policy allows.

    Increments hop for cross-sheet candidates. Rejects when hop exceeds policy limit.
    """
    pol = policy or ContextBoundaryPolicy(max_cross_sheet_hops=MAX_CONTEXT_HOPS)
    if not candidate.name or not candidate.value:
        return PropagationResult(False, reason="empty field")

    if active_source_id and candidate.source_id and candidate.source_id != active_source_id:
        if candidate.scope != "cross_sheet" or not relationship:
            entry = {
                "stage": stage,
                "field": candidate.name,
                "source_id": active_source_id,
                "rejected": True,
                "reason": "foreign_source_id",
                "hop": int(candidate.hop or 0),
            }
            _log_propagation_event(job, entry)
            return PropagationResult(
                False,
                reason=f"field source_id {candidate.source_id!r} != active {active_source_id!r}",
                log_entry=entry,
            )

    cross_sheet = candidate.scope == "cross_sheet" or target_scope == "cross_sheet"
    if cross_sheet and candidate.name in _CROSS_SHEET_BLOCKED_FIELDS:
        entry = {
            "stage": stage,
            "field": candidate.name,
            "value": candidate.value,
            "source_id": active_source_id or candidate.source_id,
            "from_scope": candidate.scope,
            "to_scope": target_scope,
            "hop": _hop_for_candidate(candidate, cross_sheet=True),
            "rejected": True,
            "reason": "cross-sheet dimension literal blocked (relationship enrichment)",
        }
        _log_propagation_event(job, entry)
        return PropagationResult(
            False,
            reason=entry["reason"],
            log_entry=entry,
        )

    if cross_sheet and relationship and candidate.name not in RELATIONSHIP_ENRICHABLE_FIELDS:
        if candidate.name in _CROSS_SHEET_BLOCKED_FIELDS:
            pass  # already handled
        else:
            entry = {
                "stage": stage,
                "field": candidate.name,
                "value": candidate.value,
                "source_id": active_source_id or candidate.source_id,
                "from_scope": candidate.scope,
                "to_scope": target_scope,
                "hop": _hop_for_candidate(candidate, cross_sheet=True),
                "rejected": True,
                "reason": f"field {candidate.name!r} not in relationship enrichable allowlist",
            }
            _log_propagation_event(job, entry)
            return PropagationResult(
                False,
                reason=entry["reason"],
                log_entry=entry,
            )

    decision = pol.may_propagate(
        candidate.scope,
        target_scope,
        relationship=relationship or cross_sheet,
        evidence=bool(candidate.evidence_line),
    )
    next_hop = _hop_for_candidate(candidate, cross_sheet=cross_sheet)
    hop_decision = pol.allows_hop(next_hop, cross_sheet=cross_sheet)
    if hop_decision.verdict == BoundaryVerdict.REJECT:
        decision = hop_decision

    if decision.verdict == BoundaryVerdict.REJECT:
        logger.info(
            "context propagation rejected: %s=%r (%s)",
            candidate.name,
            candidate.value,
            decision.reason,
        )
        entry = {
            "stage": stage,
            "field": candidate.name,
            "value": candidate.value,
            "source_id": active_source_id or candidate.source_id,
            "from_scope": candidate.scope,
            "to_scope": target_scope,
            "hop": next_hop,
            "rejected": True,
            "reason": decision.reason,
        }
        _log_propagation_event(job, entry)
        return PropagationResult(
            False,
            reason=decision.reason,
            decision=decision,
            log_entry=entry,
        )

    propagated = ScopedField(
        name=candidate.name,
        value=candidate.value,
        scope=target_scope if cross_sheet else candidate.scope,
        source_id=candidate.source_id,
        sheet_name=candidate.sheet_name,
        block_id=candidate.block_id,
        block_label=candidate.block_label,
        evidence_line=candidate.evidence_line,
        confidence=_confidence_for_candidate(candidate, relationship=relationship),
        hop=next_hop,
    )
    merge_scoped_field(target_fields, propagated)
    entry = {
        "stage": stage,
        "field": propagated.name,
        "value": propagated.value,
        "source_id": active_source_id or candidate.source_id,
        "from_scope": candidate.scope,
        "to_scope": propagated.scope,
        "hop": propagated.hop,
        "rejected": False,
        "reason": decision.reason,
    }
    _log_propagation_event(job, entry)
    return PropagationResult(
        True,
        field=propagated,
        reason=decision.reason,
        decision=decision,
        log_entry=entry,
    )


def propagate_fields(
    target_fields: Dict[str, ScopedField],
    candidates: List[ScopedField],
    **kwargs: Any,
) -> List[PropagationResult]:
    return [propagate_field(target_fields, c, **kwargs) for c in candidates]
