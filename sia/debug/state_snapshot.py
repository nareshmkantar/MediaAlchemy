"""Compact debug snapshots for AgentState, ContextPacket, and job-level memory.

Snapshots are ordered for human inspection: source identity first, then run state,
context, job memory, and per-capture decision metadata. Lists are real arrays (not
only counts) where they aid debugging.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd


MAX_LIST_ITEMS = 16
MAX_STRING_LEN = 200


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > MAX_STRING_LEN:
            return value[:MAX_STRING_LEN] + "…"
        return value
    return str(type(value).__name__)


def _tool_names(tools: Any, *, limit: int = MAX_LIST_ITEMS) -> List[str]:
    out: List[str] = []
    if not isinstance(tools, list):
        return out
    for item in tools[:limit]:
        if isinstance(item, dict):
            name = str(item.get("tool") or item.get("module") or item.get("name") or "").strip()
            if name:
                out.append(name)
        elif item is not None and str(item).strip():
            out.append(str(item).strip())
    return out


def _string_list(values: Any, *, limit: int = MAX_LIST_ITEMS) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(v).strip() for v in values[:limit] if v is not None and str(v).strip()]


def _float_tail(values: Any, *, limit: int = 8) -> List[float]:
    if not isinstance(values, list):
        return []
    out: List[float] = []
    for v in values[-limit:]:
        try:
            out.append(round(float(v), 4))
        except (TypeError, ValueError):
            continue
    return out


def _frame_shape(value: Any) -> Optional[Dict[str, int]]:
    if isinstance(value, pd.DataFrame):
        return {"rows": int(len(value)), "cols": int(len(value.columns))}
    if isinstance(value, dict):
        rows = value.get("rows")
        cols = value.get("cols")
        if rows is not None or cols is not None:
            return {"rows": int(rows or 0), "cols": int(cols or 0)}
    return None


def _compact_demarcation_blocks(blocks: Any, *, limit: int = 8) -> List[Dict[str, Any]]:
    if not isinstance(blocks, list):
        return []
    out: List[Dict[str, Any]] = []
    for block in blocks[:limit]:
        if not isinstance(block, dict):
            continue
        coords = block.get("coordinates") if isinstance(block.get("coordinates"), dict) else block
        if not isinstance(coords, dict):
            continue
        label = block.get("label") or block.get("id") or block.get("block_id")
        out.append(
            {
                "label": _safe_scalar(label),
                "category": _safe_scalar(block.get("category") or block.get("block_category")),
                "start_row": coords.get("start_row"),
                "end_row": coords.get("end_row"),
                "start_col": coords.get("start_col"),
                "end_col": coords.get("end_col"),
                "header_row": coords.get("header_row"),
            }
        )
    return out


def _compact_scoped_load_scope(scoped: Any) -> Dict[str, Any]:
    if not isinstance(scoped, dict) or not scoped:
        return {}
    hdr = scoped.get("header_derivation") if isinstance(scoped.get("header_derivation"), dict) else {}
    main_blocks = _compact_demarcation_blocks(scoped.get("main_blocks"))
    out: Dict[str, Any] = {
        "scope_type": scoped.get("scope_type"),
        "requires_extraction": bool(scoped.get("requires_extraction")),
        "main_blocks_count": len(scoped.get("main_blocks") or []),
        "context_blocks_count": len(scoped.get("context_blocks") or []),
    }
    if hdr.get("header_row") is not None:
        out["header_row"] = hdr.get("header_row")
    if main_blocks:
        out["main_blocks"] = main_blocks
    sheet_frame = scoped.get("sheet_frame")
    if isinstance(sheet_frame, dict) and sheet_frame:
        out["full_sheet"] = {
            "rows": sheet_frame.get("rows"),
            "cols": sheet_frame.get("cols"),
        }
    return {k: v for k, v in out.items() if v is not None and v != "" and v != []}


def _compact_workbook_inventory(inventory: Any) -> Dict[str, Any]:
    if not isinstance(inventory, dict) or not inventory:
        return {}
    names = _string_list(inventory.get("sheet_names") or [], limit=MAX_LIST_ITEMS)
    return {
        "total_sheets": inventory.get("total_sheets"),
        "visible_sheets": inventory.get("visible_sheets"),
        "sheet_names": names,
    }


def _resolve_source_block(
    state: Dict[str, Any],
    context_packet: Optional[Dict[str, Any]],
    job: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    st = state or {}
    cp = context_packet if isinstance(context_packet, dict) else {}
    sm = cp.get("source_metadata") if isinstance(cp.get("source_metadata"), dict) else {}
    scoped = st.get("scoped_source") if isinstance(st.get("scoped_source"), dict) else {}

    source_id = st.get("source_id") or sm.get("source_id")
    sheet_name = st.get("sheet_name") or sm.get("sheet_name") or scoped.get("sheet_name")
    processing_sheet = scoped.get("sheet_name") if scoped else None

    file_name = sm.get("file_name")
    if not file_name and job and source_id:
        for entry in job.get("source_registry") or []:
            if isinstance(entry, dict) and str(entry.get("source_id")) == str(source_id):
                file_name = entry.get("file_name")
                break

    file_path = st.get("file_path")
    block: Dict[str, Any] = {
        "source_id": source_id,
        "sheet_name": sheet_name,
        "processing_sheet": processing_sheet,
        "file_name": file_name,
        "file_path": str(file_path).split("/")[-1].split("\\")[-1] if file_path else None,
        "date_granularity": sm.get("date_granularity"),
        "date_shape": sm.get("date_shape"),
    }
    uid = _string_list(sm.get("uid") or [], limit=12)
    if uid:
        block["uid_columns"] = uid
    return {k: v for k, v in block.items() if v is not None and v != "" and v != []}


def compact_context_packet(context_packet: Any) -> Dict[str, Any]:
    cp = context_packet if isinstance(context_packet, dict) else {}
    planning_summary = cp.get("planning_summary") if isinstance(cp.get("planning_summary"), dict) else {}
    source_summary = (
        planning_summary.get("source_summary")
        if isinstance(planning_summary.get("source_summary"), dict)
        else {}
    )
    layout_summary = (
        planning_summary.get("layout_summary")
        if isinstance(planning_summary.get("layout_summary"), dict)
        else {}
    )
    template_summary = (
        planning_summary.get("template_summary")
        if isinstance(planning_summary.get("template_summary"), dict)
        else {}
    )

    out: Dict[str, Any] = {
        "deferrals": {
            "weekly_rollups_to_post_union": bool(cp.get("defer_weekly_rollups_to_post_union")),
            "graph_file_relationship_review": bool(cp.get("defer_graph_file_relationship_review")),
        },
        "setup_counts": {
            "approved_mappings": len(cp.get("approved_mappings") or []),
            "business_rules": len(cp.get("business_rules") or []),
            "file_relationships": len(cp.get("file_relationships") or []),
            "relationship_proposals": len(cp.get("relationship_proposals") or []),
        },
        "planning": {
            "source_date_granularity": source_summary.get("date_granularity"),
            "source_date_shape": source_summary.get("date_shape"),
            "target_date_granularity": template_summary.get("target_date_granularity"),
            "layout_scope_type": layout_summary.get("scope_type"),
            "main_blocks_count": layout_summary.get("main_blocks_count"),
        },
    }

    available = cp.get("available_source_summaries") or []
    if isinstance(available, list) and len(available) > 1:
        out["multi_source"] = {
            "available_source_count": len(available),
            "source_ids": [
                str((s or {}).get("source_id") or "")
                for s in available[:MAX_LIST_ITEMS]
                if isinstance(s, dict) and (s or {}).get("source_id")
            ],
        }

    ledger = cp.get("job_run_ledger_summary")
    if isinstance(ledger, dict) and ledger:
        out["run_ledger"] = {
            str(k): _safe_scalar(v)
            for k, v in list(ledger.items())[:12]
        }

    lineage = cp.get("lineage") if isinstance(cp.get("lineage"), dict) else {}
    if lineage:
        out["lineage"] = {
            k: _safe_scalar(v)
            for k, v in lineage.items()
            if k in ("job_id", "source_id", "sheet_name", "file_name")
        }

    ic = cp.get("interpreted_context") if isinstance(cp.get("interpreted_context"), dict) else {}
    if ic:
        fields = dict(ic.get("fields") or {})
        evidence = list(ic.get("evidence") or [])[:MAX_LIST_ITEMS]
        scoped = dict(ic.get("scoped_fields") or {})
        if fields or evidence or scoped:
            out["interpreted_context"] = {
                "fields": fields,
                "evidence": evidence,
                "scoped_fields": {
                    str(k): v for k, v in list(scoped.items())[:MAX_LIST_ITEMS] if isinstance(v, dict)
                },
            }
        try:
            from sia.integrity.context_isolation import local_context_fields

            local = local_context_fields(cp)
            if local:
                out["local_context_fields"] = local
        except Exception:
            pass

    snippets = cp.get("context_block_snippets") or []
    if isinstance(snippets, list) and snippets:
        out["context_block_snippets"] = [
            {
                "block_label": s.get("block_label"),
                "block_id": s.get("block_id"),
                "summary": (str(s.get("summary") or "")[:120] or None),
            }
            for s in snippets[:MAX_LIST_ITEMS]
            if isinstance(s, dict)
        ]

    return out


def compact_agent_state(state: Any) -> Dict[str, Any]:
    st = state if isinstance(state, dict) else {}
    current_shape = _frame_shape(st.get("current_df")) or _frame_shape(st.get("current_frame"))
    source_shape = _frame_shape(st.get("source_frame"))

    out: Dict[str, Any] = {
        "run": {
            "multi_source_active_batch": bool(st.get("multi_source_active_batch")),
            "iteration": st.get("iteration"),
            "max_iterations": st.get("max_iterations"),
            "resume_mode": st.get("resume_mode"),
        },
        "frames": {
            "current": current_shape,
            "source": source_shape,
        },
    }

    scope = _compact_scoped_load_scope(st.get("scoped_source"))
    if scope:
        out["scope"] = scope

    workbook = _compact_workbook_inventory(st.get("file_inventory"))
    if workbook:
        out["workbook"] = workbook

    out["plan"] = {
        "suggested_tools": _tool_names(st.get("suggested_tools")),
        "deferred_post_collate_tools": _tool_names(st.get("deferred_post_collate_tools")),
    }

    hitl: Dict[str, Any] = {
        "pending_approval": bool(st.get("hitl_pending_approval")),
        "pause_type": st.get("hitl_pause_type"),
        "requires_review": bool(st.get("requires_review")),
    }
    if st.get("review_reason"):
        hitl["review_reason"] = _safe_scalar(st.get("review_reason"))
    if any(hitl.values()) or hitl.get("review_reason"):
        out["hitl"] = hitl

    history: Dict[str, Any] = {
        "tools_executed": len(st.get("tools_history") or []),
        "issues_logged": len(st.get("issues_history") or []),
    }
    trajectory = _float_tail(st.get("confidence_trajectory"))
    if trajectory:
        history["confidence_trajectory"] = trajectory
    if history["tools_executed"] or history["issues_logged"] or trajectory:
        out["history"] = history

    rel_approved = len(st.get("approved_relationships") or [])
    rel_proposals = len(st.get("relationship_proposals") or [])
    if rel_approved or rel_proposals:
        out["relationships"] = {
            "approved_count": rel_approved,
            "proposals_count": rel_proposals,
        }

    return out


def compact_memory(job: Optional[Dict[str, Any]] = None, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    job = job or {}
    state = state or {}

    route = job.get("processing_route")
    registry = job.get("source_registry") or []
    multi = bool(state.get("multi_source_active_batch")) or (
        isinstance(route, str) and str(route).startswith("multi_")
    )
    if not multi and len(registry) <= 1:
        return {}

    def_by_src = job.get("_deferred_post_collate_by_source") or {}
    deferred_by_source: Dict[str, List[str]] = {}
    if isinstance(def_by_src, dict):
        for src, tools in list(def_by_src.items())[:MAX_LIST_ITEMS]:
            names = _tool_names(tools)
            if names:
                deferred_by_source[str(src)] = names

    execution_rows: List[Dict[str, Any]] = []
    for row in (job.get("source_execution_registry") or [])[:MAX_LIST_ITEMS]:
        if not isinstance(row, dict):
            continue
        entry: Dict[str, Any] = {
            "source_id": row.get("source_id"),
            "status": row.get("status"),
        }
        deferred = _tool_names(row.get("deferred_post_collate_tools") or row.get("deferred_tools"))
        if deferred:
            entry["deferred_tools"] = deferred
        execution_rows.append(entry)

    out: Dict[str, Any] = {
        "processing_route": route,
        "source_registry_count": len(registry),
    }
    if execution_rows:
        out["source_runs"] = execution_rows
    if deferred_by_source:
        out["deferred_by_source"] = deferred_by_source

    rel_count = len(job.get("approved_file_relationships") or [])
    if rel_count:
        out["approved_file_relationships"] = rel_count

    return out


def _mapping_rows_preview(mappings: Any, *, limit: int = MAX_LIST_ITEMS) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not isinstance(mappings, list):
        return rows
    for item in mappings[:limit]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "source_column": item.get("source_column"),
                "target_column": item.get("target_column"),
                "decision": item.get("decision"),
                "confidence": round(
                    float(item.get("target_match_confidence") or item.get("confidence") or 0.0),
                    3,
                ),
            }
        )
    return rows


def _extraction_plan_preview(plan: Any) -> Dict[str, Any]:
    if plan is None:
        return {}
    if isinstance(plan, dict):
        tool_calls = plan.get("tool_calls") or []
        return {
            "confidence": plan.get("confidence"),
            "reasoning": _safe_scalar(str(plan.get("reasoning") or "")[:MAX_STRING_LEN]),
            "tool_calls": [
                {
                    "tool": (t or {}).get("tool") if isinstance(t, dict) else None,
                    "params_keys": sorted(list(((t or {}).get("params") or {}).keys()))
                    if isinstance(t, dict) and isinstance((t or {}).get("params"), dict)
                    else [],
                }
                for t in tool_calls[:MAX_LIST_ITEMS]
                if isinstance(t, dict)
            ],
            "tool_calls_count": len(tool_calls),
            "expected_columns": list(plan.get("expected_columns") or [])[:MAX_LIST_ITEMS],
        }
    tool_calls = list(getattr(plan, "tool_calls", None) or [])
    return {
        "confidence": getattr(plan, "confidence", None),
        "reasoning": _safe_scalar(str(getattr(plan, "reasoning", "") or "")[:MAX_STRING_LEN]),
        "tool_calls": [
            {
                "tool": getattr(t, "tool", None) if not isinstance(t, dict) else t.get("tool"),
                "params_keys": sorted(list((t.get("params") or {}).keys()))
                if isinstance(t, dict)
                else [],
            }
            for t in tool_calls[:MAX_LIST_ITEMS]
        ],
        "tool_calls_count": len(tool_calls),
        "expected_columns": list(getattr(plan, "expected_columns", None) or [])[:MAX_LIST_ITEMS],
    }


def _structure_analysis_preview(analysis: Any) -> Dict[str, Any]:
    if not isinstance(analysis, dict) or not analysis:
        return {}
    tables = analysis.get("tables") or []
    preview: Dict[str, Any] = {
        "confidence": analysis.get("confidence"),
        "tables_count": len(tables) if isinstance(tables, list) else 0,
        "uncertainty_count": len(analysis.get("uncertainty") or []),
    }
    if isinstance(tables, list) and tables:
        t0 = tables[0] if isinstance(tables[0], dict) else {}
        hierarchy = t0.get("hierarchy") if isinstance(t0.get("hierarchy"), dict) else {}
        if hierarchy:
            preview["hierarchy"] = hierarchy
    vp = analysis.get("visual_patterns") if isinstance(analysis.get("visual_patterns"), dict) else {}
    mls = vp.get("metric_layout_signals") if isinstance(vp.get("metric_layout_signals"), dict) else {}
    if mls:
        preview["metric_layout_signals"] = {
            "grouped_rows_likely": mls.get("grouped_rows_likely"),
            "block_sparse_metrics": (mls.get("block_sparse_metrics") or [])[:6],
            "merged_ranges_count": mls.get("merged_ranges_count"),
            "notes": (mls.get("notes") or [])[:4],
        }
    return preview


def _context_packet_drilldown(cp: Any) -> Dict[str, Any]:
    if not isinstance(cp, dict) or not cp:
        return {}
    supplement = cp.get("mapping_supplement") if isinstance(cp.get("mapping_supplement"), dict) else {}
    planning = cp.get("planning_summary") if isinstance(cp.get("planning_summary"), dict) else {}
    source_summary = (
        planning.get("source_summary") if isinstance(planning.get("source_summary"), dict) else {}
    )
    merged = list(
        supplement.get("merged_metric_ranges")
        or source_summary.get("merged_metric_ranges")
        or []
    )
    mappings = cp.get("approved_mappings") or []
    unresolved = cp.get("unresolved_items") or planning.get("mapping_summary", {}).get(
        "unresolved_items"
    )
    if not isinstance(unresolved, list):
        unresolved = []
    summary_bits = [
        f"{len(mappings)} mapping(s)",
        f"{len(merged)} merged metric range(s)",
        f"{len(unresolved)} unresolved",
    ]
    return {
        "summary": "; ".join(summary_bits),
        "detail": {
            "planning_summary": planning,
            "mapping_supplement": supplement,
            "template_contract": cp.get("template_contract"),
            "column_gaps": cp.get("column_gaps"),
            "unresolved_items": unresolved[:MAX_LIST_ITEMS],
            "duplicate_target_mappings": cp.get("duplicate_target_mappings"),
            "defer_weekly_rollups_to_post_union": cp.get("defer_weekly_rollups_to_post_union"),
        },
    }


def _output_item(summary: str, detail: Any) -> Dict[str, Any]:
    return {"summary": summary, "detail": detail}


def build_node_output_drilldown(
    state: Dict[str, Any],
    result_keys: List[str],
    *,
    node_name: str = "",
) -> Dict[str, Any]:
    """
    Per-result-key payloads for the debug UI Node output panel (post-node artifacts only).
    """
    st = state or {}
    keys = [str(k) for k in (result_keys or []) if k]
    out: Dict[str, Any] = {}

    for key in keys:
        if key == "context_packet":
            block = _context_packet_drilldown(st.get("context_packet"))
            if block:
                out[key] = block
        elif key == "mapping_summary":
            ms = st.get("mapping_summary")
            if isinstance(ms, dict) and ms:
                unresolved = ms.get("unresolved_items") or []
                out[key] = _output_item(
                    (
                        f"avg confidence {float(ms.get('average_confidence') or 0):.1%}; "
                        f"{ms.get('mapped_count', 0)} mapped; "
                        f"{len(unresolved)} unresolved"
                    ),
                    ms,
                )
        elif key == "approved_mappings":
            cp = st.get("context_packet") if isinstance(st.get("context_packet"), dict) else {}
            mappings = list(st.get("approved_mappings") or cp.get("approved_mappings") or [])
            rows = _mapping_rows_preview(mappings)
            out[key] = _output_item(f"{len(mappings)} mapping row(s)", rows)
        elif key == "scoped_source":
            scoped = st.get("scoped_source") if isinstance(st.get("scoped_source"), dict) else {}
            if scoped:
                detail = _compact_scoped_load_scope(scoped)
                mmr = list(scoped.get("merged_metric_ranges") or [])
                if mmr:
                    detail["merged_metric_ranges"] = mmr[:MAX_LIST_ITEMS]
                out[key] = _output_item(
                    (
                        f"scope={scoped.get('scope_type')}; "
                        f"{len(scoped.get('main_blocks') or [])} main block(s); "
                        f"{len(mmr)} merged range(s)"
                    ),
                    detail,
                )
        elif key == "source_metadata":
            sm = st.get("source_metadata") if isinstance(st.get("source_metadata"), dict) else {}
            if sm:
                out[key] = _output_item(
                    (
                        f"source_id={sm.get('source_id')}; "
                        f"grain={sm.get('date_granularity') or '—'}; "
                        f"shape={sm.get('date_shape') or '—'}"
                    ),
                    sm,
                )
        elif key == "structure_analysis":
            sa = _structure_analysis_preview(st.get("structure_analysis"))
            if sa:
                out[key] = _output_item(
                    f"{sa.get('tables_count', 0)} table(s); grouped={((sa.get('hierarchy') or {}).get('type'))}",
                    sa,
                )
        elif key == "structure_report":
            sr = st.get("structure_report")
            if isinstance(sr, dict) and sr:
                out[key] = _output_item("structure_report", sr)
        elif key in ("extraction_plan", "suggested_tools"):
            if key == "extraction_plan" and key in out:
                continue
            plan_preview = _extraction_plan_preview(st.get("extraction_plan"))
            tools = st.get("suggested_tools") or []
            tool_names = _tool_names(tools)
            if plan_preview or tool_names:
                detail = {"plan": plan_preview, "suggested_tools": tool_names}
                out["extraction_plan"] = _output_item(
                    f"{len(tool_names)} tool(s) in plan",
                    detail,
                )
        elif key == "current_df" or key == "current_frame":
            if "current_df" in out:
                continue
            shape = _frame_shape(st.get("current_df")) or _frame_shape(st.get("current_frame"))
            cols: List[str] = []
            df = st.get("current_df")
            if isinstance(df, pd.DataFrame):
                cols = [str(c) for c in df.columns[:MAX_LIST_ITEMS]]
            out["current_df"] = _output_item(
                f"shape {shape or {}}; columns={len(cols)} shown",
                {"shape": shape, "columns": cols},
            )
        elif key == "template_contract":
            tc = st.get("template_contract")
            if isinstance(tc, dict) and tc:
                out[key] = _output_item("template_contract", tc)
        elif key == "target_template":
            tpl = st.get("target_template")
            if isinstance(tpl, dict) and tpl:
                xs = tpl.get("x_scope") if isinstance(tpl.get("x_scope"), dict) else {}
                out[key] = _output_item(
                    f"template properties={len((tpl.get('properties') or {}))}",
                    {
                        "x_scope": xs,
                        "properties_keys": list((tpl.get("properties") or {}).keys())[:MAX_LIST_ITEMS],
                    },
                )
        elif key == "requires_review":
            out[key] = _output_item(
                f"requires_review={bool(st.get('requires_review'))}",
                {
                    "requires_review": bool(st.get("requires_review")),
                    "review_reason": _safe_scalar(st.get("review_reason")),
                },
            )
        elif key == "hitl_checkpoints":
            cps = st.get("hitl_checkpoints") or []
            if isinstance(cps, list):
                out[key] = _output_item(
                    f"{len(cps)} checkpoint(s)",
                    cps[:MAX_LIST_ITEMS],
                )
        elif key == "review_reason":
            reason = str(st.get("review_reason") or "").strip()
            if reason:
                out[key] = _output_item(reason[:120], {"review_reason": reason})
        elif key == "trace_steps":
            steps = st.get("trace_steps") or []
            if isinstance(steps, list) and steps:
                out[key] = _output_item(
                    f"{len(steps)} trace step(s) (latest appended by this node)",
                    steps[-3:],
                )
        elif key == "message":
            msg = str(st.get("message") or "")
            if not msg and isinstance(st.get("trace_steps"), list) and st["trace_steps"]:
                last = st["trace_steps"][-1]
                if isinstance(last, dict):
                    msg = str(last.get("message") or last.get("status") or "")
            if msg:
                out[key] = _output_item(msg[:160], {"message": msg})
        elif key == "grid":
            grid = st.get("grid")
            if grid is not None:
                out[key] = _output_item(
                    f"grid {getattr(grid, 'total_rows', '?')}×{getattr(grid, 'total_cols', '?')}",
                    {
                        "total_rows": getattr(grid, "total_rows", None),
                        "total_cols": getattr(grid, "total_cols", None),
                        "merged_ranges_count": len(
                            getattr(grid, "merged_ranges", None) or []
                        ),
                    },
                )
        elif key == "file_inventory":
            inv = _compact_workbook_inventory(st.get("file_inventory"))
            if inv:
                out[key] = _output_item(
                    f"{inv.get('visible_sheets')}/{inv.get('total_sheets')} sheet(s)",
                    inv,
                )
        elif key == "sheet_name":
            sn = st.get("sheet_name")
            if sn:
                out[key] = _output_item(str(sn), {"sheet_name": sn})
        elif key == "relationship_proposals":
            rp = st.get("relationship_proposals") or []
            if isinstance(rp, list) and rp:
                out[key] = _output_item(f"{len(rp)} proposal(s)", rp[:MAX_LIST_ITEMS])
        elif key == "deferred_post_collate_tools":
            deferred = _tool_names(st.get("deferred_post_collate_tools"))
            if deferred:
                out[key] = _output_item(
                    f"{len(deferred)} deferred tool(s)",
                    {"tools": deferred},
                )

    if node_name and out:
        out = dict(out)
    return out


def build_inputs_preview(state: Dict[str, Any], input_keys: List[str]) -> Dict[str, Any]:
    """Compact previews for declared node inputs (node_before panel)."""
    st = state or {}
    previews: Dict[str, Any] = {}
    for key in input_keys or []:
        k = str(key)
        if k == "target_template":
            tpl = st.get("target_template")
            if isinstance(tpl, dict) and tpl:
                previews[k] = {
                    "properties_count": len(tpl.get("properties") or {}),
                    "x_scope_keys": list((tpl.get("x_scope") or {}).keys())
                    if isinstance(tpl.get("x_scope"), dict)
                    else [],
                }
        elif k == "approved_mappings":
            previews[k] = {
                "count": len(st.get("approved_mappings") or []),
                "preview": _mapping_rows_preview(st.get("approved_mappings") or [], limit=8),
            }
        elif k == "structure_analysis":
            previews[k] = _structure_analysis_preview(st.get("structure_analysis"))
        elif k == "iteration":
            previews[k] = st.get("iteration")
        else:
            val = st.get(k)
            if val is not None and not isinstance(val, (pd.DataFrame,)):
                if hasattr(val, "total_rows"):
                    previews[k] = {
                        "total_rows": getattr(val, "total_rows", None),
                        "total_cols": getattr(val, "total_cols", None),
                    }
                elif isinstance(val, (dict, list, str, int, float, bool)):
                    previews[k] = val
    return previews


def _normalize_decision(decision: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = dict(decision or {})
    if not raw:
        return {}

    out: Dict[str, Any] = {}

    outputs = raw.get("outputs")
    if isinstance(outputs, dict) and outputs:
        out["outputs"] = outputs
    inputs_preview = raw.get("inputs_preview")
    if isinstance(inputs_preview, dict) and inputs_preview:
        out["inputs_preview"] = inputs_preview
    phase_note = raw.get("phase_note")
    if phase_note:
        out["phase_note"] = _safe_scalar(phase_note)

    if "tool" in raw:
        step: Dict[str, Any] = {"tool": raw.get("tool")}
        keys = raw.get("params_keys")
        if isinstance(keys, list):
            step["params_keys"] = _string_list(keys)
        for key in ("rows_before", "rows_after", "duration_ms", "success"):
            if key in raw:
                step[key] = raw.get(key)
        out["tool_step"] = step

    grain: Dict[str, Any] = {}
    for key in ("defer_grain", "context_defer_weekly_rollups_to_post_union"):
        if key in raw:
            grain[key] = raw.get(key)
    for list_key, label in (
        ("suggested_tools", "suggested_tools"),
        ("pre_defer_tools", "before_deferral"),
        ("tools_to_run", "tools_to_run"),
        ("deferred_post_collate_tools", "deferred_until_after_union"),
    ):
        names = _tool_names(raw.get(list_key)) if list_key in raw else []
        if names:
            grain[label] = names
    if grain:
        out["grain_and_tools"] = grain

    node: Dict[str, Any] = {}
    if raw.get("input_keys") is not None:
        node["input_keys"] = _string_list(raw.get("input_keys"))
    if raw.get("result_keys") is not None:
        node["result_keys"] = _string_list(raw.get("result_keys"))
    if "trace_steps_count" in raw:
        node["trace_steps_count"] = raw.get("trace_steps_count")
    if node:
        out["node"] = node

    load: Dict[str, Any] = {}
    for key in (
        "sheet",
        "message",
        "scoped_rows",
        "scoped_cols",
        "scope_type",
        "requires_extraction",
        "main_blocks_count",
    ):
        if key in raw:
            load[key] = raw.get(key)
    blocks = raw.get("main_blocks")
    if isinstance(blocks, list) and blocks:
        load["main_blocks"] = blocks[:MAX_LIST_ITEMS]
    if load:
        out["load"] = load

    pause: Dict[str, Any] = {}
    for key in (
        "pause_type",
        "pause_reason",
        "checkpoint_count",
        "deletion_preview_count",
        "low_confidence_count",
        "action",
        "checkpoint_id",
    ):
        if key in raw:
            pause[key] = raw.get(key)
    checkpoints = raw.get("checkpoints")
    if isinstance(checkpoints, list) and checkpoints:
        pause["checkpoints"] = checkpoints[:MAX_LIST_ITEMS]
    if pause:
        out["hitl"] = pause

    known = {
        "tool",
        "params_keys",
        "rows_before",
        "rows_after",
        "duration_ms",
        "success",
        "defer_grain",
        "context_defer_weekly_rollups_to_post_union",
        "suggested_tools",
        "pre_defer_tools",
        "tools_to_run",
        "deferred_post_collate_tools",
        "input_keys",
        "result_keys",
        "trace_steps_count",
        "sheet",
        "message",
        "scoped_rows",
        "scoped_cols",
        "scope_type",
        "requires_extraction",
        "main_blocks_count",
        "main_blocks",
        "pause_type",
        "pause_reason",
        "checkpoint_count",
        "deletion_preview_count",
        "low_confidence_count",
        "action",
        "checkpoint_id",
        "checkpoints",
    }
    extra = {k: _safe_scalar(v) for k, v in raw.items() if k not in known}
    if extra:
        out["other"] = extra

    return out


def build_state_snapshot(
    *,
    label: str,
    phase: str,
    state: Optional[Dict[str, Any]] = None,
    context_packet: Optional[Dict[str, Any]] = None,
    job: Optional[Dict[str, Any]] = None,
    decision: Optional[Dict[str, Any]] = None,
    anchor_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    st = state or {}
    cp = context_packet if context_packet is not None else st.get("context_packet")
    source = _resolve_source_block(st, cp if isinstance(cp, dict) else None, job)

    return {
        "snapshot_id": "",
        "anchor_event_id": anchor_event_id,
        "label": label,
        "phase": phase,
        "source_id": source.get("source_id"),
        "sheet_name": source.get("sheet_name"),
        "source": source,
        "agent_state": compact_agent_state(st),
        "context": compact_context_packet(cp),
        "memory": compact_memory(job, st),
        "decision": _normalize_decision(decision),
    }


def log_state_snapshot(
    label: str,
    phase: str,
    *,
    state: Optional[Dict[str, Any]] = None,
    context_packet: Optional[Dict[str, Any]] = None,
    job: Optional[Dict[str, Any]] = None,
    decision: Optional[Dict[str, Any]] = None,
) -> None:
    from sia.debug.llm_observer import get_observer

    observer = get_observer()
    if not observer:
        return
    anchor = None
    if getattr(observer, "_active_spans", None):
        anchor = observer._active_spans[-1].span_id
    observer.log_state_snapshot(
        label=label,
        phase=phase,
        payload=build_state_snapshot(
            label=label,
            phase=phase,
            state=state,
            context_packet=context_packet,
            job=job,
            decision=decision,
            anchor_event_id=anchor,
        ),
    )
