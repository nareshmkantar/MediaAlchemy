"""
Pure helpers for multi-source job ledger, bootstrap, and processing route.

No Flask imports. Call sites live in ``web_server`` and tests.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional, Set

from sia.agent.materialized_clean_sheet import resolve_processing_workbook
from sia.tools.transformation_tools import TransformationTools

logger = logging.getLogger(__name__)

ProcessingRoute = Literal[
    "single_main",
    "multi_union",
    "multi_join",
    "needs_relationship_hitl",
]


def clear_multi_source_ledger(job: Dict[str, Any]) -> None:
    """Reset per-source deferred-tool ledger before a new multi-source batch."""
    job["_deferred_post_collate_by_source"] = {}
    job["source_execution_registry"] = []


def record_source_run_ledger(
    job: Dict[str, Any],
    source_id: str,
    sheet_meta: Dict[str, Any],
    deferred_tools: Any,
) -> None:
    """
    Append one row to ``source_execution_registry`` and merge deferred post-collate tools.

    ``sheet_meta`` keys (all optional strings):
        - ``sheet_name``: logical sheet from registry
        - ``processing_sheet``: sheet name passed to the graph (may differ when materialized)

    ``deferred_tools`` should be the list from ``trace_dict['deferred_post_collate_tools']``.
    """
    dft = list(deferred_tools or [])
    reg = list(job.get("source_execution_registry") or [])
    reg.append(
        {
            "source_id": source_id,
            "sheet_name": str(sheet_meta.get("sheet_name") or ""),
            "processing_sheet": str(sheet_meta.get("processing_sheet") or ""),
            "deferred_post_collate_tools_count": len(dft),
        }
    )
    job["source_execution_registry"] = reg
    if dft:
        acc = dict(job.get("_deferred_post_collate_by_source") or {})
        acc[source_id] = dft
        job["_deferred_post_collate_by_source"] = acc


def build_job_run_ledger_summary(job: Dict[str, Any]) -> Dict[str, Any]:
    """Compact multi-source ledger summary for planners (avoid raw deferred blobs)."""
    reg = [r for r in (job.get("source_execution_registry") or []) if isinstance(r, dict)]
    def_by_src = job.get("_deferred_post_collate_by_source") or {}
    sources_with_deferred: List[str] = []
    if isinstance(def_by_src, dict):
        sources_with_deferred = [str(k) for k, v in def_by_src.items() if v]
    return {
        "registry_row_count": len(reg),
        "source_ids": [str(r.get("source_id") or "") for r in reg if r.get("source_id") is not None],
        "total_deferred_post_collate_tool_calls": sum(
            int(r.get("deferred_post_collate_tools_count") or 0) for r in reg
        ),
        "sources_with_deferred_tools": sources_with_deferred,
        "processing_route": job.get("processing_route"),
        "source_bootstrap_complete": bool(job.get("source_bootstrap_complete")),
    }


def _compact_inventory(inv: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(inv, dict):
        return {}
    sheets = inv.get("sheets") or []
    names: List[str] = []
    if isinstance(sheets, list):
        for s in sheets[:24]:
            if isinstance(s, dict) and s.get("name") is not None:
                names.append(str(s["name"]))
    fm = inv.get("file_metadata") if isinstance(inv.get("file_metadata"), dict) else {}
    return {
        "total_sheets": inv.get("total_sheets"),
        "visible_sheets": inv.get("visible_sheets"),
        "hidden_sheets": inv.get("hidden_sheets"),
        "sheet_names_sample": names,
        "file_name": fm.get("file_name"),
        "file_size_kb": fm.get("file_size_kb"),
    }


def _compact_inspect(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    if data.get("empty"):
        return {"empty": True, "sheet_name": data.get("sheet_name")}
    dr = data.get("data_region") if isinstance(data.get("data_region"), dict) else {}
    summ = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return {
        "sheet_name": data.get("sheet_name"),
        "dimensions": data.get("dimensions"),
        "has_data_region": bool(dr),
        "data_region_rows": dr.get("rows"),
        "data_region_cols": dr.get("cols"),
        "noise_score": data.get("noise_score"),
        "density": data.get("density"),
        "header_candidate_count": len(data.get("header_candidates") or []),
        "summary": summ,
    }


def _main_block_hint_from_inspect(inspect_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Heuristic aligned with fields ``analyze_structure`` already trusts."""
    if not isinstance(inspect_data, dict):
        return {"estimated_main_blocks": 0}
    if inspect_data.get("empty"):
        return {"estimated_main_blocks": 0, "empty_sheet": True}
    dr = inspect_data.get("data_region") if isinstance(inspect_data.get("data_region"), dict) else {}
    rows = int(dr.get("rows") or 0) if dr else 0
    hc = len(inspect_data.get("header_candidates") or [])
    est = 1 if rows > 0 else (1 if hc else 0)
    return {
        "estimated_main_blocks": est,
        "header_candidates": hc,
        "noise_score": inspect_data.get("noise_score"),
        "density": inspect_data.get("density"),
    }


def _is_union_relationship(rec: Dict[str, Any]) -> bool:
    return str(rec.get("relationship_kind") or rec.get("kind") or "").strip().lower() == "union"


