"""Resolve LangGraph entry node when resuming after HITL."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _has_grid(state: Dict[str, Any]) -> bool:
    grid = state.get("grid")
    return grid is not None


def _has_structure(state: Dict[str, Any]) -> bool:
    sa = state.get("structure_analysis")
    return isinstance(sa, dict) and bool(sa)


def _has_mappings(state: Dict[str, Any]) -> bool:
    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    approved = list(state.get("approved_mappings") or cp.get("approved_mappings") or [])
    if not approved:
        return False
    for item in approved:
        if not isinstance(item, dict):
            continue
        tgt = str(item.get("target_column") or "").strip()
        if tgt and tgt.lower() not in ("no match", "none", ""):
            return True
    return bool(approved)


def _has_plan(state: Dict[str, Any]) -> bool:
    if state.get("suggested_tools"):
        return True
    plan = state.get("extraction_plan")
    if plan is None:
        return False
    if isinstance(plan, dict):
        return bool(plan.get("tool_calls"))
    return bool(getattr(plan, "tool_calls", None))


def can_skip_pipeline_after_load(state: Dict[str, Any]) -> bool:
    """
    True when plan-review resume can reload the workbook then jump to ``execute_tools``.

    Requires cached structure analysis, approved mappings, and an approved tool plan.
    """
    if not isinstance(state, dict):
        return False
    if not bool(state.get("resume_skip_pipeline_after_load")):
        return False
    return _has_structure(state) and _has_mappings(state) and _has_plan(state)


def prepare_plan_review_resume_state(
    resume_state: Dict[str, Any],
    *,
    use_existing_plan: bool = True,
    materialized_clean_active: bool = False,
) -> Dict[str, Any]:
    """
    Normalize pending state for plan-review resume.

    Drops stale in-memory grid (clean workbook / row-count changes) and sets the
    post-``load_file`` fast path when structure, mappings, and plan are present.
    """
    out = dict(resume_state or {})
    if not use_existing_plan:
        out.pop("resume_skip_pipeline_after_load", None)
        return out

    out.pop("grid", None)
    out["current_df"] = None
    if materialized_clean_active or out.get("materialized_clean_active"):
        out["materialized_clean_active"] = True

    if _has_structure(out) and _has_mappings(out) and _has_plan(out):
        out["resume_skip_pipeline_after_load"] = True
        out["resume_graph_from"] = "load_file"
    else:
        out.pop("resume_skip_pipeline_after_load", None)

    return out


def resolve_resume_graph_entry(state: Dict[str, Any]) -> str:
    """
    Choose the LangGraph node to invoke first on HITL resume.

    Returns one of: ``load_file``, ``generate_plan``, ``execute_tools``.
    """
    if not isinstance(state, dict):
        return "load_file"

    explicit = str(state.get("resume_graph_from") or "").strip()
    if explicit in ("load_file", "generate_plan", "execute_tools"):
        return explicit

    pause_from = str(state.get("hitl_resume_from") or state.get("hitl_pause_type") or "").strip()
    resume_mode = str(state.get("resume_mode") or "").strip()

    has_grid = _has_grid(state)
    has_structure = _has_structure(state)
    has_plan = _has_plan(state)
    has_mappings = _has_mappings(state)

    # Approved plan review: reload workbook when needed, then execute approved tools only.
    if pause_from == "plan_review" and resume_mode == "use_existing_plan":
        if _has_structure(state) and has_mappings and has_plan:
            return "load_file"
        if has_grid and has_plan:
            return "execute_tools"
        return "load_file"

    # Plan rejected / regenerate: replan without re-analyzing when structure is cached.
    if pause_from == "plan_review" and resume_mode == "replan":
        if has_grid and has_structure:
            return "generate_plan"
        return "load_file"

    # Mid-execution destructive approval or execute pause.
    if pause_from in ("execute_pause", "destructive_approval") or state.get("destructive_approved"):
        if has_grid:
            return "execute_tools"
        return "load_file"

    # Verify/replan loop within a run (replan node already set suggested_tools).
    if resume_mode == "use_existing_plan" and has_grid and has_plan:
        return "execute_tools"

    if has_grid and has_structure and resume_mode == "replan":
        return "generate_plan"

    return "load_file"


def route_graph_entry(state: Dict[str, Any]) -> str:
    """Conditional entry router for :func:`create_graph`."""
    if state.get("resume_graph_from"):
        return resolve_resume_graph_entry(state)
    if state.get("hitl_resume_from") or state.get("resume_mode"):
        return resolve_resume_graph_entry(state)
    return "load_file"


def route_after_load_file(state: Dict[str, Any]) -> str:
    """After ``load_file``, skip analyze/mapping/plan when plan-review artifacts are cached."""
    if can_skip_pipeline_after_load(state):
        print(
            "[GRAPH] Plan-review resume: skipping analyze_structure/resolve_mapping/generate_plan "
            "→ execute_tools",
            flush=True,
        )
        return "execute_tools"
    return "analyze_structure"
