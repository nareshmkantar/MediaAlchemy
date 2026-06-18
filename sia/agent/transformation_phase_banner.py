"""
UI-only macro phases for the processing console (no execution changes).

Derives three high-level buckets from structure hints + planned tools + tool_executions:
1) Structure normalization & flattening (optional — hidden when scan + plan look "clean")
2) Schema alignment & typing
3) Data transformation & validation
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from sia.tools import pipeline_catalog
from sia.tools.pipeline_catalog import PipelineStage
from sia.tools.tool_validator import normalize_tool_name

# Align loosely with HITL structural review defaults (hitl.py).
_NOISE_SCORE_STRUCTURE_PHASE = 0.5
_MIN_HEADER_CANDIDATES_AMBIGUOUS = 2

_STRUCTURE_STAGES: Set[PipelineStage] = {
    PipelineStage.DISCOVERY,
    PipelineStage.LIGHTWEIGHT_PREP,
    PipelineStage.TABLE_SHAPE,
    PipelineStage.BLOCK_EXTRACTION,
    PipelineStage.LAYOUT_FILL,
}
_SCHEMA_STAGES: Set[PipelineStage] = {
    PipelineStage.SEMANTIC_MAPPING,
    PipelineStage.COLUMN_TYPING,
    PipelineStage.DATE_INTERPRETATION,
    PipelineStage.DATE_NORMALISATION,
    PipelineStage.HYGIENE,
}
_DATA_STAGES: Set[PipelineStage] = {
    PipelineStage.VALUE_STANDARDISATION,
    PipelineStage.COMBINATION_READINESS,
    PipelineStage.RESHAPING_AGGREGATION,
    PipelineStage.CONSOLIDATION,
    PipelineStage.VALIDATION,
}

_CLEAN_OVERALL_STRUCTURE = frozenset(
    {"flat", "simple", "simple_table", "tabular", "rectangular", "clean", "normalized"}
)


_MERGED_PREVIEW_LIMIT = 15


def compact_structure_hints(
    structure_report: Optional[Dict[str, Any]],
    structure_analysis: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Small JSON-safe dict for jobs and /api/status (full analysis stays off the wire)."""
    rep = structure_report if isinstance(structure_report, dict) else {}
    ana = structure_analysis if isinstance(structure_analysis, dict) else {}
    merged = rep.get("merged_cells") or []
    merged_n = len(merged) if isinstance(merged, list) else 0
    merged_regions_preview: List[Dict[str, Any]] = []
    if isinstance(merged, list):
        for entry in merged[:_MERGED_PREVIEW_LIMIT]:
            if not isinstance(entry, dict):
                continue
            merged_regions_preview.append(
                {
                    "range": entry.get("range"),
                    "top_left_value": entry.get("top_left_value"),
                    "rows_spanned": entry.get("rows_spanned"),
                    "cols_spanned": entry.get("cols_spanned"),
                }
            )
    vp = ana.get("visual_patterns") if isinstance(ana.get("visual_patterns"), dict) else {}
    grid_merged = int(vp.get("merged_cells") or 0)
    noise = float(rep.get("noise_score") or 0.0)
    hc = rep.get("header_candidates") or []
    ambig_headers = isinstance(hc, list) and len(hc) >= _MIN_HEADER_CANDIDATES_AMBIGUOUS
    overall = str(ana.get("overall_structure") or "").strip().lower()
    return {
        "sheet_name": rep.get("sheet_name"),
        "merged_cell_signals": max(merged_n, grid_merged),
        "merged_regions_total": merged_n,
        "merged_regions_preview": merged_regions_preview,
        "merged_regions_omitted": max(0, merged_n - len(merged_regions_preview)),
        "noise_score": noise,
        "ambiguous_headers": ambig_headers,
        "overall_structure": overall or None,
        "noise_above_hitl_threshold": noise > _NOISE_SCORE_STRUCTURE_PHASE,
    }


def _macro_phase_sort_key(tool_name: str) -> Optional[int]:
    raw = str(tool_name or "").strip()
    if not raw:
        return None
    normalized, _ = normalize_tool_name(raw)
    if not normalized:
        return None
    stage = pipeline_catalog.stage_for_tool(normalized)
    if stage is None:
        return None
    if stage in _STRUCTURE_STAGES:
        return 1
    if stage in _SCHEMA_STAGES:
        return 2
    if stage in _DATA_STAGES:
        return 3
    return None


def needs_structure_normalization_phase(
    hints: Optional[Dict[str, Any]],
    plan_tool_names: List[str],
) -> bool:
    """Whether to show macro phase 1 in the banner."""
    tools = [str(t).strip() for t in plan_tool_names if str(t).strip()]
    if any(_macro_phase_sort_key(t) == 1 for t in tools):
        return True

    if not hints:
        return False

    if int(hints.get("merged_cell_signals") or 0) > 0:
        return True
    if hints.get("ambiguous_headers"):
        return True
    if hints.get("noise_above_hitl_threshold"):
        return True

    overall = (hints.get("overall_structure") or "").strip().lower()
    if overall and overall not in _CLEAN_OVERALL_STRUCTURE:
        return True

    return False


