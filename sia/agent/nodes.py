"""
Node implementations for the Schema Agent LangGraph.

Enhanced with:
- Tool validation before execution
- Checkpoint support for rollback
- HITL checkpoint triggers
- Semantic state tracking
"""
import logging
import asyncio
import contextlib
import time
import pandas as pd
import os
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import re

from sia.agent.base import ExtractionPlan, VerificationResult
from sia.agent.analyzer import StructureAnalyzer
from sia.agent.planner import PlanGenerator
from sia.agent.verifier import OutputVerifier
from sia.agent.replanner import Replanner, tool_plan_fingerprint
from sia.agent.scoped_source import (
    apply_scoped_source_to_grid,
    load_scoped_dataframe,
    rebase_layout_grid_tool_params,
    resolve_layout_extract_params,
)
from sia.agent.context_packet import (
    apply_context_to_dataframe,
    build_canonical_planning_view,
    compute_date_granularity_alignment,
    effective_target_date_granularity,
    mapping_is_excluded,
    normalize_mapping_records,
)
from sia.agent.target_template_utils import (
    apply_template_column_rules,
    build_rename_mapping_from_approved_mappings,
    build_template_contract,
    compute_column_gaps,
    mapping_targets_requiring_source,
    normalize_target_template,
    final_output_column_order,
    final_output_sort_columns,
    prune_dataframe_to_template,
    sanitize_rename_mapping,
    sort_dataframe_by_template,
    weekly_aggregate_group_by_columns,
)
from sia.agent.date_column_inference import (
    build_date_column_observations_for_planning,
    build_date_inference_approval_items,
    infer_date_range_column_pair,
    infer_date_range_pair_heuristic,
)
from sia.agent.schema_mapper import SchemaMapper
from sia.agent.json_safe import dumps as json_safe_dumps
from sia.agent.relationships import propose_file_relationships

from sia.agent.state import (
    AgentState, 
    add_tool_execution, 
    add_issue, 
    is_issue_repeated,
    is_stalled,
    create_checkpoint,
    should_rollback
)
from sia.models.schema import InferredSchema
from sia.models.confidence import ProcessingTrace
from sia.tools.transformation_tools import TransformationTools, execute_tool
from sia.tools.tool_validator import (
    validate_tool_call,
    dedupe_redundant_tool_calls,
    is_destructive_tool,
    get_destructive_tools,
    sort_tool_calls_by_pipeline_stage,
    normalize_tool_name,
)
from sia.tools import pipeline_catalog
from sia.agent.hitl import HITLManager, CheckpointType
from sia.agent.judge import LLMJudge

from functools import wraps
from ..debug.llm_observer import get_observer
from ..debug.state_snapshot import (
    build_inputs_preview,
    build_node_output_drilldown,
    log_state_snapshot,
)

_DATE_RANGE_PARAM_TOOLS = frozenset(
    {
        "transform.date_range_to_weekly",
        "transform.expand_date_range_to_daily",
        "transform.expand_date_range_to_weekly",
    }
)


def _try_execute_transform_tool_locally(
    tool_name: str,
    df: pd.DataFrame,
    params: Dict[str, Any],
) -> Optional[Tuple[bool, pd.DataFrame, str]]:
    """Run a transform in-process when MCP has not registered the tool yet."""
    norm, _ = normalize_tool_name(str(tool_name or "").strip())
    if norm == "transform.expand_grouped_block":
        result = TransformationTools.expand_grouped_block(
            df,
            dimension_columns=list(params.get("dimension_columns") or []),
            block_start_columns=params.get("block_start_columns"),
            allocations=params.get("allocations"),
            auto_detect_block_metrics=bool(params.get("auto_detect_block_metrics", True)),
            merged_metric_ranges=params.get("merged_metric_ranges"),
            child_numeric_sparse_threshold=float(
                params.get("child_numeric_sparse_threshold", 0.25)
            ),
            parent_numeric_rate_threshold=float(
                params.get("parent_numeric_rate_threshold", 0.55)
            ),
            row_filters=params.get("row_filters"),
        )
        if result.success:
            return True, result.data, result.message or "Success (local)"
        return False, df, result.message or "expand_grouped_block failed (local)"
    return None

