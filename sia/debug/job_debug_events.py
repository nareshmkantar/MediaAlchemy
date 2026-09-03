"""Job-level debug timeline events for orchestration (upload, demarcation, mapping, processing, collation)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


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


def _timeline_base_datetime(job: Dict[str, Any], live_events: List[Dict[str, Any]]) -> datetime:
    created_at = _parse_iso_datetime(job.get("created_at"))
    if created_at:
        return created_at
    live_times = [
        parsed
        for parsed in (_parse_iso_datetime(e.get("timestamp")) for e in live_events if isinstance(e, dict))
        if parsed is not None
    ]
    return min(live_times) if live_times else datetime.now(timezone.utc)


def _phase_summary_timestamp(
    live_events: List[Dict[str, Any]],
    phase: str,
    fallback: datetime,
) -> str:
    phase_times = [
        parsed
        for parsed in (
            _parse_iso_datetime(e.get("timestamp"))
            for e in live_events
            if isinstance(e, dict) and str(e.get("phase") or "") == phase
        )
        if parsed is not None
    ]
    anchor = max(phase_times) + timedelta(milliseconds=1) if phase_times else fallback
    return anchor.isoformat()


def make_job_debug_event(
    *,
    label: str,
    phase: str,
    parent_event_id: Optional[str] = None,
    source_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
    module: str = "job",
    operation: str = "orchestration",
    status: str = "info",
    summary: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a normalized orchestration debug event dict."""
    event_id = f"job-{uuid.uuid4().hex[:10]}"
    ev: Dict[str, Any] = {
        "event_id": event_id,
        "label": label,
        "parent_event_id": parent_event_id,
        "process_type": "job_debug",
        "module": module,
        "operation": operation,
        "phase": phase,
        "timestamp": timestamp or _utc_now_iso(),
        "status": status,
        "source_id": source_id,
        "sheet_name": sheet_name,
        "input_summary": summary[:500] if summary else "",
        "output_summary": summary[:500] if summary else "",
        "metadata": metadata or {},
    }
    return ev


def append_job_debug_event(
    job: Dict[str, Any],
    event: Dict[str, Any],
) -> None:
    """Append one orchestration event to job['job_debug_events']."""
    events = list(job.get("job_debug_events") or [])
    events.append(event)
    job["job_debug_events"] = events
    job_id = str(job.get("id") or "").strip()
    if job_id:
        try:
            from sia.debug.processing_log import append_debug_event

            append_debug_event(job_id, event)
        except Exception:
            pass


_COLLAPSE_SAVE_LABELS = frozenset({"Demarcation saved", "Schema mapping saved"})
_DEDUPE_CONTEXT_LABEL = "Context packet built"


def _short_source_label(source_id: Optional[str], sheet_name: Optional[str]) -> str:
    if sheet_name and str(sheet_name).strip():
        return str(sheet_name).strip()
    if not source_id:
        return ""
    text = str(source_id).strip()
    if ":" in text:
        return text.split(":")[-1]
    return text[-28:] if len(text) > 28 else text


def _display_label(event: Dict[str, Any]) -> str:
    label = str(event.get("label") or event.get("module") or event.get("phase") or "step")
    suffix = _short_source_label(event.get("source_id"), event.get("sheet_name"))
    if suffix and suffix not in label:
        return f"{label} · {suffix}"
    return label


def _live_has_label(live: List[Dict[str, Any]], label: str) -> bool:
    return any(str(e.get("label") or "") == label for e in live if isinstance(e, dict))


def _live_has_phase_submit(live: List[Dict[str, Any]], phase: str) -> bool:
    return any(
        isinstance(e, dict)
        and str(e.get("phase") or "") == phase
        and str(e.get("operation") or "") == "submit"
        for e in live
    )


def _is_unresolved_mapping_row(row: Dict[str, Any]) -> bool:
    tgt = str(row.get("target_column") or "").strip().lower()
    return not tgt or tgt in ("no match", "nomatch", "no-match")


def _mapping_table_for_source(job: Dict[str, Any], source_id: Optional[str]) -> List[Dict[str, Any]]:
    if not source_id:
        return []
    rows: List[Dict[str, Any]] = []
    for row in job.get("mapping_registry") or []:
        if not isinstance(row, dict) or str(row.get("source_id") or "") != str(source_id):
            continue
        unresolved = _is_unresolved_mapping_row(row)
        rows.append(
            {
                "source_column": row.get("column_name") or row.get("source_column"),
                "target_column": row.get("target_column") or ("—" if unresolved else ""),
                "confidence": row.get("confidence") or row.get("match_confidence"),
                "unresolved": unresolved,
            }
        )
    return rows