def _latest_macro_from_executions(tool_executions: Optional[List[Dict[str, Any]]]) -> Optional[int]:
    last_macro: Optional[int] = None
    if not isinstance(tool_executions, list):
        return None
    for row in tool_executions:
        if not isinstance(row, dict):
            continue
        if row.get("success") is False:
            continue
        name = row.get("tool") or row.get("normalized_tool") or ""
        m = _macro_phase_sort_key(str(name))
        if m is not None:
            last_macro = m
    return last_macro


def build_transformation_phase_banner(
    *,
    hints: Optional[Dict[str, Any]],
    plan_tool_names: Optional[List[str]],
    tool_executions: Optional[List[Dict[str, Any]]],
    job_status: Optional[str],
) -> Dict[str, Any]:
    """
    Returns JSON for the Console banner: phases[], optional rationale, active_phase_id.

    Execution order is unchanged; this only reflects nominal grouping for UX.
    """
    raw_plan = plan_tool_names or []
    plan_names: List[str] = []
    for t in raw_plan:
        if isinstance(t, dict) and t.get("tool"):
            plan_names.append(str(t["tool"]))
        elif isinstance(t, str):
            plan_names.append(t)
        else:
            plan_names.append(str(t))
    plan_names = [p for p in plan_names if str(p).strip()]

    show_structure = needs_structure_normalization_phase(hints, plan_names)

    phases: List[Dict[str, Any]] = []
    phase_ids: List[str] = []

    if show_structure:
        phase_ids.append("structure")
        phases.append(
            {
                "id": "structure",
                "short": "Structure",
                "title": "Structure normalization & flattening",
                "hint": "Merged/sparse layout, headers, extraction & densify-style steps when needed.",
                "status": "pending",
            }
        )

    phase_ids.extend(["schema", "data"])
    phases.append(
        {
            "id": "schema",
            "short": "Schema",
            "title": "Schema alignment & typing",
            "hint": "Rename, cast types, dates & granularity helpers, hygiene columns.",
            "status": "pending",
        }
    )
    phases.append(
        {
            "id": "data",
            "short": "Transform",
            "title": "Data transformation",
            "hint": "Maps, calculations, allocations, aggregates, collation & verification.",
            "status": "pending",
        }
    )

    idx = {p["id"]: i for i, p in enumerate(phases)}
    latest = _latest_macro_from_executions(tool_executions)

    st = str(job_status or "").lower()
    if latest is None:
        if st == "processing":
            first_id = phase_ids[0]
            phases[idx[first_id]]["status"] = "active"
    else:
        for p in phases:
            pid = p["id"]
            if pid == "structure":
                macro_num = 1
            elif pid == "schema":
                macro_num = 2
            else:
                macro_num = 3
            if macro_num < latest:
                p["status"] = "done"
            elif macro_num == latest:
                p["status"] = "active" if st == "processing" else "done"
            else:
                p["status"] = "pending"

    if st == "completed":
        for p in phases:
            p["status"] = "done"

    rationale_parts: List[str] = []
    if show_structure:
        if hints:
            if int(hints.get("merged_cell_signals") or 0) > 0:
                rationale_parts.append("merged cells / sparse layout signals")
            if hints.get("ambiguous_headers"):
                rationale_parts.append("multiple header candidates")
            if hints.get("noise_above_hitl_threshold"):
                rationale_parts.append("elevated layout noise score")
            ov = hints.get("overall_structure")
            if ov and str(ov).lower() not in _CLEAN_OVERALL_STRUCTURE:
                rationale_parts.append(f"structure profile: {ov}")
        if any(_macro_phase_sort_key(t) == 1 for t in plan_names):
            rationale_parts.append("plan includes layout/extraction steps")

    return {
        "phases": phases,
        "show_structure_phase": show_structure,
        "active_macro_from_execution": latest,
        "rationale": rationale_parts,
    }


def persist_job_transformation_banner_inputs(
    job_id: str,
    *,
    structure_report: Optional[Dict[str, Any]] = None,
    structure_analysis: Optional[Dict[str, Any]] = None,
    plan_tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Merge hints / plan tool names onto the in-memory job for /api/status."""
    if not job_id:
        return
    try:
        from sia.agent.job_manager import job_manager
    except Exception:
        return

    job = job_manager.get_job(str(job_id))
    if not job:
        return

    hints = compact_structure_hints(structure_report, structure_analysis)
    job["structure_transform_hints"] = hints

    if plan_tool_calls is not None:
        names: List[str] = []
        for tc in plan_tool_calls:
            if isinstance(tc, dict) and tc.get("tool"):
                names.append(str(tc["tool"]))
            elif isinstance(tc, str):
                names.append(tc)
        job["transformation_plan_tools"] = names