def _sync_template_contract(
    target_template: Optional[Dict[str, Any]],
    context_packet: Optional[Dict[str, Any]],
    present_columns: Optional[List[Any]] = None,
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Build template_contract + column_gaps; merge into context_packet."""
    tpl = normalize_target_template(target_template) if target_template else {}
    contract = build_template_contract(tpl) if tpl else {}
    gaps = (
        compute_column_gaps(tpl, present_columns or [], approved_mappings=approved_mappings)
        if tpl
        else {}
    )
    cp = dict(context_packet or {})
    if contract:
        cp["template_contract"] = contract
    if gaps:
        cp["column_gaps"] = gaps
    return contract, gaps, cp


def _observer_pipeline_stage_for_tool(tool_name: Optional[str]) -> Optional[str]:
    if not tool_name:
        return None
    normalized, _ = normalize_tool_name(str(tool_name))
    stage = pipeline_catalog.stage_for_tool(normalized)
    if stage is not None:
        return stage.value
    if str(tool_name).startswith("collation."):
        return pipeline_catalog.PipelineStage.CONSOLIDATION.value
    return None


def _repair_date_range_tool_params(
    df: Optional[pd.DataFrame],
    state: AgentState,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Rewrite start/end column names when they are stale, missing, or collapse to one column."""
    if df is None or df.empty or not isinstance(params, dict):
        return dict(params or {})
    p = dict(params)

    def _distinct_physical_cols(a: Any, b: Any) -> Optional[tuple]:
        try:
            sa, _ = TransformationTools._fuzzy_find_column(df, a)
            sb, _ = TransformationTools._fuzzy_find_column(df, b)
        except Exception:
            return None
        if not sa or not sb or sa not in df.columns or sb not in df.columns or sa == sb:
            return None
        return sa, sb

    fixed = _distinct_physical_cols(p.get("start_date_col"), p.get("end_date_col"))
    if fixed:
        p["start_date_col"], p["end_date_col"] = fixed
        return p

    sm = dict(state.get("source_metadata") or {})
    rs = str(sm.get("range_start_column") or "").strip()
    re = str(sm.get("range_end_column") or "").strip()
    guided = _distinct_physical_cols(rs, re)
    if guided:
        p["start_date_col"], p["end_date_col"] = guided
        logger.info(
            "Repaired date range tool params from Guided Setup columns %s / %s.",
            guided[0],
            guided[1],
        )
        return p

    cp = dict(state.get("context_packet") or {})
    am = list(state.get("approved_mappings") or cp.get("approved_mappings") or [])
    tpl = state.get("target_template") or cp.get("target_template") or {}
    pair = infer_date_range_column_pair(df, am, tpl) or infer_date_range_pair_heuristic(df)
    if isinstance(pair, dict):
        inf = _distinct_physical_cols(pair.get("start_date_col"), pair.get("end_date_col"))
        if inf:
            p["start_date_col"], p["end_date_col"] = inf
            logger.info(
                "Repaired date range tool params from inferred pair %s / %s.",
                inf[0],
                inf[1],
            )
    return p


def _load_system_prompt(name: str) -> str:
    """Helper to load system prompt from file."""
    try:
        prompts_dir = Path(__file__).parent.parent.parent / "prompts"
        prompt_file = prompts_dir / f"{name}.md"
        if prompt_file.exists():
            content = prompt_file.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return content
    except:
        pass
    return ""

def _load_target_template() -> dict:
    """Load target_template.json if it exists in the project root."""
    try:
        template_path = Path(__file__).parent.parent.parent / "config" / "target_template.json"
        if template_path.exists():
            with open(template_path, "r", encoding="utf-8") as f:
                template = json.load(f)
            logger.info(f"Loaded target template with {len(template.get('properties', {}))} columns")
            return template
    except Exception as e:
        logger.warning(f"Failed to load target_template.json: {e}")
    return {}

logger = logging.getLogger(__name__)


def _tool_snapshot_basename(state: Dict[str, Any], tool_name: str) -> str:
    """CSV basename under runtime/snapshots; prefix job_id when available for cleanup."""
    timestamp_str = str(int(time.time() * 1000))
    job_id = state.get("job_id") or (state.get("context_packet") or {}).get("job_id")
    safe_tool = re.sub(r"[^\w.\-]+", "_", str(tool_name or "tool"))[:80]
    if job_id:
        return f"{job_id}_{timestamp_str}_{safe_tool}.csv"
    return f"{timestamp_str}_{safe_tool}.csv"


def _emit_job_progress(state: AgentState, step_name: str, message: str) -> None:
    """Surface long-running graph work on /api/status (processing page) while the LLM is busy."""
    job_id = (state.get("context_packet") or {}).get("job_id")
    if not job_id:
        return
    try:
        from sia.agent.job_manager import job_manager

        job_manager.update_job_status(str(job_id), "processing", message, step_name)
    except Exception:
        logger.debug("Job progress update skipped", exc_info=True)


def _persist_transformation_banner_inputs(
    state: AgentState,
    *,
    structure_report: Optional[Dict[str, Any]] = None,
    structure_analysis: Optional[Dict[str, Any]] = None,
    plan_tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Console-only macro-phase banner inputs (hints from structure scan + optional plan tools)."""
    job_id = (state.get("context_packet") or {}).get("job_id")
    if not job_id:
        return
    try:
        from sia.agent.transformation_phase_banner import persist_job_transformation_banner_inputs

        persist_job_transformation_banner_inputs(
            str(job_id),
            structure_report=structure_report if structure_report is not None else state.get("structure_report"),
            structure_analysis=structure_analysis if structure_analysis is not None else state.get("structure_analysis"),
            plan_tool_calls=plan_tool_calls,
        )
    except Exception:
        logger.debug("Transformation banner persistence skipped", exc_info=True)


def _pipeline_eval_source_id(state: AgentState) -> str:
    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    sm = state.get("source_metadata") if isinstance(state.get("source_metadata"), dict) else {}
    lineage = cp.get("lineage") if isinstance(cp.get("lineage"), dict) else {}
    return str(
        lineage.get("source_id")
        or sm.get("source_id")
        or state.get("source_id")
        or ""
    ).strip()


def _pipeline_eval_job(state: AgentState):
    from sia.agent.job_manager import job_manager
    from sia.evals.runner import PipelineEvalRunner

    job_id = state.get("job_id") or (state.get("context_packet") or {}).get("job_id")
    if not job_id:
        return None, None
    job = job_manager.get_job(str(job_id))
    if not job:
        return None, None
    PipelineEvalRunner.ensure(job)
    return job, PipelineEvalRunner


def _record_pipeline_structure_eval(state: AgentState, analysis: Dict[str, Any]) -> None:
    job, runner = _pipeline_eval_job(state)
    sid = _pipeline_eval_source_id(state)
    if not job or not runner or not sid:
        return
    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    vp = analysis.get("visual_patterns") if isinstance(analysis.get("visual_patterns"), dict) else {}
    runner.record_structure(
        job,
        sid,
        structure_analysis=analysis,
        context_packet=cp,
        metric_layout_signals=vp.get("metric_layout_signals"),
    )


def _record_pipeline_plan_eval(
    state: AgentState,
    plan: ExtractionPlan,
    *,
    approval_items: Optional[List[Dict[str, Any]]] = None,
) -> None:
    job, runner = _pipeline_eval_job(state)
    sid = _pipeline_eval_source_id(state)
    if not job or not runner or not sid:
        return
    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    raw_tools = list(getattr(plan, "raw_tool_calls", None) or plan.tool_calls or [])
    runner.record_plan(
        job,
        sid,
        raw_tool_calls=raw_tools,
        finalized_tool_calls=list(plan.tool_calls or []),
        context_packet=cp,
        target_template=state.get("target_template"),
        structure_analysis=state.get("structure_analysis"),
        plan_confidence=float(plan.confidence or 0.0),
        approval_items=approval_items if approval_items is not None else list(plan.approval_items or []),
        plan_source_id=str(getattr(plan, "source_id", "") or ""),
        resume_state=state,
    )


def _record_pipeline_plan_review_eval(state: AgentState, plan: ExtractionPlan) -> None:
    job, runner = _pipeline_eval_job(state)
    sid = _pipeline_eval_source_id(state)
    if not job or not runner or not sid:
        return
    runner.record_plan_review(
        job,
        sid,
        approval_items=list(plan.approval_items or []),
        plan_confidence=float(plan.confidence or 0.0),
        context_packet=state.get("context_packet"),
        structure_analysis=state.get("structure_analysis"),
    )


def _record_pipeline_execution_event(state: AgentState, violation: Dict[str, Any]) -> None:
    job, runner = _pipeline_eval_job(state)
    sid = _pipeline_eval_source_id(state)
    if not job or not runner or not sid:
        return
    runner.record_execution_event(job, sid, violation)


def _tool_calls_from_history(history_slice: List[Any]) -> List[Dict[str, Any]]:
    """Convert tools_history records into plan-style tool call dicts for eval diff."""
    out: List[Dict[str, Any]] = []
    for row in history_slice or []:
        if isinstance(row, dict):
            tool = row.get("tool")
            params = row.get("params") if isinstance(row.get("params"), dict) else {}
        else:
            tool = getattr(row, "tool", None)
            params = getattr(row, "params", None)
            params = params if isinstance(params, dict) else {}
        if not tool:
            continue
        out.append({"tool": str(tool), "params": dict(params)})
    return out


def _record_pipeline_execution_deferral(
    state: AgentState,
    *,
    defer_grain: bool,
    planned_tools: List[Any],
    executed_tools: List[Any],
    deferred_tools: List[Dict[str, Any]],
) -> None:
    job, runner = _pipeline_eval_job(state)
    sid = _pipeline_eval_source_id(state)
    if not job or not runner or not sid:
        return
    runner.record_execution_deferral(
        job,
        sid,
        defer_grain=defer_grain,
        planned_tools=[dict(t) for t in planned_tools if isinstance(t, dict)],
        executed_tools=[dict(t) for t in executed_tools if isinstance(t, dict)],
        deferred_tools=deferred_tools,
    )


def _record_pipeline_verify_eval(state: AgentState, issues: List[Dict[str, Any]]) -> None:
    job, runner = _pipeline_eval_job(state)
    sid = _pipeline_eval_source_id(state)
    if not job or not runner or not sid:
        return
    bucket = job.setdefault("pipeline_evals", {}).setdefault("per_source", {}).setdefault(sid, {})
    exec_row = dict(bucket.get("execution") or {"pass": True, "integrity_events": [], "violations": []})
    exec_row["verifier_issues"] = list(issues or [])[:24]
    exec_row["metrics"] = {
        **dict(exec_row.get("metrics") or {}),
        "verifier_issue_count": len(issues or []),
        "is_flat": bool(state.get("is_flat")),
    }
    bucket["execution"] = exec_row
    runner.recompute_critical_gate(job)


def _compact_blocks_for_llm(blocks: Any, max_items: int = 8) -> List[Dict[str, Any]]:
    """Shrink demarcation blocks for structure-analyzer prompts (full blocks stay on scoped_source)."""
    if not isinstance(blocks, list):
        return []
    out: List[Dict[str, Any]] = []
    for b in blocks[:max_items]:
        if not isinstance(b, dict):
            continue
        coords = b.get("coordinates") if isinstance(b.get("coordinates"), dict) else b
        if isinstance(coords, dict):
            out.append(
                {
                    "start_row": coords.get("start_row"),
                    "end_row": coords.get("end_row"),
                    "start_col": coords.get("start_col"),
                    "end_col": coords.get("end_col"),
                    "header_row": coords.get("header_row"),
                }
            )
    return out


def _repair_layout_stack_tool_params(
    state: Dict[str, Any],
    params: Dict[str, Any],
    grid: Any,
) -> Dict[str, Any]:
    """Fill stack block coords from scope/structure and refuse ambiguous full-sheet stacks."""
    from sia.agent.layout_stack_utils import (
        assess_stack_readiness,
        detect_horizontal_blocks_by_blank_columns,
        looks_like_horizontal_block_failure,
        sanitize_layout_stack_params,
    )

    p, _ = sanitize_layout_stack_params(
        dict(params or {}),
        structure_analysis=state.get("structure_analysis"),
        scoped_source=state.get("scoped_source"),
        context_packet=state.get("context_packet"),
    )
    if grid is None:
        return p
    try:
        raw_df = grid.to_dataframe() if hasattr(grid, "to_dataframe") else pd.DataFrame(grid.data)
    except Exception:
        return p
    header_row = int(p.get("header_row") or state.get("scoped_source", {}).get("header_row") or 0)
    p["header_row"] = header_row
    blocks = list(p.get("blocks") or [])
    if len(blocks) < 2:
        auto = detect_horizontal_blocks_by_blank_columns(raw_df, header_row)
        if len(auto) >= 2:
            p["blocks"] = auto
            blocks = auto
    if blocks:
        ready, _reason = assess_stack_readiness(raw_df, header_row, blocks)
        if not ready and len(blocks) < 2:
            pass
    return p


def _coerce_tool_params_to_active_workbook(state: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Bind disk-based tool args to the workbook load_file used (materialized clean when active)."""
    out = dict(params or {})
    active_fp = str(state.get("file_path") or "").strip()
    active_sheet = state.get("sheet_name")
    if active_fp and "file_path" in out:
        prev = str(out.get("file_path") or "").strip()
        if prev and prev != active_fp:
            logger.info("Tool params: normalized file_path to active pipeline workbook.")
        out["file_path"] = active_fp
    if active_sheet is not None and str(active_sheet).strip() != "" and "sheet_name" in out:
        out["sheet_name"] = active_sheet
    return out


def _enrich_tool_params_from_template(state: Dict[str, Any], tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Inject target-template contracts the planner cannot be trusted to transcribe."""
    out = dict(params or {})
    norm, _ = normalize_tool_name(str(tool_name or "").strip())

    if norm == "verify.schema":
        # The planner hand-writes `schema` inline, which silently drops minimum /
        # maximum / enum from the uploaded template. Validate against the real one.
        tpl = state.get("target_template")
        if not isinstance(tpl, dict) or not tpl:
            cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
            tpl = cp.get("target_template") if isinstance(cp.get("target_template"), dict) else None
        if isinstance(tpl, dict) and isinstance(tpl.get("properties"), dict) and tpl["properties"]:
            planner_props = out.get("schema")
            planner_keys = (
                sorted((planner_props or {}).get("properties", {}).keys())
                if isinstance(planner_props, dict)
                else []
            )
            out["schema"] = normalize_target_template(tpl)
            logger.info(
                "verify.schema: replaced planner schema (%s cols) with target template (%s cols)",
                len(planner_keys),
                len(tpl["properties"]),
            )
        return out

    if norm != "transform.apply_column_rules":
        return out
    existing = out.get("column_rules")
    if isinstance(existing, list) and len(existing) > 0:
        return out
    tpl = state.get("target_template") or {}
    bl = tpl.get("business_logic") if isinstance(tpl, dict) else {}
    rules = list((bl or {}).get("column_rules") or []) if isinstance(bl, dict) else []
    if rules:
        out["column_rules"] = rules
        logger.info("Injected %s template column_rules into transform.apply_column_rules params", len(rules))
    return out


def _column_blank_ratio(series: pd.Series) -> float:
    blank = series.isna()
    if series.dtype == object or str(series.dtype) == "string":
        blank = blank | series.astype(str).str.strip().eq("")
    return float(blank.mean()) if len(series) else 0.0


def _merged_metric_ranges_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect Excel merge spans from scoped source, context packet, or planning summary."""
    scoped = state.get("scoped_source") if isinstance(state.get("scoped_source"), dict) else {}
    merged = list(scoped.get("merged_metric_ranges") or [])
    if merged:
        return merged
    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    for key in ("merged_metric_ranges",):
        top = cp.get(key)
        if isinstance(top, list) and top:
            return list(top)
    supplement = cp.get("mapping_supplement") if isinstance(cp.get("mapping_supplement"), dict) else {}
    merged = list(supplement.get("merged_metric_ranges") or [])
    if merged:
        return merged
    planning = cp.get("planning_summary") if isinstance(cp.get("planning_summary"), dict) else {}
    merged = list(planning.get("merged_metric_ranges") or [])
    if merged:
        return merged
    src = planning.get("source_summary") if isinstance(planning.get("source_summary"), dict) else {}
    return list(src.get("merged_metric_ranges") or [])


def _repair_expand_grouped_block_tool_params(
    state: Dict[str, Any],
    params: Dict[str, Any],
    df: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """Inject merged metric spans from scope/context; do not override analyst allocation choice."""
    out = dict(params or {})
    merged = _merged_metric_ranges_from_state(state)
    if merged:
        out["merged_metric_ranges"] = merged
    mmr = out.get("merged_metric_ranges")
    if isinstance(mmr, list) and mmr:
        src_to_tgt = build_rename_mapping_from_approved_mappings(
            list(state.get("approved_mappings") or [])
        )
        if not src_to_tgt:
            cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
            src_to_tgt = build_rename_mapping_from_approved_mappings(
                list(cp.get("approved_mappings") or [])
            )
        remapped: List[Dict[str, Any]] = []
        for spec in mmr:
            if not isinstance(spec, dict):
                continue
            spec_copy = dict(spec)
            for key in ("column_name", "metric_col"):
                raw = spec_copy.get(key)
                if raw is not None:
                    s = str(raw).strip()
                    spec_copy[key] = src_to_tgt.get(s, s) if src_to_tgt else s
            remapped.append(spec_copy)
        if df is not None and not df.empty:
            fuzzy_remapped: List[Dict[str, Any]] = []
            for spec in remapped:
                if not isinstance(spec, dict):
                    continue
                spec_copy = dict(spec)
                for key in ("column_name", "metric_col"):
                    raw = spec_copy.get(key)
                    if raw is None:
                        continue
                    resolved, _ = TransformationTools._fuzzy_find_column(df, raw)
                    if resolved and resolved in df.columns:
                        spec_copy[key] = resolved
                fuzzy_remapped.append(spec_copy)
            remapped = fuzzy_remapped
        out["merged_metric_ranges"] = remapped

    if df is not None and not df.empty:
        dim_raw = list(out.get("dimension_columns") or [])
        dim_resolved: List[str] = []
        for col in dim_raw:
            resolved, _ = TransformationTools._fuzzy_find_column(df, col)
            if resolved and resolved in df.columns and resolved not in dim_resolved:
                dim_resolved.append(resolved)
        bs_raw = list(out.get("block_start_columns") or dim_raw)
        sparse_bs: List[str] = []
        for col in bs_raw:
            resolved, _ = TransformationTools._fuzzy_find_column(df, col)
            if not resolved or resolved not in df.columns:
                continue
            if _column_blank_ratio(df[resolved]) >= 0.15:
                sparse_bs.append(resolved)
        if not sparse_bs and dim_resolved:
            best = max(dim_resolved, key=lambda c: _column_blank_ratio(df[c]))
            if _column_blank_ratio(df[best]) >= 0.05:
                sparse_bs = [best]
        if dim_resolved:
            out["dimension_columns"] = dim_resolved
        mmr_list = out.get("merged_metric_ranges")
        if isinstance(mmr_list, list) and mmr_list and df is not None:
            extra_dims: List[str] = []
            for spec in mmr_list:
                if not isinstance(spec, dict):
                    continue
                cn = str(spec.get("column_name") or "").strip()
                if not cn or TransformationTools._spend_like_column_name(cn):
                    continue
                resolved, _ = TransformationTools._fuzzy_find_column(df, cn)
                if resolved and resolved in df.columns and resolved not in extra_dims:
                    extra_dims.append(resolved)
            if extra_dims:
                merged_dims = list(out.get("dimension_columns") or [])
                for col in extra_dims:
                    if col not in merged_dims:
                        merged_dims.append(col)
                out["dimension_columns"] = merged_dims
        if sparse_bs:
            out["block_start_columns"] = sparse_bs
        elif out.get("merged_metric_ranges"):
            dim_resolved = list(out.get("dimension_columns") or [])
            if dim_resolved:
                best = max(dim_resolved, key=lambda c: _column_blank_ratio(df[c]))
                if _column_blank_ratio(df[best]) >= 0.05:
                    out["block_start_columns"] = [best]
    return out


def _repair_drop_columns_tool_params(
    state: Dict[str, Any],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Remove template primary / mapped targets from drop_columns (planner safety net)."""
    from sia.integrity.drop_columns_hitl import repair_drop_columns_params

    return repair_drop_columns_params(state, params)


def _repair_aggregate_weekly_tool_params(
    state: Dict[str, Any],
    params: Dict[str, Any],
    df: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """Merge template uid + supporting columns into group_by_cols so rollup keeps publisher etc."""
    out = dict(params or {})
    tpl = state.get("target_template") or {}
    if not isinstance(tpl, dict) or not tpl or df is None or df.empty:
        return out
    expected = weekly_aggregate_group_by_columns(
        tpl,
        date_col=out.get("date_col"),
        present_columns=list(df.columns),
    )
    if not expected:
        return out
    existing = list(out.get("group_by_cols") or [])
    merged: List[str] = []
    seen: Set[str] = set()
    for col in existing + expected:
        name = str(col or "").strip()
        if not name or name not in df.columns or name in seen:
            continue
        seen.add(name)
        merged.append(name)
    if merged:
        out["group_by_cols"] = merged
    from sia.agent.target_template_utils import sanitize_aggregate_weekly_params

    return sanitize_aggregate_weekly_params(out)


def _repair_final_layout_tool_params(
    state: Dict[str, Any],
    tool_name: str,
    params: Dict[str, Any],
    df: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """Fill and reconcile final reorder/sort params against the live dataframe."""
    out = dict(params or {})
    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    tpl = normalize_target_template(
        state.get("target_template") or cp.get("target_template") or {}
    )
    if not tpl:
        return out
    norm, _ = normalize_tool_name(str(tool_name or "").strip())
    present = list(df.columns) if df is not None and not df.empty else []
    approved = list(state.get("approved_mappings") or cp.get("approved_mappings") or [])

    from sia.agent.target_template_utils import (
        DATE_UID_COLUMN_ALIASES,
        resolve_template_column_names_against_frame,
    )

    def _replace_date_uid_aliases(columns: List[Any]) -> List[Any]:
        if not present:
            return columns
        lower_present = {str(c).strip().lower(): c for c in present if str(c).strip()}
        date_col = lower_present.get("date") or lower_present.get("calendar_date")
        if not date_col:
            return columns
        out_cols: List[Any] = []
        for col in columns:
            name = str(col or "").strip()
            if name.lower() in DATE_UID_COLUMN_ALIASES and name not in present:
                out_cols.append(date_col)
            else:
                out_cols.append(col)
        return out_cols

    if norm == "transform.reorder_columns":
        order = list(final_output_column_order(tpl))
        order = _replace_date_uid_aliases(order)
        if present:
            order = resolve_template_column_names_against_frame(
                df, order, approved_mappings=approved
            )
        out["column_order"] = order
    if norm == "transform.sort_rows":
        sort_columns = list(
            final_output_sort_columns(
                tpl, present_columns=present, approved_mappings=approved
            )
        )
        sort_columns = _replace_date_uid_aliases(sort_columns)
        if present:
            sort_columns = resolve_template_column_names_against_frame(
                df, sort_columns, approved_mappings=approved
            )
        out["sort_columns"] = sort_columns
    return out


def _repair_rename_tool_params(state: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Strip No match rename targets and merge approved physical→template renames."""
    out = dict(params or {})
    approved = build_rename_mapping_from_approved_mappings(
        list(state.get("approved_mappings") or [])
    )
    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    if not approved:
        approved = build_rename_mapping_from_approved_mappings(
            list(cp.get("approved_mappings") or [])
        )
    merged = sanitize_rename_mapping(out.get("mapping"))
    merged.update(approved)
    out["mapping"] = merged
    return out


def _debug_log(hypothesis_id: str, message: str, data: Dict[str, Any]) -> None:
    try:
        payload = {
            "sessionId": "3fc92d",
            "runId": str(data.get("job_id") or data.get("checkpoint_id") or "unknown"),
            "hypothesisId": hypothesis_id,
            "location": "sia/agent/nodes.py",
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(Path(__file__).parent.parent.parent / "debug-3fc92d.log", "a", encoding="utf-8") as f:
            f.write(json_safe_dumps(payload) + "\n")
    except Exception:
        pass


def _safe_serialize_df(df: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    from sia.utils.df_preview import dataframe_to_preview_records

    _, records = dataframe_to_preview_records(df)
    return records


def _ensure_canonical_planning_summary(
    context_packet: Dict[str, Any],
    *,
    current_df: Any = None,
    scoped_source: Optional[Dict[str, Any]] = None,
    date_observations: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Rebuild planning_summary from the full context packet (never legacy mapping-only summary)."""
    cp = dict(context_packet or {})
    supplement = dict(cp.get("mapping_supplement") or {})
    if current_df is not None and hasattr(current_df, "columns"):
        supplement.setdefault("prepared_columns", [str(c) for c in current_df.columns])
    scoped = scoped_source or {}
    if scoped.get("header_derivation"):
        supplement.setdefault("header_derivation", dict(scoped.get("header_derivation") or {}))
    planning_summary = build_canonical_planning_view(cp, mapping_supplement=supplement or None)
    date_obs = dict(date_observations or {})
    if date_obs.get("columns") or date_obs.get("inferred_date_range_pair"):
        ps = dict(planning_summary)
        ss = dict(ps.get("source_summary") or {})
        if date_obs.get("columns"):
            ss["date_column_observations"] = date_obs["columns"]
        ss["date_cadence_summary"] = date_obs.get("summary") or ""
        if date_obs.get("aggregate_confidence") is not None:
            ss["date_cadence_aggregate_confidence"] = date_obs["aggregate_confidence"]
        if date_obs.get("mismatch_note"):
            ss["date_granularity_mismatch_note"] = date_obs["mismatch_note"]
        if date_obs.get("inferred_date_range_pair"):
            ss["inferred_date_range_pair"] = date_obs["inferred_date_range_pair"]
        ps["source_summary"] = ss
        planning_summary = ps
    return planning_summary


def _build_mapping_stage_supplement(
    prepared_columns: Optional[List[str]] = None,
    header_derivation: Optional[Dict[str, Any]] = None,
    sparse_dimension_columns: Optional[List[Dict[str, Any]]] = None,
    merged_metric_ranges: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the mapping-stage supplement used to enrich the canonical view.

    This intentionally returns ONLY the fields produced at the mapping stage that
    are not already captured in ``ContextPacket.planning_summary``. It must never
    be used as a standalone planning summary - use ``build_canonical_planning_view``
    to merge it into the canonical view.
    """
    return {
        "prepared_columns": list(prepared_columns or []),
        "header_derivation": header_derivation or {},
        "sparse_dimension_columns": list(sparse_dimension_columns or []),
        "merged_metric_ranges": list(merged_metric_ranges or []),
    }


def _find_sparse_dimension_columns(
    df: Optional[pd.DataFrame],
    approved_mappings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []

    keep_decisions = {"keep", "approved", "primary", "supporting", "metadata", "context", "use as context"}
    sparse: List[Dict[str, Any]] = []
    for item in approved_mappings or []:
        if not isinstance(item, dict):
            continue
        source_column = str(item.get("source_column") or "").strip()
        if not source_column or source_column not in df.columns:
            continue
        decision = str(item.get("decision", "")).strip().lower()
        if decision not in keep_decisions:
            continue
        column_type = str(item.get("source_column_type") or item.get("column_type") or "").strip().lower()
        target_column = str(item.get("target_column") or "").strip().lower()
        if (
            "metric" in column_type
            or "date" in column_type
            or target_column in {"date", "week_start", "week_end", "calendar_date"}
        ):
            continue

        series = df[source_column]
        blank_mask = series.isna()
        if series.dtype == object:
            blank_mask = blank_mask | series.astype(str).str.strip().eq("")
        blank_ratio = float(blank_mask.mean()) if len(series) else 0.0
        if 0.05 <= blank_ratio < 0.95:
            sparse.append({
                "source_column": source_column,
                "target_column": item.get("target_column") or "No match",
                "blank_ratio": round(blank_ratio, 4),
            })

    sparse.sort(key=lambda item: item.get("blank_ratio", 0.0), reverse=True)
    return sparse[:8]


def _mapping_quality_summary(approved_mappings: List[Dict[str, Any]], target_template: Dict[str, Any]) -> Dict[str, Any]:
    keep_mappings = [
        item for item in approved_mappings
        if str(item.get("decision", "")).strip().lower() != "discard"
    ]
    confidences = [
        float(item.get("target_match_confidence") or item.get("confidence") or 0.0)
        for item in keep_mappings
        if item.get("target_column") and item.get("target_column") != "No match"
    ]
    average_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    mandatory_targets = sorted(mapping_targets_requiring_source(target_template)) if isinstance(target_template, dict) else []
    mapped_targets = {
        item.get("target_column")
        for item in keep_mappings
        if item.get("target_column") and item.get("target_column") != "No match"
    }
    unresolved = [
        {"target_column": column, "status": "missing_mapping", "requirement": "requires_source"}
        for column in mandatory_targets
        if column not in mapped_targets
    ]
    return {
        "average_confidence": average_confidence,
        "mapped_count": len([item for item in keep_mappings if item.get("target_column") and item.get("target_column") != "No match"]),
        "unresolved_items": unresolved,
    }


def _evaluate_business_rules(df: pd.DataFrame, business_rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return issues

    for rule in business_rules or []:
        target_column = rule.get("target_column")
        rule_type = rule.get("rule_type")
        expression = rule.get("rule_expression")
        if not target_column:
            continue

        if target_column not in df.columns:
            issues.append({
                "issue_type": "business_rule_missing_column",
                "description": f"Rule target column '{target_column}' is missing from output.",
                "rule_type": rule_type,
                "target_column": target_column,
                "severity": "high" if rule.get("is_mandatory") else "medium",
            })
            continue

        series = df[target_column]
        if rule_type in {"fill_blank", "default_value"}:
            blank_mask = series.isna()
            if series.dtype == object:
                blank_mask = blank_mask | series.astype(str).str.strip().eq("")
            if blank_mask.any():
                issues.append({
                    "issue_type": "business_rule_blank_violation",
                    "description": f"Column '{target_column}' still contains blanks after applying rule '{rule_type}'.",
                    "rule_type": rule_type,
                    "target_column": target_column,
                    "severity": "medium",
                })
        elif rule_type == "format" and str(expression).lower() == "yyyy-mm-dd":
            non_null = series.dropna().astype(str)
            invalid = non_null[~non_null.str.match(r"^\d{4}-\d{2}-\d{2}$")]
            if not invalid.empty:
                issues.append({
                    "issue_type": "business_rule_format_violation",
                    "description": f"Column '{target_column}' contains values not matching YYYY-MM-DD.",
                    "rule_type": rule_type,
                    "target_column": target_column,
                    "severity": "medium",
                })
        elif rule_type == "cross_field_constraint":
            issues.append({
                "issue_type": "business_rule_manual_check",
                "description": f"Cross-field constraint for '{target_column}' requires manual or custom validation: {expression}",
                "rule_type": rule_type,
                "target_column": target_column,
                "severity": "low",
            })

    return issues


def trace_node(name: str, input_keys: List[str] = None):
    """Decorator to trace node execution with specific state keys as input."""
    def decorator(func):
        @wraps(func)
        def wrapper(state: AgentState):
            observer = get_observer()
            if observer:
                # Extract requested input keys or default to sheet_name
                input_data = {k: state.get(k) for k in input_keys} if input_keys else {"sheet": state.get("sheet_name")}
                with observer.trace_span(name, type="node", input=input_data) as span:
                    try:
                        _input_keys = list(input_keys or ["sheet_name"])
                        log_state_snapshot(
                            f"state.{name}",
                            "node_before",
                            state=state,
                            decision={
                                "input_keys": _input_keys,
                                "inputs_preview": build_inputs_preview(state, _input_keys),
                                "phase_note": "Inputs available before this node runs.",
                            },
                        )
                        result = func(state)
                        # Ensure result is a dict to merge with state
                        if not isinstance(result, dict):
                            result = {}
                        
                        if span:
                            span.output = result
                        merged_state = {**state, **result}
                        _result_keys = sorted(list(result.keys()))
                        decision_after: Dict[str, Any] = {
                            "input_keys": list(input_keys or ["sheet_name"]),
                            "result_keys": _result_keys,
                            "trace_steps_count": len(result.get("trace_steps") or []),
                            "outputs": build_node_output_drilldown(
                                merged_state,
                                _result_keys,
                                node_name=name,
                            ),
                            "phase_note": "Expand each output to inspect payloads passed downstream.",
                        }
                        if name == "load_file" and isinstance(result, dict):
                            scoped = result.get("scoped_source") if isinstance(result.get("scoped_source"), dict) else {}
                            sf = result.get("source_frame") if isinstance(result.get("source_frame"), dict) else {}
                            decision_after["load"] = {
                                "sheet": result.get("sheet_name"),
                                "message": (result.get("message") or "")[:200],
                                "scoped_rows": sf.get("rows"),
                                "scoped_cols": sf.get("cols"),
                                "scope_type": scoped.get("scope_type"),
                                "requires_extraction": bool(scoped.get("requires_extraction")),
                                "main_blocks_count": len(scoped.get("main_blocks") or []),
                                "main_blocks": _compact_blocks_for_llm(scoped.get("main_blocks")),
                            }
                        log_state_snapshot(
                            f"state.{name}",
                            "node_after",
                            state=merged_state,
                            decision=decision_after,
                        )
                        return result
                    except Exception as e:
                        logger.error(f"Node '{name}' crashed: {e}")
                        import traceback
                        error_tb = traceback.format_exc()
                        error_msg = f"Crash in node '{name}': {str(e)}"
                        if span:
                            span.error_message = f"{error_msg}\n{error_tb}"
                            span.status = "error"
                            
                        # CRITICAL: Re-raise exception so LangGraph stops!
                        # Swallowing it causes state not to update (iteration stays same) -> Infinite Loop
                        raise e
                        
                        # Return delta to append error without wiping state
                        return {
                            "errors": [error_msg],
                            "trace_steps": [{
                                "step": name,
                                "error": str(e),
                                "message": f"CRITICAL: Node crashed - {e}",
                                "status": "error"
                            }]
                        }
            else:
                try:
                    result = func(state)
                    return result if isinstance(result, dict) else {}
                except Exception as e:
                    logger.error(f"Node '{name}' crashed (no observer): {e}")
                    # Keep behavior consistent regardless of observer availability:
                    # fail fast and let orchestrator handle the terminal error path.
                    raise
        return wrapper
    return decorator


def _set_inline_pipeline_steps(steps: List[Dict[str, Any]]) -> None:
    """Debug-only: fixed sub-steps under a graph node (not planner tool_calls / MCP)."""
    observer = get_observer()
    if not observer or not getattr(observer, "_active_spans", None):
        return
    observer._active_spans[-1].metadata["inline_pipeline_steps"] = list(steps)


@trace_node("load_file", input_keys=["file_path", "sheet_name"])
def load_file_node(state: AgentState) -> Dict[str, Any]:
    """Load Excel file into Grid object."""
    print(f"[NODE_TRACE] Running load_file for: {state.get('file_path')}", flush=True)
    from ..modules.visual_normalizer import VisualNormalizer

    _emit_job_progress(
        state,
        "Load workbook",
        "Reading the workbook and normalizing sheet layout (headers, merges, grid shape)…",
    )

    file_path = state["file_path"]
    visual_normalizer = VisualNormalizer()
    
    # We load all sheets, but for this graph we focus on the specific sheet_name
    # In a full migration, we might iterate over sheets outside or have a specialized node.
    # For now, we assume single sheet processing or we process the "first" sheet if not specified.
    
    grids, norm_conf = visual_normalizer.normalize_workbook(file_path)
    inline_steps: List[Dict[str, Any]] = [
        {
            "id": "visual_normalizer.normalize_workbook",
            "label": "VisualNormalizer.normalize_workbook",
            "kind": "deterministic_function",
            "status": "ok",
            "detail": f"{len(grids)} sheet grid(s) materialized",
        },
    ]
    
    target_sheet = state["sheet_name"]
    # If sheet not found or not specified, pick the first one
    if target_sheet not in grids:
        if grids:
            target_sheet = list(grids.keys())[0]
        else:
            inline_steps[0]["status"] = "error"
            inline_steps[0]["detail"] = "No sheets found in workbook"
            _set_inline_pipeline_steps(inline_steps)
            return {"errors": ["No sheets found in file"]}
            
    grid = grids[target_sheet]
    scoped_grid, scoped_source = apply_scoped_source_to_grid(grid, state.get("scoped_source"))
    inline_steps.append(
        {
            "id": "apply_scoped_source_to_grid",
            "label": "apply_scoped_source_to_grid",
            "kind": "deterministic_function",
            "status": "ok",
            "detail": f"Scoped working grid {scoped_grid.total_rows}×{scoped_grid.total_cols} (sheet={target_sheet!r})",
        }
    )

    file_inventory_summary: Dict[str, Any] = {}
    inv_ok = False
    inv_detail = ""
    try:
        inv_res = TransformationTools.get_file_inventory(str(file_path))
        if getattr(inv_res, "success", False) and isinstance(inv_res.data, dict):
            inv_ok = True
            invd = inv_res.data
            sheets = invd.get("sheets") or []
            names = [str(s.get("name")) for s in sheets[:40] if isinstance(s, dict) and s.get("name")]
            file_inventory_summary = {
                "total_sheets": invd.get("total_sheets"),
                "visible_sheets": invd.get("visible_sheets"),
                "hidden_sheets": invd.get("hidden_sheets"),
                "sheet_names": names,
                "file_metadata": invd.get("file_metadata") if isinstance(invd.get("file_metadata"), dict) else {},
            }
            vis = invd.get("visible_sheets")
            tot = invd.get("total_sheets")
            inv_detail = f"visible {vis}, total {tot} tab(s); first names: {', '.join(names[:8])}" if names else f"visible {vis}, total {tot} tab(s)"
        else:
            inv_detail = (getattr(inv_res, "message", None) or "inventory returned no data")[:240]
    except Exception as inv_exc:
        logger.warning("load_file: discovery.inventory failed: %s", inv_exc)
        inv_detail = str(inv_exc)[:240]
    inline_steps.append(
        {
            "id": "TransformationTools.get_file_inventory",
            "label": "Workbook inventory (parity with discovery.inventory tool)",
            "kind": "deterministic_function",
            "status": "ok" if inv_ok else "error",
            "detail": inv_detail or ("ok" if inv_ok else "failed"),
        }
    )
    
    trace_step = {
        "step": "load_file",
        "sheet": target_sheet,
        "rows": scoped_grid.total_rows,
        "cols": scoped_grid.total_cols,
        "confidence": 1.0,  # Deterministic step
        "file_path": str(file_path),
        "workbook_sheet_names": list(grids.keys())[:80],
        "workbook_sheet_count": len(grids),
        "normalize_confidence": getattr(norm_conf, "score", None),
        "normalize_module": getattr(norm_conf, "module_name", "") or "visual_normalizer",
        "file_inventory": file_inventory_summary,
    }

    observer = get_observer()
    if observer and observer._active_spans:
        cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
        sm = state.get("source_metadata") if isinstance(state.get("source_metadata"), dict) else {}
        meta = {
            "node": "load_file",
            "langgraph_layer": "per_source",
            "file_basename": Path(str(file_path)).name,
            "workbook_sheet_names": list(grids.keys())[:80],
            "workbook_sheet_count": len(grids),
            "selected_sheet": target_sheet,
            "scoped_rows": int(scoped_grid.total_rows),
            "scoped_cols": int(scoped_grid.total_cols),
            "normalize_confidence": getattr(norm_conf, "score", None),
        }
        if cp.get("job_id"):
            meta["job_id"] = cp.get("job_id")
        if sm.get("source_id"):
            meta["source_id"] = sm.get("source_id")
        if file_inventory_summary:
            meta["inventory_total_sheets"] = file_inventory_summary.get("total_sheets")
        _set_inline_pipeline_steps(inline_steps)
        observer._active_spans[-1].metadata.update(meta)
    
    return {
        "grid": scoped_grid,
        "sheet_name": target_sheet, # Update in case we defaulted
        "scoped_source": scoped_source,
        "source_frame": {"rows": scoped_grid.total_rows, "cols": scoped_grid.total_cols}, # PHYSICAL ANCHOR
        "file_inventory": file_inventory_summary or None,
        "message": f"Loaded {scoped_grid.total_rows} scoped rows from {target_sheet}",
        "trace_steps": [trace_step]
    }


@trace_node("resolve_mapping", input_keys=["target_template", "approved_mappings"])
def resolve_mapping_node(state: AgentState) -> Dict[str, Any]:
    """Resolve or propose schema mapping as a native graph stage."""
    target_template = state.get("target_template") or _load_target_template()
    approved_mappings = list(state.get("approved_mappings", []))
    business_rules = list(state.get("business_rules", []))
    source_metadata = dict(state.get("source_metadata") or {})
    context_packet = dict(state.get("context_packet") or {})
    user_notes = list(context_packet.get("user_notes") or [])
    file_path = state.get("file_path", "")
    sheet_name = state.get("sheet_name")
    scoped_source = state.get("scoped_source")

    if source_metadata and not source_metadata.get("sheet_name"):
        source_metadata["sheet_name"] = sheet_name

    incoming_approved_mappings = bool(list(state.get("approved_mappings", [])))

    if not target_template and not approved_mappings:
        _set_inline_pipeline_steps(
            [
                {
                    "id": "resolve_mapping",
                    "label": "resolve_mapping",
                    "kind": "graph_node",
                    "status": "skipped",
                    "detail": "No target template and no approved mappings",
                }
            ]
        )
        return {
            "mapping_summary": {
                "average_confidence": 1.0,
                "mapped_count": 0,
                "unresolved_items": [],
            },
            "trace_steps": [{
                "step": "resolve_mapping",
                "mappings_count": 0,
                "confidence": 1.0,
                "status": "skipped",
            }]
        }

    rm_inline: List[Dict[str, Any]] = []

    prepared_df = None
    resolved_scope = scoped_source
    try:
        prepared_df, resolved_scope = load_scoped_dataframe(file_path, sheet_name, scoped_source)
        if prepared_df is not None and not prepared_df.empty:
            rm_inline.append(
                {
                    "id": "load_scoped_dataframe",
                    "label": "load_scoped_dataframe",
                    "kind": "deterministic_function",
                    "status": "ok",
                    "detail": f"{len(prepared_df)} rows × {len(prepared_df.columns)} cols",
                }
            )
        else:
            rm_inline.append(
                {
                    "id": "load_scoped_dataframe",
                    "label": "load_scoped_dataframe",
                    "kind": "deterministic_function",
                    "status": "ok",
                    "detail": "Empty or missing scoped dataframe (headers only or no data)",
                }
            )
    except Exception as exc:
        logger.warning(f"Mapping stage failed to prepare scoped dataframe: {exc}")
        rm_inline.append(
            {
                "id": "load_scoped_dataframe",
                "label": "load_scoped_dataframe",
                "kind": "deterministic_function",
                "status": "error",
                "detail": str(exc)[:240],
            }
        )

    source_id = source_metadata.get("source_id") or f"{Path(file_path).stem}:{sheet_name or 'default'}"
    mapping_origin = "approved"

    if approved_mappings:
        approved_mappings = normalize_mapping_records(approved_mappings, source_id=source_id)
    elif prepared_df is not None and not prepared_df.empty:
        target_columns = list((target_template or {}).get("properties", {}).keys())
        mapper = SchemaMapper(llm_client=state.get("llm_client"), prompts_dir="prompts")
        try:
            loop = asyncio.new_event_loop()
            proposed_mappings = loop.run_until_complete(
                mapper.propose_mapping(
                    prepared_df,
                    target_columns=target_columns,
                    allow_heuristic_fallback=True,
                )
            )
        finally:
            with contextlib.suppress(Exception):
                loop.close()
        approved_mappings = normalize_mapping_records(proposed_mappings, source_id=source_id)
        mapping_origin = "proposed"

    if incoming_approved_mappings:
        rm_inline.append(
            {
                "id": "mapping.use_approved",
                "label": "Use analyst-approved mappings (normalize_mapping_records)",
                "kind": "deterministic_function",
                "status": "ok",
                "detail": f"{len(approved_mappings)} mapping row(s) after normalize",
            }
        )
    elif prepared_df is not None and not prepared_df.empty:
        rm_inline.append(
            {
                "id": "SchemaMapper.propose_mapping",
                "label": "SchemaMapper.propose_mapping (async LLM / heuristic)",
                "kind": "llm",
                "status": "ok",
                "detail": f"{len(approved_mappings)} proposed mapping row(s); origin={mapping_origin!r}",
            }
        )
    else:
        rm_inline.append(
            {
                "id": "mapping.resolve",
                "label": "Schema column mapping",
                "kind": "skipped",
                "status": "skipped",
                "detail": "No incoming approved mappings and scoped dataframe unusable for proposal",
            }
        )

    quality = _mapping_quality_summary(approved_mappings, target_template or {})
    sparse_dimension_columns = _find_sparse_dimension_columns(prepared_df, approved_mappings)

    mapping_supplement = _build_mapping_stage_supplement(
        prepared_columns=list(prepared_df.columns) if prepared_df is not None and not prepared_df.empty else [],
        header_derivation=(resolved_scope or {}).get("header_derivation") or {},
        sparse_dimension_columns=sparse_dimension_columns,
        merged_metric_ranges=list((resolved_scope or {}).get("merged_metric_ranges") or []),
    )

    context_packet["source_metadata"] = source_metadata
    from sia.agent.planner_decisions import duplicate_target_sources

    context_packet["approved_mappings"] = approved_mappings
    dup_map = duplicate_target_sources(approved_mappings)
    if dup_map:
        context_packet["duplicate_target_mappings"] = dup_map
    context_packet["business_rules"] = business_rules
    context_packet["target_template"] = target_template or {}
    _cols_preview: List[str] = []
    if prepared_df is not None and hasattr(prepared_df, "columns"):
        _cols_preview = [str(c) for c in prepared_df.columns]
    template_contract, column_gaps, context_packet = _sync_template_contract(
        target_template,
        context_packet,
        present_columns=_cols_preview,
        approved_mappings=approved_mappings,
    )
    context_packet["user_notes"] = user_notes
    context_packet["mapping_supplement"] = mapping_supplement
    merged_for_context = list((resolved_scope or {}).get("merged_metric_ranges") or [])
    if merged_for_context:
        context_packet["merged_metric_ranges"] = merged_for_context
    context_packet["planning_summary"] = build_canonical_planning_view(
        context_packet,
        mapping_supplement=mapping_supplement,
    )
    context_packet["unresolved_items"] = quality["unresolved_items"]

    hitl_checkpoints = list(state.get("hitl_checkpoints", []))
    requires_review = state.get("requires_review", False)
    review_reason = state.get("review_reason", "")
    if mapping_origin == "proposed" and (quality["unresolved_items"] or quality["average_confidence"] < 0.65):
        requires_review = True
        review_reason = (
            f"Schema mapping needs review: {len(quality['unresolved_items'])} unresolved mandatory targets, "
            f"average confidence {quality['average_confidence']:.1%}"
        )
        hitl_manager = state.get("hitl_manager")
        if hitl_manager:
            checkpoint = hitl_manager.create_checkpoint(
                CheckpointType.COLUMN_DECISION,
                state,
                review_reason,
                title="Column Mapping Review",
                description="Review proposed source-to-target mappings before continuing.",
                trigger_data={
                    "unresolved_items": quality["unresolved_items"],
                    "proposed_mappings": approved_mappings[:25],
                },
                available_actions=["approve", "resolve"],
                recommended_action="resolve",
            )
            hitl_checkpoints.append(checkpoint.to_dict())

    if prepared_df is not None and not prepared_df.empty:
        prepared_df, context_actions = apply_context_to_dataframe(
            prepared_df,
            approved_mappings=approved_mappings,
            business_rules=business_rules,
        )
        rm_inline.append(
            {
                "id": "apply_context_to_dataframe",
                "label": "apply_context_to_dataframe",
                "kind": "deterministic_function",
                "status": "ok",
                "detail": f"{len(context_actions)} context / template action(s)",
            }
        )
    else:
        context_actions = []
        rm_inline.append(
            {
                "id": "apply_context_to_dataframe",
                "label": "apply_context_to_dataframe",
                "kind": "skipped",
                "status": "skipped",
                "detail": "No non-empty dataframe — context application skipped",
            }
        )

    observer = get_observer()
    if observer and observer._active_spans:
        cols_preview = []
        if prepared_df is not None and not prepared_df.empty and hasattr(prepared_df, "columns"):
            cols_preview = [str(c) for c in list(prepared_df.columns)[:48]]
        excluded_preview = [
            str(item.get("source_column") or "")
            for item in approved_mappings
            if mapping_is_excluded(item) and item.get("source_column")
        ]
        rm_meta = {
            "node": "resolve_mapping",
            "langgraph_layer": "per_source",
            "sheet_name": sheet_name,
            "mapping_origin": mapping_origin,
            "mappings_count": len(approved_mappings),
            "excluded_count": len(excluded_preview),
            "excluded_column_preview": excluded_preview[:48],
            "unresolved_targets": len(quality["unresolved_items"]),
            "prepared_column_preview": cols_preview,
        }
        if context_packet.get("job_id"):
            rm_meta["job_id"] = context_packet.get("job_id")
        if source_id:
            rm_meta["source_id"] = source_id
        _set_inline_pipeline_steps(rm_inline)
        observer._active_spans[-1].metadata.update(rm_meta)

    return {
        "target_template": target_template or state.get("target_template"),
        "template_contract": template_contract or {},
        "source_metadata": source_metadata,
        "approved_mappings": approved_mappings,
        "mapping_summary": quality,
        "context_packet": context_packet,
        "scoped_source": resolved_scope or scoped_source,
        "current_df": prepared_df if prepared_df is not None and not prepared_df.empty else state.get("current_df"),
        "current_frame": {
            "rows": len(prepared_df),
            "cols": len(prepared_df.columns),
        } if prepared_df is not None and not prepared_df.empty else state.get("current_frame"),
        "requires_review": requires_review,
        "review_reason": review_reason,
        "hitl_checkpoints": hitl_checkpoints,
        "trace_steps": [{
            "step": "resolve_mapping",
            "mappings_count": len(approved_mappings),
            "confidence": quality["average_confidence"] if approved_mappings else 1.0,
            "unresolved_targets": len(quality["unresolved_items"]),
            "mapping_origin": mapping_origin,
            "actions": context_actions,
        }]
    }


def apply_file_relationship_inference(
    state: AgentState,
    *,
    trace_step_name: str = "infer_relationships",
) -> Dict[str, Any]:
    """
    Propose cross-source file relationships from ``available_source_summaries``.

    Used historically as graph node ``infer_relationships``; now also invokable as the
    optional tool ``discovery.propose_file_relationships`` inside ``execute_tools``.
    """
    context_packet = dict(state.get("context_packet") or {})
    source_summaries = list(context_packet.get("available_source_summaries") or [])
    approved_relationships = list(state.get("approved_relationships") or context_packet.get("file_relationships") or [])

    if len(source_summaries) < 2:
        return {
            "relationship_proposals": [],
            "approved_relationships": approved_relationships,
            "trace_steps": [{
                "step": trace_step_name,
                "relationship_count": 0,
                "status": "skipped",
                "confidence": 1.0,
            }]
        }

    proposals = propose_file_relationships(
        source_summaries,
        target_template=context_packet.get("target_template"),
    )
    context_packet["relationship_proposals"] = proposals
    context_packet["file_relationships"] = approved_relationships

    if approved_relationships:
        return {
            "context_packet": context_packet,
            "relationship_proposals": proposals,
            "approved_relationships": approved_relationships,
            "trace_steps": [{
                "step": trace_step_name,
                "relationship_count": len(approved_relationships),
                "status": "approved",
                "confidence": min((item.get("confidence", 0.8) for item in approved_relationships), default=0.8),
            }]
        }

    defer_graph_hitl = bool(context_packet.get("defer_graph_file_relationship_review"))
    if defer_graph_hitl:
        return {
            "context_packet": context_packet,
            "relationship_proposals": proposals,
            "approved_relationships": approved_relationships,
            "requires_review": False,
            "review_reason": state.get("review_reason", ""),
            "hitl_checkpoints": list(state.get("hitl_checkpoints", [])),
            "hitl_pending_approval": False,
            "hitl_pause_type": None,
            "trace_steps": [{
                "step": trace_step_name,
                "relationship_count": len(proposals),
                "status": "deferred_post_execution",
                "confidence": min((item.get("confidence", 0.7) for item in proposals), default=0.7),
            }],
        }

    requires_review = bool(proposals)
    review_reason = "Multiple sources were uploaded. Review the proposed file relationships before processing continues."
    hitl_checkpoints = list(state.get("hitl_checkpoints", []))
    if requires_review and state.get("hitl_manager"):
        checkpoint = state["hitl_manager"].create_checkpoint(
            CheckpointType.FILE_RELATIONSHIP_REVIEW,
            state,
            review_reason,
            title="File Relationship Review",
            description="Review how the uploaded files should be combined before planning and collation continue.",
            severity="medium",
            available_actions=["approve", "modify", "cancel"],
            recommended_action="approve",
            trigger_data={
                "relationship_proposals": proposals,
                "sources": source_summaries,
            },
        )
        hitl_checkpoints.append(checkpoint.to_dict())

    return {
        "context_packet": context_packet,
        "relationship_proposals": proposals,
        "approved_relationships": approved_relationships,
        "requires_review": requires_review,
        "review_reason": review_reason if requires_review else state.get("review_reason", ""),
        "hitl_checkpoints": hitl_checkpoints,
        "hitl_pending_approval": requires_review,
        "hitl_pause_type": "file_relationship_review" if requires_review else None,
        "trace_steps": [{
            "step": trace_step_name,
            "relationship_count": len(proposals),
            "status": "pending_review" if requires_review else "ready",
            "confidence": min((item.get("confidence", 0.7) for item in proposals), default=0.7),
        }]
    }


@trace_node("infer_relationships", input_keys=["context_packet", "approved_relationships"])
def infer_relationships_node(state: AgentState) -> Dict[str, Any]:
    """Backward-compatible wrapper; graph no longer calls this node by default."""
    return apply_file_relationship_inference(state, trace_step_name="infer_relationships")


@trace_node("analyze_structure")
def analyze_structure_node(state: AgentState) -> Dict[str, Any]:
    """LLM analyzes spreadsheet structure."""
    
    if not state.get("llm_client"):
        _set_inline_pipeline_steps(
            [
                {
                    "id": "analyze_structure",
                    "label": "analyze_structure",
                    "kind": "skipped",
                    "status": "error",
                    "detail": "LLM client missing — structure LLM skipped",
                }
            ]
        )
        return {
            "errors": ["LLM client missing - cannot perform structure analysis"],
            "trace_steps": [{
                "step": "analyze_structure", 
                "error": "LLM client missing",
                "message": "AI analysis skipped: No active LLM client"
            }]
        }

    grid = state.get("grid")
    if not grid:
        _set_inline_pipeline_steps(
            [
                {
                    "id": "analyze_structure",
                    "label": "analyze_structure",
                    "kind": "skipped",
                    "status": "error",
                    "detail": "Grid missing from state — cannot summarize or analyze",
                }
            ]
        )
        return {
            "errors": ["Grid object missing - cannot perform structure analysis"],
            "trace_steps": [{
                "step": "analyze_structure", 
                "error": "Grid missing",
                "message": "AI analysis failed: Grid object not found in state"
            }]
        }

    try:
        # Bounded summary only — never embed the full sheet for the structure LLM.
        max_cols_llm = min(56, max(1, int(grid.total_cols)))
        grid_summary = grid.to_smart_summary(
            max_cols=max_cols_llm,
            header_rows=min(5, max(1, grid.total_rows)),
            samples_per_anchor=2,
            max_anchor_rows=160,
            max_sparse_column_lines=40,
            max_cell_chars=20,
        )
        inline_steps: List[Dict[str, Any]] = [
            {
                "id": "grid.to_smart_summary",
                "label": "Bounded grid summary (VisualGrid.to_smart_summary)",
                "kind": "deterministic_function",
                "status": "ok",
                "detail": f"Budget ≤{max_cols_llm} cols, header sample ≤{min(5, max(1, grid.total_rows))} row(s)",
            },
        ]
        scoped_source = state.get("scoped_source", {}) or {}
        main_blocks = scoped_source.get("main_blocks", []) or []
        ctx_blocks = scoped_source.get("context_blocks", []) or []
        from sia.agent.metric_layout_signals import (
            detect_metric_layout_from_grid,
            enrich_structure_analysis_with_metric_signals,
        )

        metric_layout_signals = detect_metric_layout_from_grid(grid)
        inline_steps.append(
            {
                "id": "detect_metric_layout_from_grid",
                "label": "Block-sparse metric layout scan",
                "kind": "deterministic_function",
                "status": "ok",
                "detail": (
                    f"grouped_rows_likely={metric_layout_signals.get('grouped_rows_likely')}; "
                    f"block_metrics={len(metric_layout_signals.get('block_sparse_metrics') or [])}"
                ),
            }
        )
        visual_patterns_llm = {
            "merged_cells": len(getattr(grid, "merged_regions", getattr(grid, "merged_ranges", []))),
            "rows": grid.total_rows,
            "cols": grid.total_cols,
            "physical_boundaries": state.get("source_frame"),
            "sheet_frame": scoped_source.get("sheet_frame"),
            "scope_type": scoped_source.get("scope_type", "full_sheet"),
            "approved_blocks_count": len(main_blocks),
            "approved_blocks_preview": _compact_blocks_for_llm(main_blocks),
            "context_blocks_count": len(ctx_blocks),
            "context_blocks_preview": _compact_blocks_for_llm(ctx_blocks),
            "metric_layout_signals": metric_layout_signals,
        }
        visual_patterns_full = {
            **visual_patterns_llm,
            "approved_blocks": main_blocks,
            "context_blocks": ctx_blocks,
        }
        if metric_layout_signals.get("notes"):
            grid_summary = (
                grid_summary
                + "\n\n--- DETERMINISTIC METRIC LAYOUT SCAN ---\n"
                + "\n".join(f"- {n}" for n in metric_layout_signals["notes"])
            )
            bsm = metric_layout_signals.get("block_sparse_metrics") or []
            if bsm:
                grid_summary += "\nBlock-sparse metrics (parent row only → needs allocation):\n"
                for m in bsm[:6]:
                    grid_summary += (
                        f"  - col {m.get('column_index')}: {m.get('column_label')!r} "
                        f"(partial_segments={m.get('partial_segment_rate')}, mean_fill={m.get('mean_segment_fill_rate')})\n"
                    )
        
        rows = int(getattr(grid, "total_rows", 0) or 0)
        cols = int(getattr(grid, "total_cols", 0) or 0)
        analyzer = StructureAnalyzer(state.get("llm_client"))
        dim = f"{rows:,}×{cols:,}" if rows and cols else "this sheet"
        _emit_job_progress(
            state,
            "Structure analyzer",
            f"Starting LLM structure analysis for {dim}. Large sheets or slow models may take several minutes.",
        )

        # Capture prompts for trace
        observer = get_observer()
        if observer:
            span = observer._active_spans[-1] if observer._active_spans else None
            if span:
                span.system_prompt = _load_system_prompt("structure_analyzer")
                span.user_prompt = f"Analyze spreadsheet with patterns: {json_safe_dumps(visual_patterns_llm, indent=2)}"

        analysis = analyzer.analyze(grid_summary, visual_patterns_llm)
        analysis = enrich_structure_analysis_with_metric_signals(
            analysis,
            metric_layout_signals,
            grid_cols=cols,
        )
        _emit_job_progress(
            state,
            "Structure analyzer",
            "Structure analysis finished; running consistency scan on the workbook…",
        )
        analysis["visual_patterns"] = visual_patterns_full
        if scoped_source:
            analysis["scoped_source"] = scoped_source

        tbl_n = len(analysis.get("tables", []))
        conf = float(analysis.get("confidence", 0) or 0.0)
        inline_steps.append(
            {
                "id": "StructureAnalyzer.analyze",
                "label": "StructureAnalyzer.analyze",
                "kind": "llm",
                "status": "ok",
                "detail": f"{tbl_n} table(s); confidence={conf:.0%}",
            }
        )
    except Exception as e:
        logger.error(f"Structure analysis failed: {e}")
        _set_inline_pipeline_steps(
            [
                {
                    "id": "analyze_structure.pipeline",
                    "label": "Structure analysis (summary + StructureAnalyzer)",
                    "kind": "llm",
                    "status": "error",
                    "detail": str(e)[:320],
                }
            ]
        )
        return {
            "errors": [f"Analysis failed: {str(e)}"],
            "trace_steps": [{
                "step": "analyze_structure", 
                "error": str(e),
                "message": f"AI analysis failed: {e}"
            }]
        }
    
    # If LLM returned empty tables, we still consider this a valid (though poor) result
    # but we will flag it if confidence is low
    
    # ===== Run deterministic structural scan for HITL review =====
    structure_report = {}
    hitl_checkpoints = list(state.get("hitl_checkpoints", []))
    requires_review = state.get("requires_review", False)
    review_reason = state.get("review_reason", "")
    
    file_path = state.get("file_path", "")
    scan_detail = ""
    scan_status = "skipped"
    if file_path:
        try:
            from ..tools.transformation_tools import TransformationTools
            scan_result = TransformationTools.inspect_sheet_structure(
                file_path,
                sheet_name=state.get("sheet_name"),
            )
            if scan_result.success:
                structure_report = scan_result.data or {}
                if state.get("scoped_source"):
                    structure_report["scoped_source"] = state.get("scoped_source")
                logger.info(f"Structural scan: noise={structure_report.get('noise_score', 0):.2f}, "
                           f"density={structure_report.get('density', 0):.2f}")
                scan_status = "ok"
                scan_detail = (
                    f"noise={float(structure_report.get('noise_score', 0) or 0):.2f}, "
                    f"density={float(structure_report.get('density', 0) or 0):.2f}, "
                    f"header_candidates={len(structure_report.get('header_candidates') or [])}"
                )
            else:
                scan_status = "error"
                scan_detail = str(getattr(scan_result, "message", "") or "inspect_sheet_structure failed")[:240]
        except Exception as e:
            logger.warning(f"Structural scan failed (non-fatal): {e}")
            scan_status = "error"
            scan_detail = str(e)[:240]
    else:
        scan_detail = "No file_path on state — workbook scan skipped"

    inline_steps.append(
        {
            "id": "TransformationTools.inspect_sheet_structure",
            "label": "Sheet structure scan (parity with discovery.inspect tool)",
            "kind": "deterministic_function",
            "status": scan_status,
            "detail": scan_detail,
        }
    )
    
    # Check for HITL structural review checkpoint
    hitl_manager = state.get("hitl_manager")
    if hitl_manager and structure_report:
        # Build a temporary state dict for should_checkpoint
        check_state = {**state, "structure_report": structure_report}
        checkpoint = hitl_manager.should_checkpoint(check_state, "analyze_structure")
        if checkpoint:
            hitl_checkpoints.append(checkpoint.to_dict())
            requires_review = True
            review_reason = checkpoint.trigger_reason
            logger.info(f"HITL structural checkpoint created: {checkpoint.checkpoint_id}")
    
    trace_steps = [{
        "step": "analyze_structure",
        "sheet_name": state.get("sheet_name"),
        "tables_found": len(analysis.get("tables", [])),
        "confidence": analysis.get("confidence", 0),
        "noise_score": structure_report.get("noise_score", 0.0),
        "main_blocks": len((state.get("scoped_source") or {}).get("main_blocks") or []),
        "context_blocks": len((state.get("scoped_source") or {}).get("context_blocks") or []),
        "header_candidate_count": len(structure_report.get("header_candidates") or []),
        "data_region": bool(structure_report.get("data_region")),
    }]
    observer = get_observer()
    if observer and observer._active_spans:
        scoped = state.get("scoped_source") or {}
        cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
        sm = state.get("source_metadata") if isinstance(state.get("source_metadata"), dict) else {}
        span_meta = {
            "node": "analyze_structure",
            "langgraph_layer": "per_source",
            "sheet_name": state.get("sheet_name"),
            "tables_found": len(analysis.get("tables", [])),
            "main_blocks": len(scoped.get("main_blocks") or []),
            "context_blocks": len(scoped.get("context_blocks") or []),
            "noise_score": structure_report.get("noise_score"),
            "density": structure_report.get("density"),
            "header_candidate_count": len(structure_report.get("header_candidates") or []),
        }
        if cp.get("job_id"):
            span_meta["job_id"] = cp.get("job_id")
        if sm.get("source_id"):
            span_meta["source_id"] = sm.get("source_id")
        _set_inline_pipeline_steps(inline_steps)
        observer._active_spans[-1].metadata.update(span_meta)

    _persist_transformation_banner_inputs(
        state,
        structure_report=structure_report,
        structure_analysis=analysis,
        plan_tool_calls=None,
    )
    _record_pipeline_structure_eval(state, analysis)

    return {
        "structure_analysis": analysis,
        "structure_report": structure_report,
        "requires_review": requires_review,
        "review_reason": review_reason,
        "hitl_checkpoints": hitl_checkpoints,
        "message": f"Found {len(analysis.get('tables', []))} tables with {analysis.get('confidence', 0):.1%} confidence",
        "trace_steps": trace_steps,
    }

@trace_node("generate_plan", input_keys=["structure_analysis"])
def generate_plan_node(state: AgentState) -> Dict[str, Any]:
    """LLM generates extraction plan with HITL checkpoint check."""
    from sia.integrity.context_isolation import plan_bound_to_wrong_source

    if state.get("resume_mode") == "use_existing_plan" and state.get("extraction_plan"):
        if plan_bound_to_wrong_source(state, state.get("context_packet")):
            logger.warning(
                "generate_plan_node: persisted plan targets a different source_id; forcing replan"
            )
            state = dict(state)
            state.pop("extraction_plan", None)
            state.pop("suggested_tools", None)
            state["resume_mode"] = "replan"
        else:
            existing_plan = state.get("extraction_plan")
            if isinstance(existing_plan, dict):
                existing_plan = ExtractionPlan(**existing_plan)
            from sia.agent.planner import finalize_extraction_plan

            finalized = finalize_extraction_plan(
                existing_plan,
                state.get("context_packet"),
                state.get("target_template"),
                state.get("structure_analysis"),
            )
            if isinstance(finalized, ExtractionPlan):
                existing_plan = finalized
            observer_early = get_observer()
            if observer_early and getattr(observer_early, "_active_spans", None):
                observer_early.update_span_metadata(
                    "plan_generator_llm",
                    "skipped — resume_mode=use_existing_plan (reusing persisted extraction_plan; use Regenerate plan to call the planner LLM)",
                )
            _persist_transformation_banner_inputs(state, plan_tool_calls=list(existing_plan.tool_calls or []))
            _record_pipeline_plan_eval(state, existing_plan, approval_items=[])
            return {
                "extraction_plan": existing_plan,
                "suggested_tools": existing_plan.tool_calls,
                "expected_columns": existing_plan.expected_columns,
                "planned_rule_actions": existing_plan.business_rule_actions,
                "approval_items": [],
                "hitl_checkpoints": [],
                "requires_review": False,
                "review_reason": "",
                "hitl_pending_approval": False,
                "hitl_pause_type": None,
                "trace_steps": [{
                    "step": "generate_plan",
                    "tools_count": len(existing_plan.tool_calls),
                    "confidence": existing_plan.confidence,
                    "reused_existing_plan": True,
                }]
            }
    
    if not state.get("llm_client"):
        # Fallback plan for rule-based mode
        observer_fallback = get_observer()
        if observer_fallback and getattr(observer_fallback, "_active_spans", None):
            observer_fallback.update_span_metadata(
                "plan_generator_llm",
                "skipped — no llm_client (minimal empty plan)",
            )
        _persist_transformation_banner_inputs(state, plan_tool_calls=[])
        return {
            "extraction_plan": ExtractionPlan(tool_calls=[], confidence=0.5, reasoning="Rule-based fallback"),
            "errors": ["LLM client required for plan generation - using minimal fallback"],
            "trace_steps": [{"step": "generate_plan", "tools_count": 0, "confidence": 0.5}]
        }
    
    plan_generator = PlanGenerator(state["llm_client"])
    
    # Capture prompts for trace
    observer = get_observer()
    if observer:
        span = observer._active_spans[-1] if observer._active_spans else None
        if span:
            span.system_prompt = _load_system_prompt("plan_generator")
            # Injecting physical context for coordinate stability
            source_frame = state.get("source_frame", {})
            current_df = state.get("current_df")
            # Robust shape extraction (current_df might be a list or None in early stages)
            current_shape = None
            if current_df is not None and hasattr(current_df, 'columns') and hasattr(current_df, 'index'):
                 current_shape = {"rows": len(current_df), "cols": len(current_df.columns)}
            
            context_injection = {
                "source_frame": source_frame,
                "current_frame": current_shape,
                "reasoning_mode": "stability_mandate"
            }
            span.user_prompt = (
                f"Context: {json_safe_dumps(context_injection)}\n"
                f"Structure: {json_safe_dumps(state['structure_analysis'], indent=2)}"
            )
            
    # Note: RAG examples would be fetched here in full implementation
    examples = [] 
    
    # Load target template for enrichment planning
    target_template = state.get("target_template")
    if not target_template:
        target_template = _load_target_template()
    
    context_packet = dict(state.get("context_packet") or {})
    date_obs_pkg: Dict[str, Any] = {}
    if context_packet:
        date_obs_pkg = build_date_column_observations_for_planning(
            state.get("current_df"),
            list(context_packet.get("approved_mappings") or []),
            target_template or {},
            guided_date_granularity=str(
                (dict(context_packet.get("source_metadata") or {}).get("date_granularity") or "")
            ),
        )
        planning_summary = _ensure_canonical_planning_summary(
            context_packet,
            current_df=state.get("current_df"),
            scoped_source=state.get("scoped_source") or {},
            date_observations=date_obs_pkg,
        )
        context_packet["planning_summary"] = planning_summary

    if _should_defer_weekly_rollup_until_post_collate(state):
        context_packet["defer_weekly_rollups_to_post_union"] = True

    _present_cols: List[str] = []
    _cdf = state.get("current_df")
    if _cdf is not None and hasattr(_cdf, "columns"):
        _present_cols = [str(c) for c in _cdf.columns]
    template_contract, column_gaps, context_packet = _sync_template_contract(
        target_template,
        context_packet,
        present_columns=_present_cols,
        approved_mappings=list(context_packet.get("approved_mappings") or []),
    )

    _emit_job_progress(
        state,
        "Plan generator",
        "Building the transformation plan with the AI (tool sequence and checks). This can take a few minutes…",
    )

    plan = plan_generator.generate(
        state["structure_analysis"],
        examples,
        target_template=target_template,
        context_packet=context_packet or None,
    )

    date_review_items = build_date_inference_approval_items(date_obs_pkg)
    if date_review_items:
        plan.approval_items = list(plan.approval_items or []) + date_review_items

    # Update observer metadata for UI
    observer = get_observer()
    if observer:
        observer.update_span_metadata("tool_sequence", [
            {
                "tool": t.get("tool") or "invalid_tool",
                "params": t.get("params") or {},
                "reason": t.get("description") or t.get("reason") or "No reason provided"
            }
            for t in plan.tool_calls if isinstance(t, dict)
        ])
    
    # Track confidence trajectory
    confidence_trajectory = list(state.get("confidence_trajectory", []))
    confidence_trajectory.append(plan.confidence)
    
    # Check for HITL checkpoint trigger
    hitl_checkpoints = list(state.get("hitl_checkpoints", []))
    requires_review = plan.requires_human_review
    review_reason = plan.review_reason
    
    low_confidence_plan = plan.confidence < 0.7
    if low_confidence_plan:
        requires_review = True
        review_reason = f"Plan confidence ({plan.confidence:.1%}) is below threshold (70%)"

    if plan.approval_items:
        requires_review = True
        if not review_reason:
            review_reason = f"Plan has {len(plan.approval_items)} approval items that need analyst review"

    if requires_review and review_reason:
        hitl_manager = state.get("hitl_manager")
        if hitl_manager:
            sheet_name = (
                state.get("sheet_name")
                or (state.get("scoped_source") or {}).get("sheet_name")
                or ((state.get("context_packet") or {}).get("source_metadata") or {}).get("sheet_name")
            )
            has_approvals = bool(plan.approval_items)
            title = "Plan Assumptions Review" if has_approvals and not low_confidence_plan else "Plan Review Required"
            if has_approvals and low_confidence_plan:
                title = "Plan Review Required"
            description_parts = [
                f"AI generated plan with {len(plan.tool_calls)} tools at {plan.confidence:.1%} confidence."
            ]
            if has_approvals:
                description_parts.append(
                    f"{len(plan.approval_items)} approval item(s) and "
                    f"{len(plan.business_rule_actions)} planned business-rule action(s) need review."
                )
            checkpoint = hitl_manager.create_checkpoint(
                CheckpointType.PLAN_REVIEW,
                state,
                review_reason,
                title=title,
                description=" ".join(description_parts),
                severity="medium" if plan.confidence > 0.5 else "high",
                available_actions=["approve", "modify", "regenerate", "cancel"],
                recommended_action="modify" if has_approvals else ("approve" if plan.confidence > 0.6 else "modify"),
                confidence=float(plan.confidence),
                trigger_data={
                    "plan_confidence": float(plan.confidence),
                    "sheet_name": sheet_name,
                    "approval_items": plan.approval_items,
                    "planned_rule_actions": plan.business_rule_actions,
                    "reasoning": plan.reasoning[:1200] if plan.reasoning else "",
                    "tool_calls": plan.tool_calls,
                    "expected_columns": plan.expected_columns,
                },
            )
            hitl_checkpoints.append(checkpoint.to_dict())
            _record_pipeline_plan_review_eval(state, plan)

    should_pause_for_plan_review = bool(hitl_checkpoints) and requires_review

    _persist_transformation_banner_inputs(state, plan_tool_calls=list(plan.tool_calls or []))
    _record_pipeline_plan_eval(state, plan)

    return {
        "extraction_plan": plan,
        "last_replan_tools": list(plan.tool_calls or []),
        "template_contract": template_contract or {},
        "context_packet": context_packet,
        "suggested_tools": plan.tool_calls,
        "expected_columns": plan.expected_columns,
        "planned_rule_actions": plan.business_rule_actions,
        "approval_items": plan.approval_items,
        "confidence_trajectory": confidence_trajectory,
        "requires_review": requires_review,
        "review_reason": review_reason,
        "hitl_checkpoints": hitl_checkpoints,
        "hitl_pending_approval": should_pause_for_plan_review,
        "hitl_pause_type": "plan_review" if should_pause_for_plan_review else None,
        "message": f"Plan: {len(plan.tool_calls)} tools, {len(plan.expected_columns)} target columns",
        "trace_steps": [{
            "step": "generate_plan",
            "tools_count": len(plan.tool_calls),
            "confidence": plan.confidence,
            "rule_actions_count": len(plan.business_rule_actions),
            "approval_items_count": len(plan.approval_items),
        }]
    }


def _squeeze_consecutive_same_tool(
    tool_calls: Optional[List[Any]],
    tool_name: str,
) -> List[Any]:
    """Collapse consecutive duplicate tool entries, keeping the last (e.g. repeated verify.schema)."""
    if not tool_calls:
        return []
    out: List[Any] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        if tc.get("tool") == tool_name and out and isinstance(out[-1], dict) and out[-1].get("tool") == tool_name:
            out[-1] = tc
            continue
        out.append(tc)
    return out


def _last_column_typing_tool_index(tool_calls: Optional[List[Any]]) -> Optional[int]:
    """Index of the final rename/type/add/rule operation in the sorted plan."""
    last_index: Optional[int] = None
    for index, tool_call in enumerate(tool_calls or []):
        if not isinstance(tool_call, dict):
            continue
        normalized, _ = normalize_tool_name(str(tool_call.get("tool") or "").strip())
        if pipeline_catalog.stage_for_tool(normalized) == pipeline_catalog.PipelineStage.COLUMN_TYPING:
            last_index = index
    return last_index


_DAILY_PREP_BEFORE_WEEKLY = frozenset(
    {
        "transform.expand_period_to_daily",
        "transform.infer_granularity_expand_to_daily",
        "transform.date_range_to_weekly",
        "transform.expand_date_range_to_daily",
        "transform.expand_date_range_to_weekly",
    }
)

# Grain/metric-changing tools deferred until after multi-source collation duplicate_check.
POST_COLLATE_DEFERRED_GRAIN_TOOLS = _DAILY_PREP_BEFORE_WEEKLY | frozenset(
    {"transform.aggregate_weekly"}
)


def _period_span_date_shape_from_state(state: AgentState) -> bool:
    sm = dict(state.get("source_metadata") or {})
    raw = str(sm.get("date_shape") or "").strip().lower()
    return raw in ("period_span", "range_pair", "range", "two_column_span")


def _resolve_inferred_date_range_pair_for_execution(state: AgentState) -> Optional[Dict[str, Any]]:
    """Prefer planning-time pair; re-infer from the active frame if missing."""
    cp = dict(state.get("context_packet") or {})
    ps = cp.get("planning_summary") or {}
    ss = ps.get("source_summary") or {}
    pair = ss.get("inferred_date_range_pair") if isinstance(ss, dict) else None
    if isinstance(pair, dict) and pair.get("start_date_col") and pair.get("end_date_col"):
        return pair
    df = state.get("current_df")
    if df is None or not hasattr(df, "columns"):
        return None
    mapped = infer_date_range_column_pair(
        df,
        list(cp.get("approved_mappings") or state.get("approved_mappings") or []),
        state.get("target_template") or cp.get("target_template") or {},
    )
    if mapped:
        return mapped
    return infer_date_range_pair_heuristic(df)


def _inject_infer_daily_before_aggregate_weekly(
    tools_to_run: List[Any],
    state: AgentState,
) -> List[Any]:
    """
    If the plan runs aggregate_weekly without any prior expand/infer/date_range tool, insert
    either ``infer_granularity_expand_to_daily`` (single period column) or, when a **start/end**
    column pair is inferred, ``date_range_to_weekly`` with ``granularity='daily'`` so metrics are
    prorated across each day in the inclusive range before weekly rollup.
    """
    if not tools_to_run:
        return tools_to_run

    tt = state.get("target_template") or {}
    xs = tt.get("x_scope") if isinstance(tt, dict) else None
    tgt = str(effective_target_date_granularity(xs) if isinstance(xs, dict) else "").strip().lower()
    if tgt in ("daily", "day", "date"):
        return tools_to_run

    range_pair = _resolve_inferred_date_range_pair_for_execution(state)

    out: List[Any] = []
    for tc in tools_to_run:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        name = str(tc.get("tool") or "").strip()
        if name == "transform.aggregate_weekly":
            already = any(
                isinstance(t, dict) and t.get("tool") in _DAILY_PREP_BEFORE_WEEKLY for t in out
            )
            if not already:
                p = dict(tc.get("params") or {})
                date_col = p.get("date_col")
                metric_rules = p.get("metric_rules")
                value_cols: Optional[List[Any]] = None
                if isinstance(metric_rules, dict) and metric_rules:
                    value_cols = list(metric_rules.keys())
                else:
                    vc = p.get("value_cols")
                    if isinstance(vc, list) and vc:
                        value_cols = list(vc)
                gb = p.get("group_by_cols")
                use_range_expand = (
                    isinstance(range_pair, dict)
                    and float(range_pair.get("confidence") or 0) >= 0.55
                    and range_pair.get("start_date_col")
                    and range_pair.get("end_date_col")
                    and value_cols
                )
                if use_range_expand:
                    span_tool = (
                        "transform.expand_date_range_to_daily"
                        if _period_span_date_shape_from_state(state)
                        else "transform.date_range_to_weekly"
                    )
                    range_params: Dict[str, Any] = {
                        "start_date_col": range_pair["start_date_col"],
                        "end_date_col": range_pair["end_date_col"],
                        "value_cols": value_cols,
                        "id_cols": gb if isinstance(gb, list) else None,
                        "date_column": "calendar_date",
                    }
                    if span_tool == "transform.date_range_to_weekly":
                        range_params["granularity"] = "daily"
                    range_tc: Dict[str, Any] = {
                        "tool": span_tool,
                        "params": range_params,
                        "_valid": True,
                    }
                    out.append(range_tc)
                    tc = dict(tc)
                    p2 = dict(tc.get("params") or {})
                    p2["date_col"] = "calendar_date"
                    tc["params"] = p2
                    logger.info(
                        "Inserted %s before aggregate_weekly based on inferred start/end columns %s / %s.",
                        span_tool,
                        range_pair.get("start_date_col"),
                        range_pair.get("end_date_col"),
                    )
                elif date_col and value_cols:
                    infer_tc: Dict[str, Any] = {
                        "tool": "transform.infer_granularity_expand_to_daily",
                        "params": {
                            "date_col": date_col,
                            "value_cols": value_cols,
                            "id_cols": gb if isinstance(gb, list) else None,
                            "date_column": "calendar_date",
                            "min_confidence": 0.35,
                        },
                        "_valid": True,
                    }
                    out.append(infer_tc)
                    tc = dict(tc)
                    p2 = dict(tc.get("params") or {})
                    p2["date_col"] = "calendar_date"
                    tc["params"] = p2
                    logger.info(
                        "Inserted transform.infer_granularity_expand_to_daily before aggregate_weekly "
                        "(no prior expand/infer/date_range_to_weekly in plan)."
                    )
            out.append(tc)
        else:
            out.append(tc)
    return out


def _should_defer_weekly_rollup_until_post_collate(state: AgentState) -> bool:
    """Defer grain-changing transforms until after multi-source collation duplicate_check.

    Any batch with 2+ sources is collated and scanned for duplicate keys on the stacked frame.
    Running weekly rollup or daily expansion per source first can collapse or rewrite metrics
    before those keys are evaluated.
    """
    if bool(state.get("multi_source_active_batch")) or bool(state.get("multi_block_active_batch")):
        return True
    if isinstance(state.get("context_packet"), dict) and bool(
        state.get("context_packet", {}).get("process_blocks_separately")
    ):
        return True
    context_packet = state.get("context_packet") or {}
    if isinstance(context_packet, dict) and bool(
        context_packet.get("defer_weekly_rollups_to_post_union")
    ):
        return True
    return False


def _partition_deferred_post_collate_grain_tools(
    tools_to_run: List[Any],
    state: AgentState,
) -> tuple[List[Any], List[Dict[str, Any]]]:
    if not _should_defer_weekly_rollup_until_post_collate(state):
        return tools_to_run, []
    out: List[Any] = []
    deferred: List[Dict[str, Any]] = []
    for tc in tools_to_run:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        norm, _ = normalize_tool_name(str(tc.get("tool") or "").strip())
        if norm in POST_COLLATE_DEFERRED_GRAIN_TOOLS:
            deferred.append(dict(tc))
        else:
            out.append(tc)
    return out, deferred


@trace_node("execute_tools", input_keys=["suggested_tools"])
def execute_tools_node(state: AgentState) -> Dict[str, Any]:
    """
    Execute transformation tools with validation and checkpointing.
    
    Enhancements:
    - Validates tool calls before execution
    - Creates checkpoint before destructive tools
    - Tracks tool execution history
    - Supports rollback on failure
    - **NEW**: Pauses for HITL approval before destructive operations
    """
    grid = state["grid"]
    current_df = state["current_df"]

    tools_to_run = _squeeze_consecutive_same_tool(list(state.get("suggested_tools") or []), "verify.schema")
    tools_to_run, dedupe_notes = dedupe_redundant_tool_calls(tools_to_run)
    if dedupe_notes:
        logger.info(
            "execute_tools: dropped %s redundant tool call(s): %s",
            len(dedupe_notes),
            "; ".join(dedupe_notes[:8]),
        )
    from sia.integrity.context_isolation import rebind_source_local_plan_literals

    tools_to_run, rebind_notes = rebind_source_local_plan_literals(
        tools_to_run,
        state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {},
        job=_pipeline_eval_job(state)[0],
    )
    for note in rebind_notes[:4]:
        logger.info("execute_tools rebind: %s", note)

    from sia.integrity.context_isolation import (
        context_packet_source_id,
        plan_bound_to_wrong_source,
        plan_source_id,
    )

    cp = state.get("context_packet") if isinstance(state.get("context_packet"), dict) else {}
    active_sid = str(state.get("source_id") or context_packet_source_id(cp) or "").strip()
    bound_plan_sid = plan_source_id(state.get("extraction_plan"))
    if active_sid and bound_plan_sid and bound_plan_sid != active_sid:
        reason = (
            f"Execution plan is bound to source_id {bound_plan_sid!r} but active sheet is "
            f"{active_sid!r}. Regenerate the plan for this source before executing tools."
        )
        logger.error(reason)
        hitl_manager = state.get("hitl_manager")
        if hitl_manager:
            checkpoint = hitl_manager.create_checkpoint(
                CheckpointType.PLAN_REVIEW,
                state,
                reason,
                title="Wrong-source plan blocked",
                description=reason,
                severity="high",
                available_actions=["regenerate", "modify", "cancel"],
                recommended_action="regenerate",
                trigger_data={
                    "reasoning": reason,
                    "plan_source_id": bound_plan_sid,
                    "active_source_id": active_sid,
                },
            )
            hitl_checkpoints = list(state.get("hitl_checkpoints", []))
            hitl_checkpoints.append(checkpoint.to_dict())
        return {
            "requires_review": True,
            "review_reason": reason,
            "hitl_pending_approval": True,
            "hitl_pause_type": "plan_review",
            "hitl_checkpoints": hitl_checkpoints if hitl_manager else list(state.get("hitl_checkpoints", [])),
            "trace_steps": [{
                "step": "hitl_pause",
                "reason": reason,
                "plan_source_id": bound_plan_sid,
                "active_source_id": active_sid,
            }],
        }
    if plan_bound_to_wrong_source(dict(state), cp):
        reason = (
            "Persisted plan or context packet targets a different source than the active sheet. "
            "Regenerate the plan before executing tools."
        )
        logger.error(reason)
        hitl_manager = state.get("hitl_manager")
        if hitl_manager:
            checkpoint = hitl_manager.create_checkpoint(
                CheckpointType.PLAN_REVIEW,
                state,
                reason,
                title="Wrong-source memory blocked",
                description=reason,
                severity="high",
                available_actions=["regenerate", "cancel"],
                recommended_action="regenerate",
            )
            hitl_checkpoints = list(state.get("hitl_checkpoints", []))
            hitl_checkpoints.append(checkpoint.to_dict())
        return {
            "requires_review": True,
            "review_reason": reason,
            "hitl_pending_approval": True,
            "hitl_pause_type": "plan_review",
            "hitl_checkpoints": hitl_checkpoints if hitl_manager else list(state.get("hitl_checkpoints", [])),
            "trace_steps": [{"step": "hitl_pause", "reason": reason}],
        }

    job, _pe_runner = _pipeline_eval_job(state)
    from sia.context.verifier import ContextVerifier

    ctx_verify = ContextVerifier.verify_plan_context(
        state.get("extraction_plan"),
        cp,
        job=job,
        tool_calls=tools_to_run,
        resume_state=state,
        target_template=state.get("target_template"),
    )
    if not ctx_verify.pass_:
        critical = ctx_verify.critical_violations()
        if critical:
            reason = (
                "Context verifier blocked execution: "
                + "; ".join(str(v.get("message") or v.get("type")) for v in critical[:3])
            )
            logger.error(reason)
            hitl_manager = state.get("hitl_manager")
            hitl_checkpoints = list(state.get("hitl_checkpoints", []))
            if hitl_manager:
                checkpoint = hitl_manager.create_checkpoint(
                    CheckpointType.PLAN_REVIEW,
                    state,
                    reason,
                    title="Context bleed blocked",
                    description=(
                        "Plan literals or source binding disagree with scoped local context. "
                        "Review or regenerate the plan before executing tools."
                    ),
                    severity="high",
                    available_actions=["regenerate", "modify", "cancel"],
                    recommended_action="regenerate",
                    trigger_data={
                        "reasoning": reason,
                        "context_violations": critical[:8],
                    },
                )
                hitl_checkpoints.append(checkpoint.to_dict())
            return {
                "requires_review": True,
                "review_reason": reason,
                "hitl_pending_approval": True,
                "hitl_pause_type": "plan_review",
                "hitl_checkpoints": hitl_checkpoints,
                "suggested_tools": tools_to_run,
                "trace_steps": [{
                    "step": "hitl_pause",
                    "reason": reason,
                    "context_gate": "pre_execute",
                }],
            }

    deferred_post_collate: List[Dict[str, Any]] = []
    scoped_source = state.get("scoped_source", {}) or {}
    consumed_fast_resume = bool(state.get("resume_skip_pipeline_after_load"))
    malformed_tools = [t for t in (tools_to_run or []) if not isinstance(t, dict) or t.get("_valid") is False]
    hitl_checkpoints = list(state.get("hitl_checkpoints", []))
    # region agent log
    _debug_log(
        "H1_H2_H3",
        "execute_tools_node entry",
        {
            "job_id": state.get("job_id"),
            "tools_count": len(tools_to_run or []),
            "tool_names": [t.get("tool") if isinstance(t, dict) else str(type(t)) for t in (tools_to_run or [])],
            "tool_valid_flags": [None if not isinstance(t, dict) else t.get("_valid") for t in (tools_to_run or [])],
            "current_df_is_none": current_df is None,
            "grid_is_none": grid is None,
            "requires_extraction": bool(scoped_source.get("requires_extraction")),
        },
    )
    # endregion
    
    logger.info(f"Execute Tools: {len(tools_to_run) if tools_to_run else 0} tools to run. Current DF: {current_df is not None}. Grid: {grid is not None}")

    if malformed_tools:
        reason = "Plan contains malformed or invalid tool entries; pausing for review instead of executing raw sheet fallback."
        logger.error(reason)
        # region agent log
        _debug_log(
            "H1",
            "malformed tools plan-review pause",
            {
                "job_id": state.get("job_id"),
                "malformed_tools": [
                    t if not isinstance(t, dict) else {
                        "tool": t.get("tool"),
                        "_valid": t.get("_valid"),
                        "params_keys": sorted(list((t.get("params") or {}).keys())),
                    }
                    for t in malformed_tools
                ],
            },
        )
        # endregion
        hitl_manager = state.get("hitl_manager")
        if hitl_manager:
            checkpoint = hitl_manager.create_checkpoint(
                CheckpointType.PLAN_REVIEW,
                state,
                reason,
                title="Execution Plan Review Required",
                description=(
                    "The current execution plan includes malformed or invalid tool entries. "
                    "Review or regenerate the plan before continuing."
                ),
                severity="high",
                available_actions=["approve", "modify", "regenerate", "cancel"],
                recommended_action="modify",
                trigger_data={
                    "reasoning": reason,
                    "tool_calls": [t for t in (tools_to_run or []) if isinstance(t, dict)],
                    "expected_columns": list(state.get("expected_columns", []) or []),
                }
            )
            hitl_checkpoints.append(checkpoint.to_dict())
        return {
            "requires_review": True,
            "review_reason": reason,
            "hitl_pending_approval": True,
            "hitl_pause_type": "plan_review",
            "hitl_checkpoints": hitl_checkpoints,
            "trace_steps": [{
                "step": "hitl_pause",
                "reason": reason,
                "tools": [t.get("tool") if isinstance(t, dict) else str(t) for t in malformed_tools]
            }]
        }

    extraction_tools = {
        "layout.extract",
        "layout.stack",
        "layout.unpivot_matrix",
        "extract_data_block",
        "stack_tables",
        "crosstab_unpivot",
        "xls.layout.extract",
        "xls.layout.stack",
        "xls.reshape.unpivot_matrix",
    }
    requested_extraction = any(
        isinstance(tool_call, dict) and tool_call.get("tool") in extraction_tools
        for tool_call in (tools_to_run or [])
    )

    if current_df is None and scoped_source.get("requires_extraction") and not requested_extraction:
        reason = (
            "Scoped table boundaries were approved, but the plan does not start with "
            "layout.extract/layout.stack. Pausing to avoid falling back to broad sheet processing."
        )
        logger.warning(reason)
        # region agent log
        _debug_log(
            "H2",
            "missing extraction plan-review pause",
            {
                "job_id": state.get("job_id"),
                "requested_extraction": requested_extraction,
                "requires_extraction": bool(scoped_source.get("requires_extraction")),
                "tool_names": [t.get("tool") for t in (tools_to_run or []) if isinstance(t, dict)],
            },
        )
        # endregion
        hitl_manager = state.get("hitl_manager")
        if hitl_manager:
            checkpoint = hitl_manager.create_checkpoint(
                CheckpointType.PLAN_REVIEW,
                state,
                reason,
                title="Extraction Step Missing",
                description=(
                    "The scoped source requires an explicit extraction step, but the current "
                    "plan would skip directly to downstream transforms. Review or regenerate "
                    "the plan before continuing."
                ),
                severity="high",
                available_actions=["approve", "modify", "regenerate", "cancel"],
                recommended_action="modify",
                trigger_data={
                    "reasoning": reason,
                    "tool_calls": [t for t in (tools_to_run or []) if isinstance(t, dict)],
                    "expected_columns": list(state.get("expected_columns", []) or []),
                }
            )
            hitl_checkpoints.append(checkpoint.to_dict())
        return {
            "requires_review": True,
            "review_reason": reason,
            "hitl_pending_approval": True,
            "hitl_pause_type": "plan_review",
            "hitl_checkpoints": hitl_checkpoints,
            "trace_steps": [{
                "step": "hitl_pause",
                "reason": reason,
                "tools": [t.get("tool") for t in (tools_to_run or []) if isinstance(t, dict)]
            }]
        }
    
    # ===== Initialize current_df from grid if None (BEFORE early return check) =====
    if current_df is None and grid is not None:
        logger.info("Execute Tools: Initializing current_df from grid.to_dataframe()")
        try:
            current_df = grid.to_dataframe()
            logger.info(f"Execute Tools: Initialized current_df with shape {current_df.shape if current_df is not None else 'None'}")
        except Exception as e:
            logger.warning(f"Could not convert grid to DataFrame: {e}")
            # Try fallback: raw data from grid
            if hasattr(grid, 'data') and grid.data is not None:
                current_df = pd.DataFrame(grid.data)
                logger.info(f"Execute Tools: Fallback - created DataFrame from grid.data with shape {current_df.shape}")
    elif current_df is None and grid is None:
        logger.warning("Execute Tools: Both current_df and grid are None!")
    
    # If no tools but we have data, just return the existing data (no error)
    if not tools_to_run:
        logger.info(f"Execute Tools: No tools to run. current_df is {'available' if current_df is not None else 'None'}")
        if current_df is not None:
            current_df, context_actions = apply_context_to_dataframe(
                current_df,
                approved_mappings=state.get("approved_mappings", []),
                business_rules=state.get("business_rules", []),
            )
            tpl = state.get("target_template") or {}
            current_df, rule_actions = apply_template_column_rules(current_df, tpl if isinstance(tpl, dict) else {})
            if rule_actions:
                context_actions = list(context_actions) + rule_actions
            logger.info("Execute Tools: No tools to run, returning existing DataFrame.")
            return {
                "current_df": current_df,
                "current_frame": {
                    "rows": len(current_df),
                    "cols": len(current_df.columns),
                },
                "iteration": state["iteration"],
                "message": f"No tools to execute. DF shape: {current_df.shape}",
                "trace_steps": [{
                    "step": "execute_tools",
                    "tools_executed": 0,
                    "tools": [],
                    "messages": ["No tools suggested - using existing data", *context_actions],
                    "confidence": 1.0
                }]
            }
        else:
            logger.warning("Execute Tools: No tools and no DataFrame. Returning error.")
            return {"errors": ["No tools suggested and no data extracted"]}
    
    iteration_data = current_df
    tools_history = list(state.get("tools_history", []))
    prev_tools_len = len(tools_history)
    warnings = list(state.get("warnings", []))
    triggered_sensitive = []
    low_confidence_items = []
    messages = []
    
    # Import preview generation
    from ..tools.transformation_tools import (
        TransformationTools, 
        generate_all_previews,
        is_tool_destructive,
        DESTRUCTIVE_TOOLS
    )
    
    # ===== HITL Pre-Execution Check for Destructive Tools =====
    destructive_tools_in_plan = [t for t in tools_to_run if is_tool_destructive(t.get("tool", ""))]
    
    # Check if we need to pause for HITL approval
    if not state.get("destructive_approved", False):
        previews = (
            generate_all_previews(iteration_data, destructive_tools_in_plan)
            if destructive_tools_in_plan
            else []
        )

        from sia.integrity.drop_columns_hitl import generate_drop_columns_hitl_preview

        for tool_call in tools_to_run:
            norm_drop, _ = normalize_tool_name(str(tool_call.get("tool", "")).strip())
            if norm_drop != "transform.drop_columns":
                continue
            drop_preview = generate_drop_columns_hitl_preview(
                iteration_data,
                tool_call.get("tool", "transform.drop_columns"),
                tool_call.get("params", {}),
                state,
            )
            if drop_preview:
                previews.append(drop_preview)
        
        # ONLY pause if there is actually something to delete (avoid "0 destructive operations" warning)
        if previews and any(p.rows_to_delete or p.columns_to_delete for p in previews):
            # Return state that signals HITL pause
            return {
                "hitl_pending_approval": True,
                "deletion_previews": [p.to_dict() for p in previews],
                "pending_destructive_tools": destructive_tools_in_plan,
                "trace_steps": [{
                    "step": "hitl_pause",
                    "reason": f"Awaiting approval for {len(previews)} destructive operations",
                    "tools": [t.get("tool") for t in destructive_tools_in_plan]
                }]
            }
        else:
            logger.info("Execute Tools: Destructive tools in plan but none actually found data to delete. Skipping HITL pause.")
    
    # If approved, filter out any tools the user rejected
    approved_tool_indices = state.get("approved_tool_indices", None)
    if approved_tool_indices is not None:
        # User selectively approved - filter tools
        original_count = len(tools_to_run)
        tools_to_run = [
            t for i, t in enumerate(tools_to_run) 
            if i in approved_tool_indices or not is_tool_destructive(t.get("tool", ""))
        ]
        skipped = original_count - len(tools_to_run)
        if skipped > 0:
            logger.info(f"HITL: Skipped {skipped} tools rejected by user")
            messages.append(f"Skipped {skipped} tools per user request")

    tools_to_run = _squeeze_consecutive_same_tool(tools_to_run, "verify.schema")
    defer_grain = _should_defer_weekly_rollup_until_post_collate(state)
    if not defer_grain:
        tools_to_run = _inject_infer_daily_before_aggregate_weekly(tools_to_run, state)
    pre_defer_tools = [dict(t) for t in tools_to_run if isinstance(t, dict)]
    tools_to_run, deferred_post_collate = _partition_deferred_post_collate_grain_tools(
        tools_to_run, state
    )
    tools_to_run = sort_tool_calls_by_pipeline_stage(tools_to_run)
    if state.get("integrity_suppress_checks") or state.get("schema_constraint_suppress_checks"):
        pause_tool = str(
            state.get("integrity_pause_tool")
            or state.get("schema_constraint_pause_tool")
            or ""
        ).strip()
        resume_after_idx = state.get("integrity_resume_after_tool_index")
        if resume_after_idx is not None:
            try:
                idx = int(resume_after_idx)
                if idx + 1 < len(tools_to_run):
                    tools_to_run = tools_to_run[idx + 1 :]
                    logger.info("[HITL] Resume after review: skipping first %d tool(s)", idx + 1)
                else:
                    tools_to_run = []
                    logger.info(
                        "[HITL] Resume after review: no tools remaining after index %s",
                        resume_after_idx,
                    )
            except (TypeError, ValueError):
                pass
        elif pause_tool:
            found = False
            trimmed: List[Dict[str, Any]] = []
            pause_norm, _ = normalize_tool_name(pause_tool)
            for tc in tools_to_run:
                tname = str((tc.get("tool") if isinstance(tc, dict) else "") or "").strip()
                tnorm, _ = normalize_tool_name(tname)
                if found:
                    trimmed.append(tc)
                elif tname == pause_tool or (pause_norm and tnorm == pause_norm):
                    found = True
            if found:
                tools_to_run = trimmed
                logger.info(
                    "[HITL] Resume after review: continuing after tool %s (%d tool(s) left)",
                    pause_tool,
                    len(tools_to_run),
                )
    column_typing_gate_index = _last_column_typing_tool_index(tools_to_run)
    log_state_snapshot(
        "state.execute_tools.deferral",
        "deferral_decision",
        state={**state, "deferred_post_collate_tools": deferred_post_collate},
        decision={
            "defer_grain": defer_grain,
            "context_defer_weekly_rollups_to_post_union": bool(
                (state.get("context_packet") or {}).get("defer_weekly_rollups_to_post_union")
            )
            if isinstance(state.get("context_packet"), dict)
            else False,
            "suggested_tools": [
                t.get("tool") for t in (state.get("suggested_tools") or []) if isinstance(t, dict)
            ],
            "pre_defer_tools": [t.get("tool") for t in pre_defer_tools],
            "tools_to_run": [t.get("tool") for t in tools_to_run if isinstance(t, dict)],
            "column_typing_gate_index": column_typing_gate_index,
            "deferred_post_collate_tools": [
                t.get("tool") for t in deferred_post_collate if isinstance(t, dict)
            ],
        },
    )

    # Create checkpoint before destructive operations
    destructive_tools = get_destructive_tools(tools_to_run)
    last_valid_checkpoint = state.get("last_valid_checkpoint")
    checkpoint_iteration = state.get("checkpoint_iteration", 0)
    
    # Save checkpoint if we have data and there are destructive tools coming
    if iteration_data is not None and not iteration_data.empty and destructive_tools:
        last_valid_checkpoint = iteration_data.copy()
        checkpoint_iteration = state.get("iteration", 0)
        logger.info(f"Checkpoint saved before destructive tools: {destructive_tools}")


    from ..utils.mcp_client import TOOL_TIMEOUT_SEC, get_mcp_client, parse_mcp_call_tool_result

    tool_names_preview = [
        (t.get("tool") if isinstance(t, dict) else "?") for t in (tools_to_run or [])
    ]
    _emit_job_progress(
        state,
        "execute_tools",
        f"Running {len(tools_to_run)} tool(s): {', '.join(tool_names_preview)}",
    )

    merged_context_packet = dict(state.get("context_packet") or {})
    relationship_tool_traces: List[Dict[str, Any]] = []
    relationship_hitl_early: Optional[Dict[str, Any]] = None
    integrity_hitl_early: Optional[Dict[str, Any]] = None
    schema_constraint_hitl_early: Optional[Dict[str, Any]] = None
    context_packet_dirty = False
    async def _mcp_execute_tool_chain(session, tools_cache):
        nonlocal iteration_data, tools_history, messages, warnings, low_confidence_items, triggered_sensitive
        nonlocal merged_context_packet, relationship_tool_traces, relationship_hitl_early, integrity_hitl_early
        nonlocal schema_constraint_hitl_early
        nonlocal context_packet_dirty

        from sia.integrity.metric_reconcile import (
            check_post_tool_integrity,
            integrity_violation_to_hitl_state,
        )
        from sia.integrity.schema_constraints import (
            constraint_issues_from_dataframe,
            constraint_issues_from_report,
            parse_validation_report,
            schema_constraint_to_hitl_state,
        )

        for tool_idx, tool_call in enumerate(tools_to_run):
            tool_name = tool_call.get("tool")
            params = tool_call.get("params", {})
            description = tool_call.get("description", "")
            
            # Validate tool call (handles aliases like merge_blocks -> stack_tables)
            pre_params = _enrich_tool_params_from_template(
                state,
                tool_call.get("tool", ""),
                dict(tool_call.get("params") or {}),
            )
            validation_result = validate_tool_call({**tool_call, "params": pre_params})
            if not validation_result.valid:
                error_msg = f"Tool validation failed for {tool_name}: {validation_result.errors}"
                logger.error(error_msg)
                messages.append(error_msg)
                warnings.extend(validation_result.errors)
                
                # Track failed validation in history
                tools_history = add_tool_execution(
                    state,
                    tool=tool_name,
                    params=params,
                    success=False,
                    message=f"Validation failed: {validation_result.errors}",
                    rows_before=len(iteration_data) if iteration_data is not None else 0,
                    rows_after=len(iteration_data) if iteration_data is not None else 0,
                    duration_ms=0,
                    base_history=tools_history,
                )
                continue
            
            # Use normalized values from validation
            tool_name = validation_result.normalized_tool_name
            params = _coerce_tool_params_to_active_workbook(state, validation_result.normalized_params)
            params = _enrich_tool_params_from_template(state, tool_name, params)
            norm_tool, _ = normalize_tool_name(str(tool_name or "").strip())
            if norm_tool == "transform.rename":
                params = _repair_rename_tool_params(state, params)
            if norm_tool in ("transform.reorder_columns", "transform.sort_rows"):
                params = _repair_final_layout_tool_params(
                    state, tool_name, params, iteration_data
                )
            if norm_tool == "transform.aggregate_weekly":
                params = _repair_aggregate_weekly_tool_params(state, params, iteration_data)
            if norm_tool == "transform.expand_grouped_block":
                params = _repair_expand_grouped_block_tool_params(state, params, iteration_data)
            if norm_tool == "transform.drop_columns":
                params = _repair_drop_columns_tool_params(state, params)
            _grid_layout_tools = frozenset(
                {
                    "layout.extract",
                    "layout.stack",
                    "layout.unpivot_matrix",
                    "extract_data_block",
                    "stack_tables",
                    "crosstab_unpivot",
                    "xls.layout.extract",
                    "xls.layout.stack",
                    "xls.reshape.unpivot_matrix",
                }
            )
            if norm_tool in _grid_layout_tools and grid is not None:
                scoped_for_grid = state.get("scoped_source")
                if norm_tool in (
                    "layout.extract",
                    "extract_data_block",
                    "get_data_block",
                    "extract_table",
                    "xls.layout.extract",
                ):
                    resolved = resolve_layout_extract_params(params, scoped_for_grid)
                else:
                    resolved = rebase_layout_grid_tool_params(
                        norm_tool, params, scoped_for_grid
                    )
                if resolved != params:
                    logger.info(
                        "Resolved %s params for scoped grid (header_row=%s, origin=%s)",
                        norm_tool,
                        resolved.get("header_row"),
                        (scoped_for_grid or {}).get("absolute_bounds")
                        or (scoped_for_grid or {}).get("analysis_bounds"),
                    )
                    params = resolved

            if norm_tool == "layout.stack":
                from sia.agent.layout_stack_utils import looks_like_horizontal_block_failure

                if grid is None:
                    error_msg = (
                        "layout.stack requires the raw sheet grid with explicit block column ranges. "
                        "A flattened DataFrame with duplicate columns (Date, Date.1) cannot be stacked; "
                        "re-run from load_file on the source workbook or define per-block boundaries."
                    )
                    logger.error(error_msg)
                    messages.append(error_msg)
                    warnings.append(error_msg)
                    tools_history = add_tool_execution(
                        state,
                        tool=tool_name,
                        params=params,
                        success=False,
                        message=error_msg,
                        rows_before=rows_before,
                        rows_after=rows_before,
                        duration_ms=0,
                        base_history=tools_history,
                    )
                    continue
                params = _repair_layout_stack_tool_params(state, params, grid)
                if (
                    iteration_data is not None
                    and isinstance(iteration_data, pd.DataFrame)
                    and looks_like_horizontal_block_failure(iteration_data)
                ):
                    warnings.append(
                        "Current frame already has side-by-side duplicate columns; "
                        "layout.stack will use the raw grid only."
                    )

            if (
                tool_name in _DATE_RANGE_PARAM_TOOLS
                and iteration_data is not None
                and isinstance(iteration_data, pd.DataFrame)
                and not iteration_data.empty
            ):
                params = _repair_date_range_tool_params(iteration_data, state, params)
            
            # Add any validation warnings
            if validation_result.warnings:
                warnings.extend(validation_result.warnings)
                logger.info(f"Tool validation warnings: {validation_result.warnings}")
            
            # Track rows before execution
            rows_before = len(iteration_data) if iteration_data is not None else 0
            df_before_tool = (
                iteration_data.copy()
                if isinstance(iteration_data, pd.DataFrame) and not iteration_data.empty
                else None
            )
            log_state_snapshot(
                "state.execute_tools.tool",
                "tool_before",
                state={**state, "current_df": iteration_data, "deferred_post_collate_tools": deferred_post_collate},
                decision={
                    "tool": tool_name,
                    "params_keys": sorted(list((params or {}).keys())),
                    "rows_before": rows_before,
                    "defer_grain": defer_grain,
                },
            )
            
            start_time = time.time()
            mcp_result: Dict[str, Any] = {}

            if str(tool_name).startswith("collation."):
                from sia.tools.collation_tools import execute_collation_tool

                try:
                    if iteration_data is None:
                        success = False
                        message = "No dataframe for collation tool"
                        result_data = iteration_data
                    elif not isinstance(iteration_data, pd.DataFrame):
                        success = False
                        message = "Collation tools require a DataFrame state"
                        result_data = iteration_data
                    else:
                        result_data = execute_collation_tool(tool_name, iteration_data, params)
                        success = True
                        message = "Collation step applied"
                except Exception as e:
                    logger.exception("Collation tool error for %s: %s", tool_name, e)
                    success = False
                    message = str(e)
                    result_data = iteration_data

                duration_ms = (time.time() - start_time) * 1000
                rows_after = len(result_data) if result_data is not None else 0
                if success:
                    iteration_data = result_data
                    rows_after = len(iteration_data) if iteration_data is not None else 0
                    messages.append(f"{tool_name}: Success ({rows_before} → {rows_after} rows)")
                else:
                    rows_after = rows_before
                    messages.append(f"{tool_name}: Failed - {message}")

                tools_history = add_tool_execution(
                    state,
                    tool=tool_name,
                    params=params,
                    success=success,
                    message=message[:1000] if message else "",
                    rows_before=rows_before,
                    rows_after=rows_after,
                    duration_ms=duration_ms,
                    base_history=tools_history,
                )
                detail = (message or "").replace("\n", " ").strip()
                if len(detail) > 160:
                    detail = detail[:157] + "…"
                status_word = "ok" if success else "FAIL"
                _emit_job_progress(
                    state,
                    str(tool_name or "tool"),
                    f"{status_word} — rows {rows_before}→{rows_after} in {duration_ms:.0f}ms"
                    + (f" — {detail}" if detail else ""),
                )
                if success and isinstance(iteration_data, pd.DataFrame) and not state.get("integrity_suppress_checks"):
                    violation = check_post_tool_integrity(
                        df_before_tool,
                        iteration_data,
                        tool_name,
                        params,
                        state,
                        rows_before=rows_before,
                        rows_after=rows_after,
                        is_destructive_fn=is_destructive_tool,
                        norm_tool=norm_tool,
                    )
                    if violation:
                        _record_pipeline_execution_event(state, violation)
                        integrity_hitl_early = integrity_violation_to_hitl_state(
                            violation,
                            iteration_data,
                            state,
                            tools_history_slice=tools_history[prev_tools_len:],
                            deferred_post_collate=deferred_post_collate,
                            warnings=warnings,
                            low_confidence_items=low_confidence_items,
                            tool_index=tool_idx,
                        )
                        break
                if is_destructive_tool(tool_name):
                    triggered_sensitive.append(tool_name)
                observer = get_observer()
                if observer:
                    output_preview = {}
                    snapshot_path = None
                    if iteration_data is not None:
                        sample_rows = (
                            _safe_serialize_df(iteration_data.head(12))
                            if isinstance(iteration_data, pd.DataFrame)
                            else []
                        )
                        output_preview = {
                            "shape": str(iteration_data.shape),
                            "rows": int(len(iteration_data)),
                            "columns": int(len(iteration_data.columns)),
                            "column_order": list(iteration_data.columns),
                            "sample": sample_rows,
                        }
                        try:
                            snapshot_dir = str(Path(__file__).parent.parent.parent / "runtime" / "snapshots")
                            os.makedirs(snapshot_dir, exist_ok=True)
                            filename = _tool_snapshot_basename(state, tool_name)
                            filepath = os.path.join(snapshot_dir, filename)
                            if isinstance(iteration_data, pd.DataFrame):
                                iteration_data.to_csv(filepath, index=False)
                            snapshot_path = filename
                        except Exception as e:
                            logger.error(f"Failed to save snapshot: {e}")
                    observer.log_tool_execution(
                        tool_name=tool_name,
                        params=params,
                        result=message,
                        success=success,
                        duration_ms=duration_ms,
                        output_preview=output_preview,
                        snapshot_path=snapshot_path,
                        pipeline_stage=_observer_pipeline_stage_for_tool(tool_name),
                    )
                log_state_snapshot(
                    "state.execute_tools.tool",
                    "tool_after",
                    state={**state, "current_df": iteration_data, "deferred_post_collate_tools": deferred_post_collate},
                    decision={
                        "tool": tool_name,
                        "success": success,
                        "rows_before": rows_before,
                        "rows_after": rows_after,
                        "duration_ms": duration_ms,
                    },
                )
                continue

            if tool_name == "discovery.propose_file_relationships":
                synth = {**state, "context_packet": merged_context_packet}
                rel_delta = apply_file_relationship_inference(
                    synth, trace_step_name="discovery.propose_file_relationships"
                )
                duration_ms = (time.time() - start_time) * 1000
                if isinstance(rel_delta.get("context_packet"), dict):
                    merged_context_packet = dict(rel_delta["context_packet"])
                    context_packet_dirty = True
                ts_list = list(rel_delta.get("trace_steps") or [])
                relationship_tool_traces.extend(ts_list)
                rel_msg = (ts_list[-1].get("status") if ts_list else "") or "ok"
                tools_history = add_tool_execution(
                    state,
                    tool=tool_name,
                    params=params,
                    success=True,
                    message=str(rel_msg)[:1000],
                    rows_before=rows_before,
                    rows_after=rows_before,
                    duration_ms=duration_ms,
                    base_history=tools_history,
                )
                messages.append(
                    f"{tool_name}: {rel_msg} (proposals={len(rel_delta.get('relationship_proposals') or [])})"
                )
                observer = get_observer()
                if observer:
                    observer.log_tool_execution(
                        tool_name=tool_name,
                        params=params or {},
                        result=str(rel_msg),
                        success=True,
                        duration_ms=duration_ms,
                        input_preview={"sources": len((merged_context_packet.get("available_source_summaries") or []))},
                        output_preview={"proposal_count": len(rel_delta.get("relationship_proposals") or [])},
                        pipeline_stage=_observer_pipeline_stage_for_tool(tool_name),
                    )
                if rel_delta.get("hitl_pending_approval") and rel_delta.get("hitl_pause_type") == "file_relationship_review":
                    relationship_hitl_early = {
                        "current_df": iteration_data,
                        "current_frame": {
                            "rows": len(iteration_data) if iteration_data is not None else 0,
                            "cols": len(iteration_data.columns) if iteration_data is not None else 0,
                        },
                        "iteration": state["iteration"],
                        "hitl_pending_approval": True,
                        "hitl_pause_type": "file_relationship_review",
                        "context_packet": merged_context_packet,
                        "relationship_proposals": list(rel_delta.get("relationship_proposals") or []),
                        "approved_relationships": list(rel_delta.get("approved_relationships") or []),
                        "requires_review": bool(rel_delta.get("requires_review")),
                        "review_reason": str(rel_delta.get("review_reason") or ""),
                        "hitl_checkpoints": list(rel_delta.get("hitl_checkpoints") or []),
                        "trace_steps": relationship_tool_traces
                        + [
                            {
                                "step": "hitl_pause",
                                "reason": "file_relationship_review",
                                "via_tool": "discovery.propose_file_relationships",
                            }
                        ],
                    }
                    break
                log_state_snapshot(
                    "state.execute_tools.tool",
                    "tool_after",
                    state={
                        **state,
                        "context_packet": merged_context_packet,
                        "current_df": iteration_data,
                        "deferred_post_collate_tools": deferred_post_collate,
                    },
                    decision={
                        "tool": tool_name,
                        "success": True,
                        "rows_before": rows_before,
                        "rows_after": rows_before,
                        "relationship_proposals_count": len(rel_delta.get("relationship_proposals") or []),
                    },
                )
                continue

            # ===== PREPARE DATA FOR MCP =====
            # Logic: 
            # 1. If tool is an extraction tool or stack_tables, it needs the FULL GRID (nested list)
            # 2. Otherwise, it needs the CURRENT DATAFRAME (list of records)
            
            grid_based_tools = [
                'layout.extract', 'layout.stack', 'layout.unpivot_matrix',
                # Backward compat aliases:
                'extract_data_block', 'stack_tables', 'crosstab_unpivot',
                'xls.layout.extract', 'xls.layout.stack', 'xls.reshape.unpivot_matrix'
            ]
            
            if tool_name in grid_based_tools and grid is not None:
                # Use raw grid data (nested list)
                data_for_mcp = grid.data if hasattr(grid, 'data') else grid.to_dataframe().values.tolist()
                logger.info(f"Passing GRID (raw data) to {tool_name}")
            elif iteration_data is not None:
                # Use current DataFrame data (list of records)
                data_for_mcp = iteration_data.to_dict(orient='records')
                logger.info(f"Passing DATAFRAME (records) to {tool_name}")
            else:
                logger.warning(f"No data available for tool {tool_name}")
                continue
    
            # Add data to params for MCP call
            mcp_params = {**params, 'data': data_for_mcp}
            
            # ===== EXECUTE VIA MCP (shared session) =====
            mcp_result = {}
            try:
                if tool_name not in tools_cache:
                    local_out = None
                    if iteration_data is not None and isinstance(iteration_data, pd.DataFrame):
                        local_out = _try_execute_transform_tool_locally(
                            tool_name, iteration_data, params
                        )
                    if local_out is not None:
                        loc_ok, loc_df, loc_msg = local_out
                        mcp_result = {
                            "ok": loc_ok,
                            "success": loc_ok,
                            "message": loc_msg,
                            "full_data": (
                                loc_df.to_dict(orient="records")
                                if loc_ok and loc_df is not None
                                else []
                            ),
                        }
                        if loc_ok:
                            logger.info(
                                "Executed %s via local TransformationTools fallback (not in MCP cache)",
                                tool_name,
                            )
                    else:
                        mcp_result = {
                            "ok": False,
                            "success": False,
                            "message": (
                                f"Unknown tool: {tool_name}. Available: {list(tools_cache.keys())}"
                            ),
                        }
                else:
                    raw = await asyncio.wait_for(
                        session.call_tool(tool_name, mcp_params),
                        timeout=TOOL_TIMEOUT_SEC,
                    )
                    mcp_result = parse_mcp_call_tool_result(raw)
                
                # Convert MCP result to local format
                if mcp_result.get('ok') or mcp_result.get('success'):
                    # Reconstruct DataFrame from result
                    result_data = iteration_data
                    
                    # IMPORTANT: Use full_data to avoid truncation. 
                    # data_preview is only for the LLM/UI and is limited to 100 rows.
                    full_data = mcp_result.get('full_data', [])
                    if full_data and isinstance(full_data, list):
                        result_data = pd.DataFrame(full_data)
                        logger.info(f"Reconstructed DataFrame from full_data. Shape: {result_data.shape}")
                    else:
                        # Fallback to data_preview if full_data is missing (should not happen with latest server)
                        preview = mcp_result.get('data_preview', [])
                        if preview and isinstance(preview, list):
                            result_data = pd.DataFrame(preview)
                            logger.warning(f"full_data missing! Fell back to data_preview (potential truncation). Shape: {result_data.shape}")
                    
                    # Check for success even if no data returned (e.g. side effects)
                    success = True
                    message = mcp_result.get('message', 'Success')
                    if norm_tool == "layout.stack" and isinstance(result_data, pd.DataFrame):
                        from sia.agent.layout_stack_utils import looks_like_horizontal_block_failure

                        if looks_like_horizontal_block_failure(result_data):
                            success = False
                            message = (
                                "layout.stack left side-by-side duplicate columns (e.g. Date, Date.1). "
                                "Define separate col_start/col_end per block on the raw grid."
                            )
                            result_data = iteration_data
                else:
                    success = False
                    # Check for message in the new 'context.summary' or top-level 'message'
                    context = mcp_result.get('context', {})
                    message = context.get('summary') or mcp_result.get('message', 'MCP Tool Failed')
                    result_data = iteration_data
            except Exception as e:
                logger.error(f"MCP execution error for {tool_name}: {e}")
                success = False
                message = f"MCP Error: {str(e)}"
                result_data = iteration_data
                
            duration_ms = (time.time() - start_time) * 1000
            rows_after = 0
            
            if success:
                iteration_data = result_data
                rows_after = len(iteration_data) if iteration_data is not None else 0
                messages.append(f"{tool_name}: Success ({rows_before} → {rows_after} rows)")
            else:
                rows_after = rows_before  # No change on failure
                messages.append(f"{tool_name}: Failed - {message}")
                
            # Track execution in history
            tools_history = add_tool_execution(
                state,
                tool=tool_name,
                params=params,
                success=success,
                message=message[:1000] if message else "",
                rows_before=rows_before,
                rows_after=rows_after,
                duration_ms=duration_ms,
                base_history=tools_history,
            )
    
            detail = (message or "").replace("\n", " ").strip()
            if len(detail) > 160:
                detail = detail[:157] + "…"
            status_word = "ok" if success else "FAIL"
            _emit_job_progress(
                state,
                str(tool_name or "tool"),
                f"{status_word} — rows {rows_before}→{rows_after} in {duration_ms:.0f}ms"
                + (f" — {detail}" if detail else ""),
            )
    
            # ===== DATA INTEGRITY GUARDRAIL (pause for HITL on violation) =====
            if success and isinstance(iteration_data, pd.DataFrame) and not state.get("integrity_suppress_checks"):
                violation = check_post_tool_integrity(
                    df_before_tool,
                    iteration_data,
                    tool_name,
                    params,
                    state,
                    rows_before=rows_before,
                    rows_after=rows_after,
                    is_destructive_fn=is_destructive_tool,
                    norm_tool=norm_tool,
                )
                if violation:
                    _record_pipeline_execution_event(state, violation)
                    integrity_hitl_early = integrity_violation_to_hitl_state(
                        violation,
                        iteration_data,
                        state,
                        tools_history_slice=tools_history[prev_tools_len:],
                        deferred_post_collate=deferred_post_collate,
                        warnings=warnings,
                        low_confidence_items=low_confidence_items,
                        tool_index=tool_idx,
                    )
                    break
            
            if is_destructive_tool(tool_name):
                triggered_sensitive.append(tool_name)
                
            # Log to observer for Debug UI
            observer = get_observer()
            if observer:
                output_preview = {}
                snapshot_path = None
                
                if iteration_data is not None:
                    sample_rows = _safe_serialize_df(iteration_data.head(12)) if isinstance(iteration_data, pd.DataFrame) else []
                    output_preview = {
                        "shape": str(iteration_data.shape),
                        "rows": int(len(iteration_data)),
                        "columns": int(len(iteration_data.columns)),
                        "column_order": list(iteration_data.columns),
                        "sample": sample_rows,
                    }
                    
                    try:
                        import os
                        snapshot_dir = str(Path(__file__).parent.parent.parent / "runtime" / "snapshots")
                        os.makedirs(snapshot_dir, exist_ok=True)
                        filename = _tool_snapshot_basename(state, tool_name)
                        filepath = os.path.join(snapshot_dir, filename)
                        iteration_data.to_csv(filepath, index=False)
                        snapshot_path = filename
                    except Exception as e:
                        logger.error(f"Failed to save snapshot: {e}")
                        
                observer.log_tool_execution(
                    tool_name=tool_name,
                    params=params,
                    result=message,
                    success=success,
                    duration_ms=duration_ms,
                    output_preview=output_preview,
                    snapshot_path=snapshot_path,
                    pipeline_stage=_observer_pipeline_stage_for_tool(tool_name),
                )
            log_state_snapshot(
                "state.execute_tools.tool",
                "tool_after",
                state={**state, "current_df": iteration_data, "deferred_post_collate_tools": deferred_post_collate},
                decision={
                    "tool": tool_name,
                    "success": success,
                    "rows_before": rows_before,
                    "rows_after": rows_after,
                    "duration_ms": duration_ms,
                },
            )
                
            # NEW: Check for low confidence column matches or tool execution
            changes_made = mcp_result.get('changes_made', {}) if isinstance(mcp_result, dict) else {}
            col_confidences = changes_made.get('column_confidences', {})
            
            for col, conf in col_confidences.items():
                if conf < 0.9:
                    low_confidence_items.append({
                        "type": "column_match",
                        "tool": tool_name,
                        "item": col,
                        "confidence": conf,
                        "details": f"Fuzzy match for '{col}' has low confidence ({conf:.2f})"
                    })
                    
            # Also check tool-level confidence if provided by planner or verifier
            tool_confidence = tool_call.get("confidence", 1.0)
            if tool_confidence < 0.9:
                low_confidence_items.append({
                    "type": "tool_call",
                    "tool": tool_name,
                    "confidence": tool_confidence,
                    "details": f"Tool call suggested with low confidence ({tool_confidence:.2f})"
                })

            # Runtime fail-fast gate: once rename/type_cast/add/rule operations
            # are complete, scan only template min/max before row-expanding work.
            if (
                success
                and tool_idx == column_typing_gate_index
                and isinstance(iteration_data, pd.DataFrame)
                and not state.get("schema_constraint_suppress_checks")
            ):
                gate_template = state.get("target_template")
                if not isinstance(gate_template, dict) or not gate_template:
                    gate_template = (
                        merged_context_packet.get("target_template")
                        if isinstance(merged_context_packet.get("target_template"), dict)
                        else {}
                    )
                gate_issues = constraint_issues_from_dataframe(
                    iteration_data,
                    normalize_target_template(gate_template),
                )
                if gate_issues:
                    for gate_issue in gate_issues:
                        _record_pipeline_execution_event(
                            state,
                            {
                                "type": "schema_constraint",
                                "subtype": "post_column_typing",
                                "tool": tool_name,
                                **gate_issue,
                            },
                        )
                    schema_constraint_hitl_early = schema_constraint_to_hitl_state(
                        gate_issues,
                        iteration_data,
                        state,
                        tools_history_slice=tools_history[prev_tools_len:],
                        deferred_post_collate=deferred_post_collate,
                        warnings=warnings,
                        low_confidence_items=low_confidence_items,
                        tool_index=tool_idx,
                        tool_name=str(tool_name or "column_typing"),
                    )
                    break

            # Template min/max → Review-style pause (same class of flag as duplicates)
            if (
                success
                and norm_tool == "verify.schema"
                and not state.get("schema_constraint_suppress_checks")
            ):
                report = parse_validation_report(
                    message=message,
                    changes_made=changes_made if isinstance(changes_made, dict) else None,
                )
                constraint_issues = constraint_issues_from_report(report)
                if constraint_issues:
                    schema_constraint_hitl_early = schema_constraint_to_hitl_state(
                        constraint_issues,
                        iteration_data if isinstance(iteration_data, pd.DataFrame) else None,
                        state,
                        tools_history_slice=tools_history[prev_tools_len:],
                        deferred_post_collate=deferred_post_collate,
                        warnings=warnings,
                        low_confidence_items=low_confidence_items,
                        tool_index=tool_idx,
                        tool_name=str(tool_name or "verify.schema"),
                    )
                    break

    try:
        get_mcp_client().run_session(_mcp_execute_tool_chain)
    except Exception as e:
        logger.exception("MCP shared session failed: %s", e)
        messages.append(f"MCP session error: {e}")

    if relationship_hitl_early is not None:
        rh = dict(relationship_hitl_early)
        rh["tools_history"] = tools_history[prev_tools_len:]
        return rh

    if integrity_hitl_early is not None:
        ih = dict(integrity_hitl_early)
        ih["tools_history"] = tools_history[prev_tools_len:]
        return ih

    if schema_constraint_hitl_early is not None:
        sh = dict(schema_constraint_hitl_early)
        sh["tools_history"] = tools_history[prev_tools_len:]
        return sh

    # Check if we should rollback (data became empty or confidence dropped)
    if iteration_data is not None and iteration_data.empty and last_valid_checkpoint is not None and not last_valid_checkpoint.empty:
        logger.warning("Data became empty after tools - rolling back to checkpoint")
        iteration_data = last_valid_checkpoint
        messages.append("ROLLBACK: Restored from checkpoint due to empty result")
        warnings.append("Rollback triggered - tools resulted in empty DataFrame")
    
    iteration_data, context_actions = apply_context_to_dataframe(
        iteration_data,
        approved_mappings=state.get("approved_mappings", []),
        business_rules=state.get("business_rules", []),
        drop_excluded=False,
    )
    if context_actions:
        messages.extend(context_actions)
    tpl = state.get("target_template") or {}
    iteration_data, rule_actions = apply_template_column_rules(
        iteration_data, tpl if isinstance(tpl, dict) else {}
    )
    if rule_actions:
        messages.extend(rule_actions)

    if isinstance(tpl, dict) and tpl and iteration_data is not None:
        iteration_data, prune_actions = prune_dataframe_to_template(
            iteration_data,
            tpl,
            approved_mappings=state.get("approved_mappings", []),
            business_rules=state.get("business_rules", []),
            extra_allowed=["week_start", "calendar_date", "days_in_week"],
        )
        if prune_actions:
            messages.extend(prune_actions)
            warnings.extend(prune_actions)

    requires_review = len(triggered_sensitive) > 0 or len(low_confidence_items) > 0
    
    # ===== HITL: Check for checksum failures and create review items =====
    hitl_checkpoints = list(state.get("hitl_checkpoints", []))
    hitl_manager = state.get("hitl_manager")
    
    # Check if any verify_checksum tool ran and had failures
    checksum_result = {}
    for hist in tools_history:
        if hist.get("tool") == "verify_checksum" and hist.get("success"):
            result_data = hist.get("result_data", {})
            if result_data.get("failures"):
                checksum_result = result_data
    
    if hitl_manager and checksum_result:
        check_state = {**state, "checksum_result": checksum_result}
        checkpoint = hitl_manager.should_checkpoint(check_state, "execute_tools")
        if checkpoint:
            hitl_checkpoints.append(checkpoint.to_dict())
            requires_review = True
            logger.info(f"HITL checksum checkpoint created: {checkpoint.checkpoint_id}")
    
    execute_trace = [{
        "step": "execute_tools",
        "tools_executed": len(tools_to_run),
        "tools": [t.get("tool") for t in tools_to_run],
        "deferred_post_collate_tools": deferred_post_collate,
        "messages": messages,
        "confidence": 1.0,
        **(
            {"plan_review_fast_resume": True}
            if consumed_fast_resume
            else {}
        ),
    }]
    _record_pipeline_execution_deferral(
        state,
        defer_grain=defer_grain,
        planned_tools=pre_defer_tools,
        executed_tools=_tool_calls_from_history(tools_history[prev_tools_len:]),
        deferred_tools=deferred_post_collate,
    )
    out_execute = {
        "current_df": iteration_data,
        "current_frame": {
            "rows": len(iteration_data) if iteration_data is not None else 0,
            "cols": len(iteration_data.columns) if iteration_data is not None else 0,
        },
        "iteration": state["iteration"],
        "hitl_pending_approval": False,
        "warnings": warnings,
        "last_valid_checkpoint": last_valid_checkpoint,
        "checkpoint_iteration": checkpoint_iteration,
        "requires_review": requires_review,
        "low_confidence_items": low_confidence_items,
        "hitl_checkpoints": hitl_checkpoints,
        "message": (
            f"Tool chain finished ({len(tools_to_run)} tool(s)); "
            f"shape {iteration_data.shape if iteration_data is not None else 'None'}"
        ),
        "tools_history": tools_history[prev_tools_len:],
        "trace_steps": relationship_tool_traces + execute_trace,
        "deferred_post_collate_tools": deferred_post_collate,
        "resume_skip_pipeline_after_load": False,
    }
    if context_packet_dirty:
        out_execute["context_packet"] = merged_context_packet
        rp = merged_context_packet.get("relationship_proposals")
        if isinstance(rp, list):
            out_execute["relationship_proposals"] = rp
    return out_execute

@trace_node("verify_output", input_keys=["iteration"])
def verify_output_node(state: AgentState) -> Dict[str, Any]:
    """
    LLM verifies if output is flat with context-rich verification.

    For multi-source **union** batches where weekly rollup is deferred to post-collation,
    weekly grain contract checks are skipped here so daily per-source frames do not force replan.
    """
    llm_present = bool(state.get("llm_client"))
    df = state.get("current_df")
    df_empty = df is None or df.empty
    
    if not state.get("llm_client"):
        # No LLM to verify — skip but don't claim success
        return {
            "is_flat": False,
            "errors": ["LLM client missing — cannot verify output"],
            "trace_steps": [{"step": "verify_output", "is_flat": False, "confidence": 0.0, "issues_count": 1}]
        }
    if state.get("current_df") is None or state["current_df"].empty:
        # Empty DataFrame is NOT valid — flag it
        return {
            "is_flat": False,
            "errors": ["DataFrame is empty — tool execution produced no data"],
            "trace_steps": [{"step": "verify_output", "is_flat": False, "confidence": 0.0, "issues_count": 1}]
        }
        
    df = state.get("current_df")
    
    # Capture prompts for trace
    observer = get_observer()
    if observer:
        span = observer._active_spans[-1] if observer._active_spans else None
        if span:
            span.system_prompt = _load_system_prompt("output_verifier")
            span.user_prompt = f"Verify DataFrame shape {state['current_df'].shape}. Iteration: {state.get('iteration', 0)}"
            
    # Pass template + Guided Setup alignment so the verifier can enforce weekly contracts
    # (prompt §4a + deterministic merge for missing rollup tools).
    tpl = state.get("target_template") or {}
    cp = dict(state.get("context_packet") or {})
    xs = tpl.get("x_scope") if isinstance(tpl, dict) else None
    xs = xs if isinstance(xs, dict) else {}
    tgt_grain = effective_target_date_granularity(xs)
    sm = dict(state.get("source_metadata") or {})
    alignment = cp.get("date_granularity_alignment")
    if not isinstance(alignment, dict) or not alignment:
        alignment = compute_date_granularity_alignment(
            str(sm.get("date_granularity") or ""),
            str(tgt_grain or ""),
            source_date_shape=str(sm.get("date_shape") or ""),
        )

    skip_weekly_contract = _should_defer_weekly_rollup_until_post_collate(state)
    it_preview = int(state.get("iteration", 1) or 1)
    mx_preview = int(state.get("max_iterations", 3) or 3)
    _emit_job_progress(
        state,
        "verify_output",
        f"Output verification running (iteration≈{it_preview}/{mx_preview})…",
    )
    sparse_dim_names: List[str] = []
    src_summary = (
        ((cp.get("planning_summary") or {}).get("source_summary") or {})
        if isinstance(cp.get("planning_summary"), dict)
        else {}
    )
    for item in list(src_summary.get("sparse_dimension_columns") or []):
        if isinstance(item, dict):
            col = str(item.get("source_column") or item.get("column") or "").strip()
        else:
            col = str(item or "").strip()
        if col:
            sparse_dim_names.append(col)
    verifier_inst = OutputVerifier(state.get("llm_client"))
    business_rules = list(state.get("business_rules", []))
    planned_rule_actions = list(state.get("planned_rule_actions", []))
    result = verifier_inst.verify(
        df=state["current_df"],
        column_info=None,
        expected_columns=state.get("expected_columns", []),
        tools_history=state.get("tools_history", []),
        issues_history=state.get("issues_history", []),
        business_rules=business_rules,
        planned_rule_actions=planned_rule_actions,
        iteration=state.get("iteration", 0),
        max_iterations=state.get("max_iterations", 3),
        date_granularity_alignment=None if skip_weekly_contract else alignment,
        target_date_granularity=None if skip_weekly_contract else (str(tgt_grain or "") or None),
        skip_weekly_grain_contract=skip_weekly_contract,
        sparse_dimension_columns=sparse_dim_names if skip_weekly_contract else None,
    )
    logger.info(
        "verify_output: verifier returned is_flat=%s issues=%s (iteration=%s)",
        result.is_flat,
        len(result.issues or []),
        state.get("iteration", 0),
    )

    deterministic_rule_issues = _evaluate_business_rules(state["current_df"], business_rules)
    if deterministic_rule_issues:
        result.rule_issues.extend(deterministic_rule_issues)
        result.issues.extend(deterministic_rule_issues)
        result.summary = (
            f"{result.summary} Rule issues: {len(deterministic_rule_issues)}."
            if result.summary else
            f"Detected {len(deterministic_rule_issues)} business rule issues."
        )
        result.is_flat = False
        result.confidence = min(result.confidence or 1.0, 0.6)
        logger.info(
            "verify_output: deterministic business rules flagged %s issue(s); forcing is_flat=False",
            len(deterministic_rule_issues),
        )

    obs_post = get_observer()
    if obs_post:
        obs_post.annotate_last_trace_component(
            "output_verifier",
            {
                "graph_is_flat_after_verify_node": bool(result.is_flat),
                "business_rules_blocked_flat": bool(deterministic_rule_issues),
                "business_rule_issue_count": len(deterministic_rule_issues or []),
            },
        )
    
    # Track issues history
    issues_history = list(state.get("issues_history", []))
    for issue in result.issues:
        issues_history = add_issue(
            state,
            issue_type=issue.get("issue_type", "unknown"),
            description=issue.get("description", ""),
            affected_columns=issue.get("affected_columns", []),
            severity=issue.get("severity", "medium")
        )
    
    # Track confidence trajectory
    confidence_trajectory = list(state.get("confidence_trajectory", []))
    confidence_trajectory.append(result.confidence)
    
    # Check for verification stall (plateau or same issue repeated)
    hitl_pending_approval = state.get("hitl_pending_approval", False)
    hitl_checkpoints = list(state.get("hitl_checkpoints", []))
    escalation_reason = state.get("escalation_reason", "")
    
    stall_state = dict(state)
    stall_state["confidence_trajectory"] = confidence_trajectory
    if (not result.is_flat) and is_stalled(stall_state):
        # Determine stall type for better messaging
        trajectory = confidence_trajectory
        if len(trajectory) >= 3 and abs(trajectory[-1] - trajectory[-2]) < 0.05:
            stall_type = "Confidence Plateau"
            escalation_reason = f"Verification stalled: Confidence has plateaued at {result.confidence:.1%}"
        else:
            stall_type = "Repeated Issues"
            escalation_reason = f"Verification stalled: Some issues are appearing repeatedly without successful resolution."
            
        logger.warning(f"[STALL] {escalation_reason}")
        hitl_pending_approval = True
        
        # Create HITL checkpoint for stall
        hitl_manager = state.get("hitl_manager")
        if hitl_manager:
            checkpoint = hitl_manager.create_checkpoint(
                CheckpointType.VERIFICATION_STALL,
                state,
                escalation_reason,
                title=f"Verification Stalled ({stall_type})",
                description=f"{escalation_reason} Human intervention is required to correct the approach.",
                available_actions=list(HITLManager.STALL_REVIEW_ACTIONS),
                recommended_action="accept_as_is",
                trigger_data={
                    "iteration": state.get("iteration", 0),
                    "verifier_issues": list(result.issues or []),
                    "sheet_name": str(
                        (state.get("scoped_source") or {}).get("sheet_name")
                        or state.get("sheet_name")
                        or ""
                    ),
                    "source_id": str(state.get("source_id") or ""),
                },
            )
            hitl_checkpoints.append(checkpoint.to_dict())

    it = int(state.get("iteration", 1))
    mx = int(state.get("max_iterations", 3))
    summary_preview = str(result.summary or "").strip().replace("\n", " ")
    if len(summary_preview) > 700:
        summary_preview = summary_preview[:700] + "…"
    _emit_job_progress(
        state,
        "verify_output",
        (
            f"Output verification — flat={bool(result.is_flat)} iteration≈{it}/{mx} "
            f"issues={len(result.issues or [])}"
            + (f" — {summary_preview}" if summary_preview else "")
        ),
    )

    _record_pipeline_verify_eval(state, list(result.issues or []))

    return {
        "is_flat": result.is_flat,
        "verifier_issues": result.issues,  # NEW: Match AgentState field name
        "verification_summary": result.summary,  # NEW: Pass verifier's detailed summary to replanner
        "suggested_tools": result.suggested_tools,
        "requires_review": False, # Handled by should_retry edge
        "confidence_trajectory": confidence_trajectory,
        "escalation_reason": escalation_reason,
        "hitl_checkpoints": hitl_checkpoints,
        "hitl_pending_approval": hitl_pending_approval,
        "hitl_pause_type": "verification_stall" if hitl_pending_approval else None,
        "message": f"Verification: {'Flat' if result.is_flat else 'Issue found'}. Conf: {result.confidence:.1%}",
        "trace_steps": [{
            "step": "verify_output",
            "is_flat": result.is_flat,
            "confidence": result.confidence,
            "issues_count": len(result.issues),
            "rule_issues_count": len(result.rule_issues),
        }]
    }


@trace_node("replan", input_keys=["issues_history"])
def replan_node(state: AgentState) -> Dict[str, Any]:
    """
    LLM re-analyzes the situation when verification fails.
    
    This is the 'Think' step in ReAct pattern:
    - Analyzes what went wrong
    - Generates alternative approach
    - May recommend escalation
    
    Only called when verify_output finds is_flat=False.
    """
    if not state["llm_client"]:
        # No LLM, just use verification suggested tools (must bump iteration — else verify→replan loops forever).
        _emit_job_progress(
            state,
            "replan",
            "Replan (no LLM): reusing verifier suggested tools; bumping iteration to avoid verify loops.",
        )
        return {
            "iteration": int(state.get("iteration", 1)) + 1,
            "current_df": state.get("current_df"),
            "suggested_tools": list(state.get("suggested_tools", [])),
            "trace_steps": [
                {
                    "step": "replan",
                    "skipped_llm": True,
                    "tools_count": len(state.get("suggested_tools", [])),
                    "confidence": 0.0,
                }
            ],
        }
    
    # Get the latest verification result from state
    # We reconstruct it from trace_steps
    verification_result = None
    trace_steps = state.get("trace_steps", [])
    for step in reversed(trace_steps):
        if step.get("step") == "verify_output":
            verification_result = VerificationResult(
                is_flat=step.get("is_flat", False),
                confidence=step.get("confidence", 0.0),
                issues=state.get("verifier_issues", []),  # FIXED: Was 'verification_issues' (wrong key)
                suggested_tools=state.get("suggested_tools", []),
                summary=state.get("verification_summary", f"Confidence: {step.get('confidence', 0):.1%}")
            )
            break
    
    replanner = Replanner(state["llm_client"])
    
    # Load target template for enrichment-aware replanning
    target_template = state.get("target_template")
    if not target_template:
        try:
            import os
            template_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "config",
                "target_template.json"
            )
            if os.path.exists(template_path):
                import json as _json
                with open(template_path, 'r') as f:
                    target_template = _json.load(f)
        except Exception as e:
            logger.warning(f"Could not load target_template for replanner: {e}")
    
    # Capture prompts for trace
    observer = get_observer()
    if observer:
        span = observer._active_spans[-1] if observer._active_spans else None
        if span:
            span.system_prompt = _load_system_prompt("replanner")
            import json
            issues = state.get("issues_history", [])
            span.user_prompt = (
                f"Replan based on {len(issues)} issues. "
                f"Latest issues: {json_safe_dumps(issues[-2:], indent=2)}"
            )
            
    logger.info(
        "replan_node: calling Replanner (iteration=%s, tools_history=%s)",
        state.get("iteration", 0),
        len(state.get("tools_history") or []),
    )
    sm = state.get("source_metadata") or {}
    xs = (target_template or {}).get("x_scope") if isinstance((target_template or {}).get("x_scope"), dict) else {}
    date_granularity_alignment = compute_date_granularity_alignment(
        str(sm.get("date_granularity") or ""),
        effective_target_date_granularity(xs),
        source_date_shape=str(sm.get("date_shape") or ""),
    )
    defer_union = _should_defer_weekly_rollup_until_post_collate(state)
    _emit_job_progress(
        state,
        "replan",
        f"Calling replanner LLM (attempt {int(state.get('iteration', 1))}/{int(state.get('max_iterations', 3))}); this can take minutes…",
    )
    new_plan = replanner.replan(
        current_df=state.get("current_df"),
        previous_tools=state.get("tools_history", []),
        previous_issues=state.get("issues_history", []),
        verification_result=verification_result,
        iteration=state.get("iteration", 0),
        max_iterations=state.get("max_iterations", 3),
        target_template=target_template,
        suggested_tools=state.get("suggested_tools", []),  # NEW: Pass verifier's tool suggestions
        date_granularity_alignment=None if defer_union else date_granularity_alignment,
        defer_union_weekly_rollup=defer_union,
    )

    previous_plan_tools = state.get("last_replan_tools")
    if not isinstance(previous_plan_tools, list):
        prior_plan = state.get("extraction_plan")
        previous_plan_tools = list(getattr(prior_plan, "tool_calls", []) or [])
    repeated_plan = bool(new_plan.tool_calls) and (
        tool_plan_fingerprint(new_plan.tool_calls)
        == tool_plan_fingerprint(previous_plan_tools)
    )
    hitl_checkpoints = list(state.get("hitl_checkpoints", []))
    hitl_pending_approval = bool(new_plan.requires_human_review)
    escalation_reason = str(new_plan.review_reason or "")
    if repeated_plan:
        escalation_reason = (
            "Replanner produced the same effective tool plan for unresolved verification issues; "
            "automatic retry was stopped to avoid repeating work."
        )
        new_plan.requires_human_review = True
        new_plan.review_reason = escalation_reason
        hitl_pending_approval = True
        logger.warning("[REPLAN_STALL] %s", escalation_reason)
    if hitl_pending_approval:
        hitl_manager = state.get("hitl_manager")
        if hitl_manager:
            checkpoint = hitl_manager.create_checkpoint(
                CheckpointType.VERIFICATION_STALL,
                state,
                escalation_reason,
                title="Replanner Made No Progress" if repeated_plan else "Replanner Escalation",
                description=(
                    f"{escalation_reason} Review the latest verifier issues and choose a different approach."
                ),
                available_actions=list(HITLManager.STALL_REVIEW_ACTIONS),
                recommended_action="accept_as_is",
                trigger_data={
                    "iteration": state.get("iteration", 0),
                    "verifier_issues": list(state.get("verifier_issues") or []),
                    "sheet_name": str(
                        (state.get("scoped_source") or {}).get("sheet_name")
                        or state.get("sheet_name")
                        or ""
                    ),
                    "source_id": str(state.get("source_id") or ""),
                },
            )
            hitl_checkpoints.append(checkpoint.to_dict())
    
    # Track confidence from replanning
    confidence_trajectory = list(state.get("confidence_trajectory", []))
    confidence_trajectory.append(new_plan.confidence)
    
    logger.info(f"Replan generated {len(new_plan.tool_calls)} new tools with confidence {new_plan.confidence:.1%}")

    _emit_job_progress(
        state,
        "replan",
        (
            f"Replan produced {len(new_plan.tool_calls)} tool(s); "
            + (
                "paused for analyst review."
                if hitl_pending_approval
                else "re-executing from the last processed frame."
            )
        ),
    )

    return {
        "iteration": int(state.get("iteration", 1)) + 1,
        # Keep the processed frame. Wiping it forced execute_tools to rebuild
        # from the raw grid (0, 1, 2…) and broke rename/fill on the next pass.
        "current_df": state.get("current_df"),
        "suggested_tools": new_plan.tool_calls,
        "last_replan_tools": list(new_plan.tool_calls or []),
        "confidence_trajectory": confidence_trajectory,
        "requires_review": new_plan.requires_human_review,
        "review_reason": new_plan.review_reason,
        "escalation_reason": escalation_reason,
        "hitl_checkpoints": hitl_checkpoints,
        "hitl_pending_approval": hitl_pending_approval,
        "hitl_pause_type": "verification_stall" if hitl_pending_approval else None,
        "message": f"Replan: Generated {len(new_plan.tool_calls)} tools to fix issues",
        "trace_steps": [{
            "step": "replan",
            "tools_count": len(new_plan.tool_calls),
            "confidence": new_plan.confidence,
            "reasoning": new_plan.reasoning[:200] if new_plan.reasoning else ""
        }]
    }

@trace_node("finalize", input_keys=["requires_review"])
def finalize_node(state: AgentState) -> Dict[str, Any]:
    """Build final schema and prepare output."""
    import asyncio
    try:
        df = state["current_df"]
        llm_client = state.get("llm_client")

        tpl = state.get("target_template") or {}
        _emit_job_progress(
            state,
            "finalize",
            "Finalizing output (schema, optional judge)…",
        )
        if isinstance(tpl, dict) and tpl and df is not None and not df.empty:
            df, final_prune_actions = prune_dataframe_to_template(
                df,
                tpl,
                approved_mappings=state.get("approved_mappings", []),
                business_rules=state.get("business_rules", []),
                extra_allowed=["week_start", "calendar_date", "days_in_week"],
            )
            if final_prune_actions:
                logger.info("Finalize prune: %s", "; ".join(final_prune_actions))
            df, sort_notes = sort_dataframe_by_template(
                df,
                tpl,
                sort_columns=final_output_sort_columns(tpl, present_columns=list(df.columns)),
            )
            if sort_notes:
                logger.info("Finalize sort: %s", "; ".join(sort_notes))

        if df is None or df.empty:
            schema = InferredSchema("empty")
        else:
            schema = InferredSchema.from_dataframe(df)
            
        # Judge Evaluation (Run synchronously) — optional via state["enable_llm_judge"]
        judge_result = None
        run_judge = bool(state.get("enable_llm_judge", True))
        if run_judge and llm_client and df is not None and not df.empty:
            try:
                logger.info("finalize_node: running LLMJudge (trace + output critique)")
                _emit_job_progress(
                    state,
                    "finalize",
                    "Optional quality judge is running (skips if the model does not respond in time)…",
                )
                judge = LLMJudge(llm_client)
                # Create a string representation of the input grid for the judge
                input_grid = state.get("grid")
                input_sample = ""
                if input_grid:
                    # Get a 20x10 snippet of the original grid
                    input_sample = input_grid.to_text_grid(max_rows=20, max_cols=10)
                
                # Aggregate Token Usage from Observer
                from ..debug.llm_observer import get_observer
                observer = get_observer()
                total_tokens = 0
                if observer:
                    summary = observer.get_summary()
                    total_tokens = summary.get("total_input_tokens", 0) + summary.get("total_output_tokens", 0)
                    # Inject total tokens into the trace for the trace_auditor
                    state.get("trace_steps", []).append({"total_tokens_used": total_tokens})

                # Run judge evaluate directly (Sync)
                pipeline_evals_summary = ""
                job, pe_runner = _pipeline_eval_job(state)
                if job and pe_runner:
                    pipeline_evals_summary = pe_runner.summarize_for_judge(job)
                score = judge.evaluate(
                    input_sample=input_sample,
                    output_df=df,
                    schema=schema.to_dict(),
                    trace=state.get("trace_steps", []),
                    pipeline_evals_summary=pipeline_evals_summary,
                )
                
                # Additional logic: if judge failed to return total tokens, inject it manually
                if "total_tokens" not in score.metrics or score.metrics["total_tokens"] == 0:
                    score.metrics["total_tokens"] = total_tokens

                judge_result = {
                    "fidelity": score.fidelity,
                    "flatness": score.flatness,
                    "integrity": score.integrity,
                    "tool_accuracy": score.tool_accuracy,
                    "trajectory_success": score.trajectory_success,
                    "task_success": score.task_success,
                    "token_efficiency": score.token_efficiency,
                    "verdict": score.verdict,
                    "critique": score.critique,
                    "metrics": score.metrics,
                    "trace_analysis": score.trace_analysis 
                }
                logger.info(f"Judge Verdict: {score.verdict} Accuracy: {score.tool_accuracy:.2f}")

                # Merge step_scores back into trace_steps for per-step UI display
                trace_steps = state.get("trace_steps", [])
                step_scores = score.trace_analysis.get("step_scores", []) if score.trace_analysis else []
                for step_score in step_scores:
                    idx = step_score.get("index")
                    if idx is not None and 0 <= idx < len(trace_steps):
                        trace_steps[idx]["judge_metrics"] = {
                            "logic_score": step_score.get("logic_score", 0.0),
                            "fidelity_score": step_score.get("fidelity_score", 0.0),
                            "critique": step_score.get("critique", "")
                        }
                logger.info(f"Merged {len(step_scores)} step scores into trace_steps.")

                if job and pe_runner and judge_result:
                    sid = _pipeline_eval_source_id(state)
                    pe_runner.record_judge_mirror(job, {**judge_result, "source_id": sid})
                    per_judge = dict((job.get("pipeline_evals") or {}).get("judge") or {})
                    by_source = dict(per_judge.get("per_source") or {})
                    by_source[sid or "__unknown__"] = judge_result
                    per_judge["per_source"] = by_source
                    per_judge["aggregate"] = judge_result
                    job.setdefault("pipeline_evals", {})["judge"] = per_judge
                    try:
                        from sia.evals.quality_score import compute_quality_scores

                        quality = compute_quality_scores(
                            job,
                            judge_result=judge_result,
                            trace_steps=state.get("trace_steps", []),
                            tool_executions=job.get("tool_executions") or [],
                            llm_client=llm_client,
                            schema=schema.to_dict() if schema else None,
                            use_deepeval=True,
                        )
                        pe_runner.record_quality(job, quality, source_id=sid or "")
                    except Exception as q_exc:
                        logger.warning("Quality score computation failed: %s", q_exc)

            except Exception as e:
                logger.error(f"Judge failed in finalize_node: {e}")
                judge_result = {"error": f"Evaluation failed: {e}"}

        # Calculate overall confidence as minimum of all steps
        confidences = [step.get("confidence", 1.0) for step in state.get("trace_steps", [])]
        overall_confidence = min(confidences) if confidences else 1.0
        
        # Flag for review based on confidence or Judge verdict
        requires_review = state.get("requires_review", False)
        review_reason = state.get("review_reason", "")
        
        if judge_result and judge_result.get("verdict") in ["FAIL", "REVIEW"]:
            requires_review = True
            review_reason = f"Judge Verdict: {judge_result.get('verdict')}. {judge_result.get('critique')[:100]}..."
        
        if overall_confidence <= 0.6 and not requires_review:
            requires_review = True
            review_reason = f"Low confidence ({overall_confidence:.1%}) in processing steps."
        elif overall_confidence <= 0.9 and not requires_review:
            requires_review = True
            review_reason = f"Moderate confidence ({overall_confidence:.1%}) - review recommended."
            
        # Flag for review if max iterations reached and not flat
        if not state.get("is_flat", True) and state.get("iteration", 0) >= state.get("max_iterations", 3):
            requires_review = True
            review_reason = "Verification did not converge after maximum iterations"

        # region agent log
        _debug_log(
            "H6",
            "finalize_node review decision",
            {
                "job_id": state.get("job_id"),
                "overall_confidence": overall_confidence,
                "judge_verdict": None if not judge_result else judge_result.get("verdict"),
                "requires_review": requires_review,
                "review_reason": review_reason,
                "is_flat": state.get("is_flat", True),
                "iteration": state.get("iteration", 0),
                "max_iterations": state.get("max_iterations", 3),
            },
        )
        # endregion
            
        # Create output file if DataFrame is not empty
        output_file = None
        if df is not None and not df.empty:
            try:
                output_dir = str(Path(__file__).parent.parent.parent / "runtime" / "outputs")
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                
                # Create a unique filename based on the job or a UUID
                filename = f"sia_output_{uuid.uuid4().hex[:8]}.xlsx"
                output_file = os.path.join(output_dir, filename)
                
                # Save to Excel
                df.to_excel(output_file, index=False)
                logger.info(f"Saved final output to {output_file}")
            except Exception as e:
                logger.error(f"Failed to save Excel output: {e}")
                output_file = None

        return {
            "current_df": df,
            "final_schema": schema,
            "output_file": output_file,
            "requires_review": requires_review,
            "review_reason": review_reason,
            "judge_result": judge_result,
            "message": f"Finalized with {len(schema.fields)} fields. Conf: {overall_confidence:.1%}",
            "trace_steps": [{
                "step": "finalize",
                "fields": len(schema.fields),
                "confidence": overall_confidence,
                "output_saved": bool(output_file),
                "judge_verdict": judge_result.get("verdict") if judge_result else None
            }]
        }
    except Exception as e:
        logger.error(f"CRITICAL: Finalize node crashed: {e}")
        return {
            "requires_review": True,
            "review_reason": f"Finalization Error: {e}",
            "message": f"CRITICAL: Finalization error - check logs.",
            "trace_steps": [{
                "step": "finalize_crash",
                "error": str(e)
            }]
        }