def _effective_layout_block_role(row: Dict[str, Any]) -> str:
    """Human role for Debug UI — decision overrides AI block_category."""
    from sia.agent.context_packet import _layout_registry_block_role

    role = _layout_registry_block_role(row)
    if role == "main":
        return "Main Data"
    if role == "context":
        return "Metadata"
    return "Ignored"


def _layout_blocks_for_source(job: Dict[str, Any], source_id: Optional[str]) -> List[Dict[str, Any]]:
    if not source_id:
        return []
    blocks: List[Dict[str, Any]] = []
    for row in job.get("layout_registry") or []:
        if not isinstance(row, dict) or str(row.get("source_id") or "") != str(source_id):
            continue
        blocks.append(
            {
                "block_label": row.get("block_label") or row.get("block_id"),
                "category": _effective_layout_block_role(row),
                "ai_category": row.get("block_category") or row.get("category"),
                "decision": row.get("decision"),
                "start_row": row.get("start_row"),
                "end_row": row.get("end_row"),
                "start_col": row.get("start_col"),
                "end_col": row.get("end_col"),
                "header_row": row.get("header_row"),
            }
        )
    return blocks


def _enrich_event_from_registries(job: Dict[str, Any], event: Dict[str, Any]) -> None:
    """Attach human-readable setup tables for the Debug detail pane."""
    label = str(event.get("label") or "")
    sid = event.get("source_id")
    meta = dict(event.get("metadata") or {})

    if label in ("Schema mapping saved", "Mapping registry"):
        if sid:
            table = _mapping_table_for_source(job, sid)
        else:
            table = []
            for row in job.get("mapping_registry") or []:
                if not isinstance(row, dict):
                    continue
                unresolved = _is_unresolved_mapping_row(row)
                table.append(
                    {
                        "source_id": row.get("source_id"),
                        "source_column": row.get("column_name") or row.get("source_column"),
                        "target_column": row.get("target_column") or ("—" if unresolved else ""),
                        "confidence": row.get("confidence") or row.get("match_confidence"),
                        "unresolved": unresolved,
                    }
                )
        if table:
            meta["mapping_table"] = table
            unresolved = sum(1 for r in table if r.get("unresolved"))
            meta["unresolved_targets"] = unresolved
            if unresolved and str(event.get("status") or "") == "success":
                event["status"] = "warning"
                if sid:
                    event["input_summary"] = (
                        f"{len(table)} mapping(s); {unresolved} unresolved — review before processing"
                    )

    if label in ("Demarcation saved", "Layout registry"):
        if sid:
            blocks = _layout_blocks_for_source(job, sid)
        else:
            blocks = []
            for row in job.get("layout_registry") or []:
                if not isinstance(row, dict):
                    continue
                blocks.append(
                    {
                        "source_id": row.get("source_id"),
                        "block_label": row.get("block_label") or row.get("block_id"),
                        "category": _effective_layout_block_role(row),
                        "ai_category": row.get("block_category") or row.get("category"),
                        "decision": row.get("decision"),
                        "start_row": row.get("start_row"),
                        "end_row": row.get("end_row"),
                        "start_col": row.get("start_col"),
                        "end_col": row.get("end_col"),
                        "header_row": row.get("header_row"),
                    }
                )
        if blocks:
            meta["layout_blocks"] = blocks

    if label == "Data file uploaded":
        files = list(job.get("data_files") or [])
        if files:
            meta["files"] = [
                {
                    "file_name": df.get("file_name"),
                    "sheets": list(df.get("sheets") or []),
                }
                for df in files
                if isinstance(df, dict)
            ]

    event["metadata"] = meta


