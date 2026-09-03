"""Synthetic debug events and state snapshots for HITL pause/resume (Agent Trace tab)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sia.debug.state_snapshot import build_state_snapshot

PAUSE_TYPE_TRIGGER_NODE: Dict[str, str] = {
    "plan_review": "generate_plan",
    "file_relationship_review": "resolve_mapping",
    "destructive_approval": "execute_tools",
    "integrity_review": "execute_tools",
    "verification_stall": "verify_output",
    "structural_review": "analyze_structure",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _find_parent_event_id(job: Dict[str, Any], trigger_module: str) -> Optional[str]:
    events = [e for e in (job.get("debug_events") or []) if isinstance(e, dict)]
    candidates: List[Tuple[datetime, str]] = []
    for ev in events:
        if ev.get("process_type") != "node":
            continue
        module = str(ev.get("module") or "")
        if module == trigger_module or trigger_module in module:
            ts = _parse_iso_datetime(ev.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
            eid = ev.get("event_id")
            if eid:
                candidates.append((ts, str(eid)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def _compact_checkpoints(checkpoints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for cp in checkpoints:
        if not isinstance(cp, dict):
            continue
        out.append(
            {
                "checkpoint_id": cp.get("checkpoint_id"),
                "checkpoint_type": cp.get("checkpoint_type"),
                "title": cp.get("title"),
                "severity": cp.get("severity"),
                "resolved": bool(cp.get("resolved")),
            }
        )
    return out


def _pause_event_id(pause_type: str, checkpoints: List[Dict[str, Any]]) -> str:
    for cp in checkpoints:
        cid = cp.get("checkpoint_id")
        if cid:
            return f"hitl-pause-{cid}"
    return f"hitl-pause-{pause_type}-{uuid.uuid4().hex[:8]}"


def _resume_event_id(checkpoint_id: str, action: str) -> str:
    return f"hitl-resume-{checkpoint_id}-{action}"


def _append_snapshot(
    job: Dict[str, Any],
    *,
    label: str,
    phase: str,
    anchor_event_id: str,
    state: Optional[Dict[str, Any]] = None,
    context_packet: Optional[Dict[str, Any]] = None,
    decision: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
) -> None:
    st = state or {}
    cp = context_packet if context_packet is not None else st.get("context_packet")
    snapshot = build_state_snapshot(
        label=label,
        phase=phase,
        state=st,
        context_packet=cp,
        job=job,
        decision=decision or {},
        anchor_event_id=anchor_event_id,
    )
    event_id = f"hitl-state-{uuid.uuid4().hex[:8]}"
    snapshot["event_id"] = event_id
    snapshot["snapshot_id"] = f"state-{event_id}"
    snapshot["timestamp"] = _utc_now_iso()
    snapshot["anchor_event_id"] = anchor_event_id
    if source_id:
        snapshot["source_id"] = source_id
    if sheet_name is not None:
        snapshot["sheet_name"] = sheet_name
    existing = [s for s in (job.get("state_snapshots") or []) if isinstance(s, dict)]
    seen = {s.get("snapshot_id") for s in existing if s.get("snapshot_id")}
    if snapshot.get("snapshot_id") in seen:
        return
    existing.append(snapshot)
    job["state_snapshots"] = existing


def _annotate_row(row: Dict[str, Any], *, source_id: Optional[str], sheet_name: Optional[str], processing_sheet: Optional[str]) -> None:
    if source_id:
        row["source_id"] = source_id
    if sheet_name is not None:
        row["sheet_name"] = sheet_name
    if processing_sheet is not None:
        row["processing_sheet"] = processing_sheet


def _extract_pause_context(trace: Any, pending_state: Optional[Dict[str, Any]]) -> Tuple[str, str, List[Dict], List[Dict], List[Dict], Dict[str, Any]]:
    ps = pending_state if isinstance(pending_state, dict) else {}
    if not ps and trace is not None:
        ps = getattr(trace, "pending_state", None) or {}
    if not isinstance(ps, dict):
        ps = {}

    pause_type = (
        ps.get("hitl_pause_type")
        or (getattr(trace, "hitl_pause_type", None) if trace is not None else None)
        or "plan_review"
    )
    pause_reason = (
        ps.get("escalation_reason")
        or ps.get("review_reason")
        or (getattr(trace, "review_reason", None) if trace is not None else None)
        or "Paused for human review"
    )
    checkpoints = list(ps.get("hitl_checkpoints") or [])
    if not checkpoints and trace is not None:
        checkpoints = list(getattr(trace, "hitl_checkpoints", []) or [])
    deletion_previews = list(ps.get("deletion_previews") or [])
    if not deletion_previews and trace is not None:
        deletion_previews = list(getattr(trace, "deletion_previews", []) or [])
    low_confidence = list(ps.get("low_confidence_items") or [])
    if not low_confidence and trace is not None:
        low_confidence = list(getattr(trace, "low_confidence_items", []) or [])
    if deletion_previews and pause_type == "plan_review":
        pause_type = "destructive_approval"
    return str(pause_type), str(pause_reason), checkpoints, deletion_previews, low_confidence, ps


def record_hitl_pause_debug(
    job: Dict[str, Any],
    trace: Any = None,
    *,
    pending_state: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
    processing_sheet: Optional[str] = None,
) -> Optional[str]:
    """Append a synthetic ``hitl_pause`` row under the triggering graph node in Agent Trace."""
    pause_type, pause_reason, checkpoints, deletion_previews, low_confidence, ps = _extract_pause_context(
        trace, pending_state
    )
    compact_cps = _compact_checkpoints(checkpoints)
    event_id = _pause_event_id(pause_type, checkpoints)

    existing_events = [e for e in (job.get("debug_events") or []) if isinstance(e, dict)]
    for ev in existing_events:
        if ev.get("event_id") == event_id:
            return event_id

    trigger_module = PAUSE_TYPE_TRIGGER_NODE.get(pause_type, pause_type)
    parent_id = _find_parent_event_id(job, trigger_module)
    ts = _utc_now_iso()

    metadata: Dict[str, Any] = {
        "pause_type": pause_type,
        "trigger_node": trigger_module,
        "checkpoint_count": len(compact_cps),
        "checkpoints": compact_cps,
        "deletion_preview_count": len(deletion_previews),
        "low_confidence_count": len(low_confidence),
        "review_url": "/review.html",
    }
    if deletion_previews:
        metadata["deletion_previews"] = [
            {
                "tool_name": p.get("tool_name") if isinstance(p, dict) else getattr(p, "tool_name", None),
                "impact_summary": (p.get("impact_summary") if isinstance(p, dict) else getattr(p, "impact_summary", ""))[:200],
            }
            for p in deletion_previews[:12]
        ]

    ev: Dict[str, Any] = {
        "event_id": event_id,
        "parent_event_id": parent_id,
        "process_type": "hitl_pause",
        "module": pause_type,
        "operation": "HITL",
        "timestamp": ts,
        "duration_ms": 0.0,
        "status": "pending",
        "input_summary": pause_reason[:240] if pause_reason else "Human review required",
        "message": pause_reason,
        "result": "",
        "metadata": metadata,
    }
    _annotate_row(ev, source_id=source_id, sheet_name=sheet_name, processing_sheet=processing_sheet)
    existing_events.append(ev)
    job["debug_events"] = existing_events

    decision = {
        "pause_type": pause_type,
        "pause_reason": pause_reason,
        "checkpoints": compact_cps,
        "deletion_preview_count": len(deletion_previews),
        "low_confidence_count": len(low_confidence),
    }
    if deletion_previews:
        decision["deletion_previews"] = metadata.get("deletion_previews", [])
    _append_snapshot(
        job,
        label=f"state.hitl.{pause_type}",
        phase="hitl_pause",
        anchor_event_id=event_id,
        state=ps,
        context_packet=ps.get("context_packet"),
        decision=decision,
        source_id=source_id or ps.get("source_id"),
        sheet_name=sheet_name if sheet_name is not None else ps.get("sheet_name"),
    )
    return event_id


def record_hitl_resume_debug(
    job: Dict[str, Any],
    *,
    checkpoint_id: str,
    pause_type: Optional[str],
    action: str,
    resolution_data: Optional[Dict[str, Any]] = None,
    pending_state: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
    processing_sheet: Optional[str] = None,
) -> Optional[str]:
    """Record resume after a checkpoint is resolved (child of the pause event when possible)."""
    pause_type = pause_type or "plan_review"
    parent_id = f"hitl-pause-{checkpoint_id}"
    existing_events = [e for e in (job.get("debug_events") or []) if isinstance(e, dict)]
    if not any(e.get("event_id") == parent_id for e in existing_events):
        parent_id = _find_parent_event_id(job, PAUSE_TYPE_TRIGGER_NODE.get(pause_type, pause_type))

    event_id = _resume_event_id(checkpoint_id, action)
    for ev in existing_events:
        if ev.get("event_id") == event_id:
            return event_id

    ts = _utc_now_iso()
    resolution_data = resolution_data or {}
    ev: Dict[str, Any] = {
        "event_id": event_id,
        "parent_event_id": parent_id,
        "process_type": "hitl_resume",
        "module": pause_type,
        "operation": "RESUME",
        "timestamp": ts,
        "duration_ms": 0.0,
        "status": "success" if action in {"approve", "modify", "regenerate", "select_header_row"} else action,
        "input_summary": f"{action} checkpoint {checkpoint_id}",
        "message": f"Checkpoint resolved: {action}",
        "result": "",
        "metadata": {
            "checkpoint_id": checkpoint_id,
            "action": action,
            "pause_type": pause_type,
            "resolution_keys": list(resolution_data.keys())[:20],
        },
    }
    _annotate_row(ev, source_id=source_id, sheet_name=sheet_name, processing_sheet=processing_sheet)
    existing_events.append(ev)
    job["debug_events"] = existing_events

    ps = pending_state if isinstance(pending_state, dict) else {}
    _append_snapshot(
        job,
        label=f"state.hitl.{pause_type}.resume",
        phase="hitl_resume",
        anchor_event_id=event_id,
        state=ps,
        context_packet=ps.get("context_packet"),
        decision={
            "checkpoint_id": checkpoint_id,
            "action": action,
            "resolution_data": resolution_data,
        },
        source_id=source_id or ps.get("source_id"),
        sheet_name=sheet_name if sheet_name is not None else ps.get("sheet_name"),
    )
    return event_id
