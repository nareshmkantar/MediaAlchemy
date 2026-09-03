"""
Pipeline checkpoint export: one JSON bundle per review stage.

Each checkpoint combines the nearest persisted state snapshot, LLM trace (if any),
context packet view, and job slice relevant at that stage — for offline audit without
clicking through the Debug UI.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sia.context.diff_log import filter_context_diff_log
from sia.debug.state_snapshot import compact_context_packet


@dataclass(frozen=True)
class CheckpointSpec:
    stage_id: str
    title: str
    description: str
    order: int
    scope: str = "per_source"  # per_source | job
    llm_components: Tuple[str, ...] = ()
    snapshot_label_contains: Tuple[str, ...] = ()
    snapshot_phases: Tuple[str, ...] = ()
    debug_event_labels: Tuple[str, ...] = ()
    job_context_keys: Tuple[str, ...] = ()
    diff_log_stages: Tuple[str, ...] = ()


CHECKPOINT_SPECS: Tuple[CheckpointSpec, ...] = (
    CheckpointSpec(
        stage_id="guided_demarcation",
        order=1,
        title="After demarcation (Guided Setup)",
        description="Main vs context blocks approved before mapping. Demarcator LLM may have run during propose-all (often not in agent llm_traces).",
        scope="job",
        job_context_keys=("layout_registry", "source_scope_registry", "demarcation_batch_proposals"),
    ),
    CheckpointSpec(
        stage_id="guided_mapping",
        order=2,
        title="After column mapping (Guided Setup)",
        description="Approved source→target mappings before Process. Schema mapper LLM may have run during mapping propose.",
        scope="job",
        llm_components=("schema_mapper",),
        job_context_keys=("mapping_registry", "pending_schema_mappings"),
    ),
    CheckpointSpec(
        stage_id="context_packet_ready",
        order=3,
        title="Context packet built (before Structure Analyzer)",
        description="Full canonical context assembled for the active source; LangGraph has not started LLM nodes yet.",
        snapshot_label_contains=("source_start", "Context packet built"),
        snapshot_phases=("before_process_file",),
        debug_event_labels=("Context packet built",),
        job_context_keys=("context_packet", "processing_route"),
    ),
    CheckpointSpec(
        stage_id="after_structure_analyzer",
        order=4,
        title="After Structure Analyzer",
        description="Bounded grid summary was sent to structure_analyzer; tables/column_analysis are in agent state.",
        llm_components=("structure_analyzer",),
        snapshot_label_contains=("analyze_structure",),
        diff_log_stages=("analyze_structure", "structure"),
    ),
    CheckpointSpec(
        stage_id="after_resolve_mapping",
        order=5,
        title="After resolve_mapping (before Plan Generator)",
        description="Scoped dataframe + approved mappings applied; planning_summary enriched. LLM only if mappings were not pre-approved.",
        llm_components=("schema_mapper",),
        snapshot_label_contains=("resolve_mapping",),
        diff_log_stages=("resolve_mapping", "mapping"),
    ),
    CheckpointSpec(
        stage_id="after_plan_generator",
        order=6,
        title="After Plan Generator",
        description="Curated context briefing + structure analysis produced extraction_plan tool_calls.",
        llm_components=("plan_generator",),
        snapshot_label_contains=("generate_plan",),
        diff_log_stages=("plan_generate", "plan_rebind", "generate_plan"),
    ),
    CheckpointSpec(
        stage_id="plan_review_hitl",
        order=7,
        title="Plan Review (HITL)",
        description="Human approval gate before execute_tools. No LLM; pending checkpoint and plan snapshot.",
        scope="job",
        job_context_keys=("pending_deletions",),
        diff_log_stages=("plan_review",),
    ),
    CheckpointSpec(
        stage_id="pre_execute",
        order=8,
        title="After plan approval / before execution",
        description="ContextVerifier + plan literal rebind run deterministically before first transform tool.",
        snapshot_label_contains=("execute_tools",),
        diff_log_stages=("plan_rebind", "pre_execute", "context_grounding"),
    ),
    CheckpointSpec(
        stage_id="after_execute_verify",
        order=9,
        title="After execution + Output Verifier",
        description="Tools ran on current_df; output_verifier LLM judged flatness/schema (may repeat per iteration).",
        llm_components=("output_verifier",),
        snapshot_label_contains=("verify_output",),
        diff_log_stages=("execute_tools", "stamp_dimensions", "align_metrics", "verify"),
    ),
    CheckpointSpec(
        stage_id="after_replan",
        order=10,
        title="Replanner (if verify failed)",
        description="LLM revision of tool plan between verify cycles. Absent when first verify passes.",
        llm_components=("replanner",),
        snapshot_label_contains=("replan",),
        diff_log_stages=("replan",),
    ),
    CheckpointSpec(
        stage_id="after_finalize",
        order=11,
        title="After finalize (+ optional LLM Judge)",
        description="Template prune/sort complete; per-source finalize stamp/align may run in web layer after graph.",
        llm_components=("llm_judge",),
        snapshot_label_contains=("source_done", "finalize"),
        snapshot_phases=("after_process_file",),
        diff_log_stages=("stamp_dimensions", "align_metrics", "finalize"),
    ),
    CheckpointSpec(
        stage_id="relationship_review",
        order=12,
        title="Relationship review (HITL)",
        description="Post-execution union/join proposal before collation. Heuristic proposals, no LLM.",
        scope="job",
        job_context_keys=(
            "_post_execution_relationship_review",
            "_pending_collation_frames",
            "approved_file_relationships",
        ),
        diff_log_stages=("relationship", "collation"),
    ),
    CheckpointSpec(
        stage_id="after_collation",
        order=13,
        title="After collation",
        description="Combined frame after union/join and optional deferred post-collate tools.",
        scope="job",
        snapshot_label_contains=("collation",),
        snapshot_phases=("after_duplicate_check_before_deferred",),
        diff_log_stages=("collation", "post_collate"),
    ),
)


def list_checkpoint_specs() -> List[Dict[str, Any]]:
    return [
        {
            "stage_id": s.stage_id,
            "order": s.order,
            "title": s.title,
            "description": s.description,
            "scope": s.scope,
            "llm_components": list(s.llm_components),
        }
        for s in CHECKPOINT_SPECS
    ]


def _norm(s: Any) -> str:
    return str(s or "").strip()


def _row_source_id(row: Dict[str, Any]) -> str:
    return _norm(row.get("source_id") or (row.get("metadata") or {}).get("source_id"))


def _filter_by_source(rows: Sequence[Dict[str, Any]], source_id: Optional[str]) -> List[Dict[str, Any]]:
    if not source_id:
        return [dict(r) for r in rows if isinstance(r, dict)]
    sid = _norm(source_id)
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rs = _row_source_id(row)
        if not rs or rs == sid:
            out.append(dict(row))
    return out


def _snapshot_score(spec: CheckpointSpec, snap: Dict[str, Any]) -> int:
    label = _norm(snap.get("label")).lower()
    phase = _norm(snap.get("phase")).lower()
    score = 0
    for needle in spec.snapshot_label_contains:
        if needle.lower() in label:
            score += 10
    for ph in spec.snapshot_phases:
        if ph.lower() == phase:
            score += 8
    if score and snap.get("timestamp"):
        score += 1
    return score


def _pick_snapshot(
    snapshots: Sequence[Dict[str, Any]],
    spec: CheckpointSpec,
) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        sc = _snapshot_score(spec, snap)
        if sc > best_score:
            best_score = sc
            best = snap
    return copy.deepcopy(best) if best else None


def _pick_llm_trace(
    traces: Sequence[Dict[str, Any]],
    spec: CheckpointSpec,
    *,
    used_trace_ids: set[str],
    occurrence: int = 0,
) -> Optional[Dict[str, Any]]:
    if not spec.llm_components:
        return None
    components = {c.lower() for c in spec.llm_components}
    matches: List[Dict[str, Any]] = []
    for tr in traces:
        if not isinstance(tr, dict):
            continue
        tid = _norm(tr.get("trace_id"))
        if tid and tid in used_trace_ids:
            continue
        comp = _norm(tr.get("component")).lower()
        if comp in components:
            matches.append(tr)
    if not matches:
        return None
    idx = min(max(0, occurrence), len(matches) - 1)
    chosen = copy.deepcopy(matches[idx])
    tid = _norm(chosen.get("trace_id"))
    if tid:
        used_trace_ids.add(tid)
    return chosen


def _pick_debug_event(
    events: Sequence[Dict[str, Any]],
    spec: CheckpointSpec,
) -> Optional[Dict[str, Any]]:
    if not spec.debug_event_labels:
        return None
    needles = [n.lower() for n in spec.debug_event_labels]
    for ev in events:
        if not isinstance(ev, dict):
            continue
        label = _norm(ev.get("label")).lower()
        if any(n in label for n in needles):
            return copy.deepcopy(ev)
    return None


def _job_slice(job: Dict[str, Any], keys: Sequence[str], *, source_id: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    sid = _norm(source_id)
    for key in keys:
        if key not in job:
            continue
        val = job.get(key)
        if val is None:
            continue
        if sid and key in ("layout_registry", "mapping_registry", "pending_schema_mappings"):
            if isinstance(val, list):
                filtered = [row for row in val if isinstance(row, dict) and _norm(row.get("source_id")) == sid]
                if filtered:
                    out[key] = copy.deepcopy(filtered)
                continue
        if sid and key == "source_scope_registry" and isinstance(val, dict):
            scoped = val.get(sid)
            if scoped is not None:
                out[key] = {sid: copy.deepcopy(scoped)}
            continue
        out[key] = copy.deepcopy(val)
    return out


def _hitl_plan_review_bundle(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pending = job.get("pending_deletions") if isinstance(job.get("pending_deletions"), dict) else None
    if pending:
        return copy.deepcopy(pending)
    trace = job.get("trace") if isinstance(job.get("trace"), dict) else {}
    cps = trace.get("hitl_checkpoints") or trace.get("pending_state") or []
    if isinstance(cps, list):
        for cp in cps:
            if isinstance(cp, dict) and _norm(cp.get("checkpoint_type")).lower() in (
                "plan_review",
                "destructive_approval",
            ):
                return copy.deepcopy(cp)
    for src in job.get("source_traces") or []:
        if not isinstance(src, dict):
            continue
        tr = src.get("trace") if isinstance(src.get("trace"), dict) else {}
        for cp in tr.get("hitl_checkpoints") or []:
            if isinstance(cp, dict) and _norm(cp.get("checkpoint_type")) == "plan_review":
                return copy.deepcopy(cp)
    return None


def _rebuild_context_packet(
    job: Dict[str, Any],
    source_id: str,
    *,
    target_template: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    try:
        from sia.agent.context_packet import build_context_packet

        reg = None
        for row in job.get("source_registry") or []:
            if isinstance(row, dict) and _norm(row.get("source_id")) == _norm(source_id):
                reg = row
                break
        sheet = _norm(reg.get("sheet_name") if reg else "")
        return build_context_packet(
            job,
            target_template=target_template,
            selected_sheet=sheet or None,
            selected_source_id=source_id,
        )
    except Exception:
        return None


def _packet_matches_source(packet: Optional[Dict[str, Any]], source_id: str) -> bool:
    if not isinstance(packet, dict) or not source_id:
        return False
    lineage = packet.get("lineage") if isinstance(packet.get("lineage"), dict) else {}
    sm = packet.get("source_metadata") if isinstance(packet.get("source_metadata"), dict) else {}
    bound = _norm(lineage.get("source_id") or sm.get("source_id"))
    return bound == _norm(source_id)


def _context_views(
    job: Dict[str, Any],
    snapshot: Optional[Dict[str, Any]],
    source_id: Optional[str],
    *,
    full_packet: bool,
    target_template: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    context_source = "empty"
    sid = _norm(source_id)

    rebuilt: Optional[Dict[str, Any]] = None
    if sid:
        rebuilt = _rebuild_context_packet(job, sid, target_template=target_template)

    if snapshot and isinstance(snapshot.get("context"), dict):
        if not sid or _norm(snapshot.get("source_id")) == sid:
            compact = copy.deepcopy(snapshot["context"])
            context_source = "snapshot"

    if not compact and rebuilt:
        compact = compact_context_packet(rebuilt)
        context_source = "rebuilt"

    if not compact and isinstance(job.get("context_packet"), dict):
        job_cp = job["context_packet"]
        if not sid or _packet_matches_source(job_cp, sid):
            compact = compact_context_packet(job_cp)
            context_source = "job_context_packet"

    full: Optional[Dict[str, Any]] = None
    if full_packet and rebuilt:
        full = rebuilt
    elif full_packet and sid:
        full = _rebuild_context_packet(job, sid, target_template=target_template)

    return {
        "compact": compact,
        "full": full,
        "full_rebuilt": full is not None,
        "context_source": context_source,
        "filtered_for_source_id": sid or None,
    }


def _carried_forward_summary(
    spec: CheckpointSpec,
    snapshot: Optional[Dict[str, Any]],
    llm_trace: Optional[Dict[str, Any]],
    job_slice: Dict[str, Any],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"stage_id": spec.stage_id}
    if snapshot:
        agent = snapshot.get("agent_state") if isinstance(snapshot.get("agent_state"), dict) else {}
        out["agent_state_keys"] = sorted(agent.keys()) if agent else []
        dec = snapshot.get("decision") if isinstance(snapshot.get("decision"), dict) else {}
        if dec:
            out["decision_keys"] = sorted(dec.keys())
    if llm_trace:
        out["llm_component"] = llm_trace.get("component")
        parsed = llm_trace.get("parsed_output")
        if isinstance(parsed, dict):
            out["llm_parsed_keys"] = sorted(parsed.keys())[:24]
    if job_slice:
        out["job_keys_present"] = sorted(job_slice.keys())
    return out


def build_checkpoint_bundle(
    job: Dict[str, Any],
    spec: CheckpointSpec,
    *,
    source_id: Optional[str] = None,
    snapshots: Sequence[Dict[str, Any]],
    llm_traces: Sequence[Dict[str, Any]],
    debug_events: Sequence[Dict[str, Any]],
    used_trace_ids: set[str],
    full_packet: bool = False,
    target_template: Optional[Dict[str, Any]] = None,
    replan_occurrence: int = 0,
) -> Dict[str, Any]:
    if spec.scope == "job" and source_id:
        effective_source = None
    else:
        effective_source = source_id

    snap_pool = _filter_by_source(snapshots, effective_source) if spec.scope == "per_source" else list(snapshots)
    trace_pool = _filter_by_source(llm_traces, effective_source) if spec.scope == "per_source" else list(llm_traces)
    event_pool = _filter_by_source(debug_events, effective_source) if spec.scope == "per_source" else list(debug_events)

    snapshot = _pick_snapshot(snap_pool, spec)
    llm_trace = _pick_llm_trace(
        trace_pool,
        spec,
        used_trace_ids=used_trace_ids,
        occurrence=replan_occurrence if spec.stage_id == "after_replan" else 0,
    )
    debug_event = _pick_debug_event(event_pool, spec)
    filter_source = _norm(source_id) or None
    job_slice = _job_slice(job, spec.job_context_keys, source_id=filter_source)

    if spec.stage_id == "plan_review_hitl":
        hitl = _hitl_plan_review_bundle(job)
        if hitl:
            job_slice["hitl_checkpoint"] = hitl

    ctx_views = _context_views(
        job,
        snapshot,
        filter_source,
        full_packet=full_packet,
        target_template=target_template,
    )

    diff_rows = filter_context_diff_log(
        job,
        stage=spec.diff_log_stages[0] if len(spec.diff_log_stages) == 1 else None,
        limit=48,
    )
    if len(spec.diff_log_stages) > 1:
        allowed = {s.lower() for s in spec.diff_log_stages}
        all_rows = filter_context_diff_log(job, stage=None, limit=96)
        diff_rows = [r for r in all_rows if _norm(r.get("stage")).lower() in allowed]

    # Always filter diff rows to the selected source when known — even for workbook
    # stages (demarcation/mapping). Otherwise Print_US / TV_FR market rows leak into
    # a Digital_UK checkpoint view.
    if filter_source:
        diff_rows = [
            r
            for r in diff_rows
            if not _norm(r.get("source_id")) or _norm(r.get("source_id")) == filter_source
        ]

    scope_note = None
    if spec.scope == "job" and filter_source:
        scope_note = (
            "Workbook-level stage: job_slice and context_diff_tail are filtered to the "
            "selected source. LLM traces for this stage may still be sparse."
        )

    status = "ok"
    if not snapshot and not llm_trace and not job_slice and not ctx_views.get("compact"):
        status = "missing"

    return {
        "stage_id": spec.stage_id,
        "order": spec.order,
        "title": spec.title,
        "description": spec.description,
        "scope": spec.scope,
        "source_id": effective_source,
        "filter_source_id": filter_source,
        "scope_note": scope_note,
        "status": status,
        "context_packet": ctx_views,
        "state_snapshot": snapshot,
        "llm_trace": llm_trace,
        "debug_event": debug_event,
        "job_slice": job_slice,
        "context_diff_tail": diff_rows,
        "carried_forward": _carried_forward_summary(spec, snapshot, llm_trace, job_slice),
    }


def build_checkpoint_export(
    job: Dict[str, Any],
    *,
    source_id: Optional[str] = None,
    stage_id: Optional[str] = None,
    full_packet: bool = False,
    target_template: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build ordered checkpoint bundles for a job.

    Parameters
    ----------
    source_id:
        When set, includes per-source checkpoints for that source plus job-scoped stages.
    stage_id:
        When set, return only that checkpoint (still wrapped in standard envelope).
    full_packet:
        When True, rebuild full context_packet via ``build_context_packet`` per per-source stage.
    """
    specs = list(CHECKPOINT_SPECS)
    if stage_id:
        specs = [s for s in specs if s.stage_id == _norm(stage_id)]
        if not specs:
            return {
                "job_id": job.get("id"),
                "error": f"Unknown stage_id: {stage_id}",
                "available_stages": [s.stage_id for s in CHECKPOINT_SPECS],
            }

    snapshots = [s for s in (job.get("state_snapshots") or []) if isinstance(s, dict)]
    llm_traces = [t for t in (job.get("llm_traces") or []) if isinstance(t, dict)]
    debug_events = [e for e in (job.get("debug_events") or []) if isinstance(e, dict)]

    if not source_id:
        for row in job.get("source_registry") or []:
            if isinstance(row, dict) and row.get("source_id"):
                source_id = _norm(row.get("source_id"))
                break

    used_trace_ids: set[str] = set()
    checkpoints: List[Dict[str, Any]] = []
    for spec in specs:
        if spec.scope == "per_source" and not source_id:
            checkpoints.append(
                {
                    "stage_id": spec.stage_id,
                    "order": spec.order,
                    "title": spec.title,
                    "status": "skipped",
                    "reason": "per_source stage requires source_id query parameter",
                }
            )
            continue
        checkpoints.append(
            build_checkpoint_bundle(
                job,
                spec,
                source_id=source_id,
                snapshots=snapshots,
                llm_traces=llm_traces,
                debug_events=debug_events,
                used_trace_ids=used_trace_ids,
                full_packet=full_packet,
                target_template=target_template,
            )
        )

    return {
        "job_id": job.get("id"),
        "filename": job.get("filename"),
        "status": job.get("status"),
        "source_id": source_id,
        "full_packet": bool(full_packet),
        "stage_filter": stage_id,
        "available_stages": list_checkpoint_specs(),
        "llm_trace_count": len(llm_traces),
        "state_snapshot_count": len(snapshots),
        "checkpoints": checkpoints,
    }