def _collapse_repeated_setup_saves(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the latest save per (label, source); stash earlier saves in save_history."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    passthrough: List[Dict[str, Any]] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        label = str(ev.get("label") or "")
        if label not in _COLLAPSE_SAVE_LABELS:
            passthrough.append(ev)
            continue
        sid = str(ev.get("source_id") or "")
        key = f"{label}|{sid}"
        buckets.setdefault(key, []).append(ev)

    collapsed: List[Dict[str, Any]] = []
    for group in buckets.values():
        group.sort(
            key=lambda x: _parse_iso_datetime(x.get("timestamp"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        latest = dict(group[-1])
        if len(group) > 1:
            meta = dict(latest.get("metadata") or {})
            meta["save_count"] = len(group)
            meta["save_history"] = [
                {
                    "timestamp": g.get("timestamp"),
                    "summary": g.get("input_summary") or g.get("output_summary"),
                }
                for g in group[:-1]
            ]
            latest["metadata"] = meta
            latest["input_summary"] = (
                f"{(latest.get('input_summary') or '').rstrip('.')}"
                f" ({len(group)} saves; showing latest)."
            )
        collapsed.append(latest)

    out = passthrough + collapsed
    out.sort(key=lambda x: _parse_iso_datetime(x.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc))
    return out


def _dedupe_context_packet_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only the latest context build per processing parent."""
    latest_by_parent: Dict[str, Dict[str, Any]] = {}
    passthrough: List[Dict[str, Any]] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("label") or "") != _DEDUPE_CONTEXT_LABEL:
            passthrough.append(ev)
            continue
        parent = str(ev.get("parent_event_id") or "__root__")
        prev = latest_by_parent.get(parent)
        ts = _parse_iso_datetime(ev.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
        if prev is None:
            latest_by_parent[parent] = ev
            continue
        prev_ts = _parse_iso_datetime(prev.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
        if ts >= prev_ts:
            latest_by_parent[parent] = ev

    merged = passthrough + list(latest_by_parent.values())
    merged.sort(key=lambda x: _parse_iso_datetime(x.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc))
    return merged


def _anchor_inventory_timestamps(events: List[Dict[str, Any]]) -> None:
    """Synthetic inventory rows use job creation time; live steps use wall clock — pin inventory after upload, before setup/process."""
    setup_phases = frozenset({"demarcation", "mapping"})
    later_phases = frozenset({"process", "agent_run", "context", "collation", "deferred", "export"})
    later_times: List[datetime] = []
    upload_times: List[datetime] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = _parse_iso_datetime(ev.get("timestamp"))
        if ts is None:
            continue
        phase = str(ev.get("phase") or "")
        if phase in setup_phases or phase in later_phases:
            later_times.append(ts)
        if phase == "upload":
            upload_times.append(ts)

    if not later_times:
        return

    ceiling = min(later_times) - timedelta(milliseconds=15)
    floor = max(upload_times) + timedelta(milliseconds=2) if upload_times else None

    offset_ms = 0
    for ev in events:
        if not isinstance(ev, dict) or str(ev.get("phase") or "") != "inventory":
            continue
        anchor = ceiling - timedelta(milliseconds=offset_ms)
        if floor is not None and anchor < floor:
            anchor = floor + timedelta(milliseconds=offset_ms)
        if anchor >= min(later_times):
            anchor = min(later_times) - timedelta(milliseconds=5 + offset_ms)
        ev["timestamp"] = anchor.isoformat()
        offset_ms += 2


def _finalize_timeline(job: Dict[str, Any], events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events = _dedupe_context_packet_events(events)
    events = _collapse_repeated_setup_saves(events)
    _anchor_inventory_timestamps(events)
    events.sort(
        key=lambda x: _parse_iso_datetime(x.get("timestamp"))
        or datetime.max.replace(tzinfo=timezone.utc)
    )
    for ev in events:
        if isinstance(ev, dict):
            _enrich_event_from_registries(job, ev)
            ev["display_label"] = _display_label(ev)
    return events


def rebuild_job_debug_timeline(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rebuild orchestration timeline from persisted job fields (for older jobs and live updates)."""
    events: List[Dict[str, Any]] = []
    live = [e for e in list(job.get("job_debug_events") or []) if isinstance(e, dict)]
    base_dt = _timeline_base_datetime(job, live)

    data_files = list(job.get("data_files") or [])
    has_data_upload = _live_has_label(live, "Data file uploaded")
    if data_files and not has_data_upload:
        total_sheets = 0
        for df in data_files:
            total_sheets += len(df.get("sheets") or [])
        events.append(
            make_job_debug_event(
                label="Upload inventory",
                phase="upload",
                module="upload",
                operation="inventory",
                status="success",
                timestamp=base_dt.isoformat(),
                summary=(
                    f"{len(data_files)} workbook(s), {total_sheets} sheet(s) loaded"
                ),
                metadata={
                    "file_count": len(data_files),
                    "total_sheets": total_sheets,
                    "files": [
                        {
                            "file_id": df.get("file_id"),
                            "file_name": df.get("file_name"),
                            "sheet_count": len(df.get("sheets") or []),
                        }
                        for df in data_files
                    ],
                },
            )
        )

    registry = list(job.get("source_registry") or [])
    if registry:
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for src in registry:
            fid = str(src.get("file_id") or "")
            by_file.setdefault(fid, []).append(src)
        events.append(
            make_job_debug_event(
                label="Source registry",
                phase="inventory",
                module="source_registry",
                operation="inventory",
                status="success",
                timestamp=(base_dt + timedelta(milliseconds=1)).isoformat(),
                summary=f"{len(registry)} source(s) across {len(by_file)} workbook(s)",
                metadata={
                    "source_count": len(registry),
                    "file_count": len(by_file),
                    "sources": [
                        {
                            "source_id": s.get("source_id"),
                            "sheet_name": s.get("sheet_name"),
                            "file_id": s.get("file_id"),
                        }
                        for s in registry[:20]
                    ],
                },
            )
        )

    layout_reg = list(job.get("layout_registry") or [])
    if layout_reg and not _live_has_phase_submit(live, "demarcation"):
        by_source: Dict[str, int] = {}
        for row in layout_reg:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("source_id") or "")
            by_source[sid] = by_source.get(sid, 0) + 1

        events.append(
            make_job_debug_event(
                label="Layout registry",
                phase="demarcation",
                module="layout_registry",
                operation="summary",
                status="success",
                timestamp=_phase_summary_timestamp(
                    live,
                    "demarcation",
                    base_dt + timedelta(milliseconds=20),
                ),
                summary=f"{len(layout_reg)} layout row(s) across {len(by_source)} source(s)",
                metadata={"by_source": dict(by_source)},
            )
        )

    mapping_reg = list(job.get("mapping_registry") or [])
    if mapping_reg and not _live_has_phase_submit(live, "mapping"):
        by_source: Dict[str, int] = {}
        unresolved_by_source: Dict[str, int] = {}
        for row in mapping_reg:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("source_id") or "")
            by_source[sid] = by_source.get(sid, 0) + 1
            tgt = str(row.get("target_column") or "").strip().lower()
            if not tgt or tgt in ("no match", "nomatch", "no-match"):
                unresolved_by_source[sid] = unresolved_by_source.get(sid, 0) + 1

        events.append(
            make_job_debug_event(
                label="Mapping registry",
                phase="mapping",
                module="mapping_registry",
                operation="summary",
                status="success",
                timestamp=_phase_summary_timestamp(
                    live,
                    "mapping",
                    base_dt + timedelta(milliseconds=30),
                ),
                summary=f"{len(mapping_reg)} mapping row(s); unresolved targets: {sum(unresolved_by_source.values())}",
                metadata={
                    "by_source": dict(by_source),
                    "unresolved_by_source": dict(unresolved_by_source),
                },
            )
        )

    merged = events + live
    merged.sort(key=lambda x: _parse_iso_datetime(x.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc))
    return _finalize_timeline(job, merged)


def get_job_debug_timeline(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Full orchestration timeline (seeded inventory + live events)."""
    return rebuild_job_debug_timeline(job)


def data_files_summary(job: Dict[str, Any]) -> Dict[str, Any]:
    """Summary of uploaded workbooks for debug API."""
    data_files = list(job.get("data_files") or [])
    total_sheets = sum(len(df.get("sheets") or []) for df in data_files)
    return {
        "file_count": len(data_files),
        "total_sheets": total_sheets,
        "files": [
            {
                "file_id": df.get("file_id"),
                "file_name": df.get("file_name"),
                "sheet_count": len(df.get("sheets") or []),
                "sheets": list(df.get("sheets") or []),
            }
            for df in data_files
        ],
    }


def job_setup_summary(job: Dict[str, Any]) -> Dict[str, Any]:
    """Layout/mapping progress snapshot for debug UI."""
    registry = list(job.get("source_registry") or [])
    layout_reg = list(job.get("layout_registry") or [])
    mapping_reg = list(job.get("mapping_registry") or [])
    ux = job.get("ux_source_progress") or {}
    if not isinstance(ux, dict):
        ux = {}

    per_source = []
    for src in registry:
        if not isinstance(src, dict):
            continue
        sid = str(src.get("source_id") or "")
        layouts = [r for r in layout_reg if isinstance(r, dict) and str(r.get("source_id")) == sid]
        maps = [r for r in mapping_reg if isinstance(r, dict) and str(r.get("source_id")) == sid]
        prog = ux.get(sid)
        if not isinstance(prog, dict):
            prog = {}
        per_source.append(
            {
                "source_id": sid,
                "sheet_name": src.get("sheet_name"),
                "file_id": src.get("file_id"),
                "layout_rows": len(layouts),
                "mapping_rows": len(maps),
                "layout_complete": bool(prog.get("layout_complete")),
                "mapping_complete": bool(prog.get("mapping_complete")),
            }
        )
    return {
        "source_count": len(registry),
        "layout_registry_count": len(layout_reg),
        "mapping_registry_count": len(mapping_reg),
        "per_source": per_source,
    }