def _main_registry_source_ids(job: Dict[str, Any]) -> List[str]:
    """Same main-row filter as ``process_all_sources`` in ``web_server``."""
    out: List[str] = []
    for reg in job.get("source_registry") or []:
        if not isinstance(reg, dict):
            continue
        sid = reg.get("source_id")
        if sid is None:
            continue
        if reg.get("contains_main_data", True) or not reg.get("contains_reference_data"):
            out.append(str(sid))
    return out


def route_processing_mode(job: Dict[str, Any]) -> ProcessingRoute:
    """
    Deterministic route hint for logging / context (Phase 1 does not gate branches on this alone).

    Conservative rule: multiple main sources without approved relationships → ``needs_relationship_hitl``.
    """
    main_ids = _main_registry_source_ids(job)
    n_main = len(main_ids)
    if n_main <= 1:
        return "single_main"

    approved = [r for r in (job.get("approved_file_relationships") or []) if isinstance(r, dict)]
    if not approved:
        return "needs_relationship_hitl"

    any_union = any(_is_union_relationship(r) for r in approved)
    any_join = any(not _is_union_relationship(r) for r in approved)
    if any_union and not any_join:
        return "multi_union"
    if any_join and not any_union:
        return "multi_join"
    if any_union:
        return "multi_union"
    return "multi_join"


def bootstrap_job_sources(
    job: Dict[str, Any],
    *,
    job_id: str = "",
    source_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Populate ``job['source_bootstrap']`` using inventory + per-sheet inspect on effective paths.

    Uses ``resolve_processing_workbook`` so paths match the LangGraph load when materialized
    clean templates exist. Sets ``source_bootstrap_complete`` when finished (even if some sheets fail).
    """
    want: Optional[Set[str]] = None
    if source_ids is not None:
        want = {str(s).strip() for s in source_ids if str(s).strip()}

    by_source: Dict[str, Any] = {}
    inventory_by_path: Dict[str, Any] = {}
    registry = [r for r in (job.get("source_registry") or []) if isinstance(r, dict)]

    scope_registry = job.get("source_scope_registry") or {}

    for row in registry:
        canonical_id = str(row.get("source_id") or "").strip()
        if not canonical_id:
            continue
        if want is not None and canonical_id not in want:
            continue

        file_path = str(row.get("file_path") or "").strip()
        sheet_name = str(row.get("sheet_name") or "")

        scoped_raw = scope_registry.get(canonical_id) or scope_registry.get(row.get("source_id"))
        if scoped_raw is None:
            sid = canonical_id
            scoped_raw = scope_registry.get(sid)
        scoped_source = dict(scoped_raw or {})

        eff_path, eff_sheet, eff_scoped, used_mat = resolve_processing_workbook(
            job,
            canonical_id,
            file_path,
            sheet_name,
            scoped_source,
        )

        inv_compact: Dict[str, Any] = {}
        if eff_path:
            if eff_path not in inventory_by_path:
                inv_res = TransformationTools.get_file_inventory(eff_path)
                if getattr(inv_res, "success", False) and isinstance(inv_res.data, dict):
                    inventory_by_path[eff_path] = _compact_inventory(inv_res.data)
                else:
                    inventory_by_path[eff_path] = {
                        "error": getattr(inv_res, "message", None) or "inventory_failed",
                    }
                    logger.warning(
                        "[BOOTSTRAP] job=%s source=%s inventory failed: %s",
                        job_id or job.get("id"),
                        canonical_id,
                        getattr(inv_res, "message", None),
                    )
            inv_compact = dict(inventory_by_path.get(eff_path) or {})

        inspect_compact: Dict[str, Any] = {}
        inspect_raw: Optional[Dict[str, Any]] = None
        if eff_path:
            insp_res = TransformationTools.inspect_sheet_structure(eff_path, eff_sheet or None)
            if getattr(insp_res, "success", False) and isinstance(insp_res.data, dict):
                inspect_raw = insp_res.data
                inspect_compact = _compact_inspect(inspect_raw)
            else:
                inspect_compact = {
                    "error": getattr(insp_res, "message", None) or "inspect_failed",
                }
                logger.warning(
                    "[BOOTSTRAP] job=%s source=%s inspect failed: %s",
                    job_id or job.get("id"),
                    canonical_id,
                    getattr(insp_res, "message", None),
                )

        by_source[canonical_id] = {
            "registry_sheet": sheet_name,
            "effective_file_path": eff_path,
            "effective_sheet": eff_sheet,
            "used_materialized_clean": used_mat,
            "inventory": inv_compact,
            "inspect": inspect_compact,
            "main_block_hint": _main_block_hint_from_inspect(inspect_raw),
        }

    job["source_bootstrap"] = {"by_source_id": by_source, "inventory_by_effective_path": inventory_by_path}
    job["source_bootstrap_complete"] = True
    return job["source_bootstrap"]


def ensure_bootstrap_and_route(job: Dict[str, Any], job_id: str, source_ids: List[str]) -> None:
    """Bootstrap once per job (until ``source_bootstrap_complete``), then set ``processing_route``."""
    ids = [str(s).strip() for s in (source_ids or []) if str(s).strip()]
    if not job.get("source_bootstrap_complete"):
        bootstrap_job_sources(job, job_id=job_id, source_ids=ids or None)
    job["processing_route"] = route_processing_mode(job)
    logger.info(
        "[ORCH] job=%s processing_route=%s bootstrap_complete=%s",
        job_id or job.get("id"),
        job.get("processing_route"),
        bool(job.get("source_bootstrap_complete")),
    )
