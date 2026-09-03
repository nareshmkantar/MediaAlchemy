"""
Plan Generator - LLM component that generates an extraction plan.
"""
import logging
import json
from typing import Any, Dict, List, Optional, Set, Tuple

from sia.agent.json_safe import dumps as json_safe_dumps
from .base import ExtractionPlan, load_prompt_from_file
from .llm_handler import LLMCallWrapper, RetryConfig, robust_json_parse, JSONParseError
from ..debug.llm_observer import get_observer
from ..tools.tool_validator import (
    validate_tool_call,
    validate_tool_sequence,
    get_destructive_tools,
    normalize_tool_name,
    sort_tool_calls_by_pipeline_stage,
)
from ..tools.pipeline_catalog import augment_system_prompt_with_catalog
from .context_packet import compute_date_granularity_alignment, effective_target_date_granularity
from .planner_decisions import (
    apply_resolved_approvals_to_plan,
    format_resolved_decisions_for_prompt,
)
from .target_template_utils import (
    allowed_post_transform_columns,
    build_template_contract,
    compute_column_gaps,
    final_output_column_order,
    final_output_sort_columns,
    is_no_match_target,
    normalize_target_template,
    pre_transform_target_columns,
    primary_target_columns,
    build_rename_mapping_from_approved_mappings,
    sanitize_rename_mapping,
    template_column_rules_summary,
    weekly_aggregate_group_by_columns,
)
from sia.context.planner_prompt import build_planner_prompt_result, context_available_columns
from sia.agent.value_scale import build_target_scales_from_mappings
from sia.agent.summary_row_signals import sheet_likely_has_summary_rows
from sia.tools.transformation_tools import TransformationTools

logger = logging.getLogger(__name__)

_LAYOUT_EXTRACT_TOOLS = frozenset(
    {
        "layout.extract",
        "layout.stack",
        "xls.layout.extract",
        "xls.layout.stack",
    }
)

_COLUMN_TYPING_BEFORE_EXPAND = frozenset(
    {
        "transform.rename",
        "transform.type_cast",
        "transform.add_column",
        "transform.apply_column_rules",
        "transform.scale_values",
    }
)

_DEFAULT_SUMMARY_FILTER_KEYWORDS = [
    "Subtotal",
    "SUBTOTAL",
    "Total",
    "Grand Total",
    "Totals",
]

KEEP_DECISIONS = {"keep", "approved", "primary", "supporting", "metadata", "context", "use as context"}


def _block_sparse_metric_entries(structure_analysis: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows from deterministic grid scan indicating metrics only on block parent rows."""
    if not isinstance(structure_analysis, dict):
        return []
    mls = structure_analysis.get("metric_layout_signals")
    if not isinstance(mls, dict):
        return []
    return [e for e in (mls.get("block_sparse_metrics") or []) if isinstance(e, dict)]


def finalize_extraction_plan(
    plan: Any,
    context_packet: Optional[Dict[str, Any]] = None,
    target_template: Optional[Dict[str, Any]] = None,
    structure_analysis: Optional[Dict[str, Any]] = None,
) -> Any:
    """Re-run deterministic plan post-processors (HITL approve, resume, tests)."""
    from sia.integrity.context_isolation import context_packet_source_id

    generator = PlanGenerator(llm_client=None)
    if isinstance(plan, dict):
        working = ExtractionPlan(
            tool_calls=list(plan.get("tool_calls") or []),
            expected_columns=list(plan.get("expected_columns") or []),
            business_rule_actions=list(
                plan.get("business_rule_actions") or plan.get("planned_rule_actions") or []
            ),
            approval_items=list(plan.get("approval_items") or []),
            confidence=float(plan.get("confidence") or 0.7),
            reasoning=str(plan.get("reasoning") or ""),
            requires_human_review=bool(plan.get("requires_human_review")),
            review_reason=str(plan.get("review_reason") or ""),
        )
        finalized = generator.finalize_plan(
            working, context_packet, target_template, structure_analysis
        )
        plan["tool_calls"] = finalized.tool_calls
        plan["expected_columns"] = finalized.expected_columns
        plan["business_rule_actions"] = finalized.business_rule_actions
        plan["approval_items"] = finalized.approval_items
        plan["reasoning"] = finalized.reasoning
        plan["confidence"] = finalized.confidence
        plan["requires_human_review"] = finalized.requires_human_review
        plan["review_reason"] = finalized.review_reason
        if context_packet_source_id(context_packet):
            plan["source_id"] = context_packet_source_id(context_packet)
        return plan
    return generator.finalize_plan(plan, context_packet, target_template, structure_analysis)


class PlanGenerator:
    """
    LLM component that generates an extraction plan.
    Takes structure analysis and creates actionable plan for tools.
    
    Enhanced with retry logic, robust parsing, and tool validation.
    """
    
    def __init__(self, llm_client, retry_config: RetryConfig = None):
        # Wrap LLM client with retry logic
        self.llm_wrapper = LLMCallWrapper(
            llm_client,
            retry_config=retry_config or RetryConfig(max_attempts=3)
        )
        self.llm_client = llm_client  # Keep for backward compat
        
        # Load prompt from external file
        self.SYSTEM_PROMPT, self.PROMPT_VERSION = load_prompt_from_file("plan_generator")
        if not self.SYSTEM_PROMPT:
            # Fallback to minimal prompt
            self.SYSTEM_PROMPT = "You are an expert data engineer creating extraction plans."
            self.PROMPT_VERSION = "0.0"

    @staticmethod
    def _job_from_context_packet(context_packet: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        cp = context_packet if isinstance(context_packet, dict) else {}
        job_id = str(cp.get("job_id") or "").strip()
        if not job_id:
            return None
        try:
            from sia.agent.job_manager import job_manager

            job = job_manager.get_job(job_id)
            return job if isinstance(job, dict) else None
        except Exception:
            return None

    def finalize_plan(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
        structure_analysis: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Deterministic post-processing applied after LLM parse (and on HITL resume)."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        plan = self._align_plan_to_approved_mappings(plan, context_packet, target_template)
        plan = self._ensure_rename_from_mappings(plan, context_packet)
        plan = self._ensure_value_scale_from_mappings(plan, context_packet)
        plan = self._collapse_plan_rename_tools(plan)
        plan = self._sanitize_plan_rename_tools(plan)
        plan = self._ensure_layout_stack_in_plan(plan, context_packet, structure_analysis)
        plan = self._ensure_filter_summaries_after_extract(
            plan, context_packet, target_template, structure_analysis
        )
        plan = self._prefer_expand_grouped_block(plan, context_packet, target_template, structure_analysis)
        plan = self._ensure_expand_after_column_typing(plan, context_packet)
        plan = self._ensure_expand_grouped_block_for_merged_layout(plan, context_packet)
        plan = self._ensure_sparse_dimension_layout_cleanup(
            plan, context_packet, target_template, structure_analysis
        )
        plan = self._inject_merged_metric_ranges_into_expand(plan, context_packet)
        plan = self._ensure_block_metric_allocation_approval_items(
            plan, context_packet, target_template, structure_analysis
        )
        plan = self._ensure_expand_weighted_spend_allocation(
            plan, context_packet, target_template
        )
        plan = self._adjust_drop_columns_for_block_metrics(plan, target_template, context_packet)
        plan = self._ensure_weekly_fill_forward(plan, context_packet, target_template)
        plan = self._sanitize_expand_grouped_block_params(plan, context_packet, target_template)
        plan = self._ensure_final_layout_tools(plan, target_template)
        plan = self._ensure_aggregate_weekly_group_by(plan, target_template)
        plan = self._finalize_expected_columns(plan, context_packet, target_template)
        plan = apply_resolved_approvals_to_plan(plan, context_packet)
        plan = self._finalize_merged_layout_expand_params(
            plan, context_packet, target_template
        )
        plan = self._reconcile_destructive_plan_review(plan)
        plan = self._rebind_source_local_literals(plan, context_packet)
        return plan

    def _rebind_source_local_literals(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Ensure add_column literals for channel/market/etc. match this source's local context."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        from sia.integrity.context_isolation import (
            context_packet_source_id,
            rebind_source_local_plan_literals,
        )

        rebound, actions = rebind_source_local_plan_literals(
            plan.tool_calls,
            context_packet,
            job=self._job_from_context_packet(context_packet),
        )
        if rebound:
            plan.tool_calls = rebound
        sid = context_packet_source_id(context_packet)
        if sid:
            plan.source_id = sid
            job = self._job_from_context_packet(context_packet)
            if job:
                from sia.context.diff_log import log_value_change

                log_value_change(
                    job,
                    stage="plan_generate",
                    source_id=sid,
                    field="plan.source_id",
                    before=None,
                    after=sid,
                    reason="plan bound to active source",
                )
        for note in actions[:6]:
            logger.info("Plan rebind (source-local): %s", note)
        return plan

    def _ensure_final_layout_tools(
        self,
        plan: ExtractionPlan,
        target_template: Optional[Dict[str, Any]],
    ) -> ExtractionPlan:
        """Insert reorder + sort before validation when a target template exists."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        tpl = normalize_target_template(target_template) if target_template else {}
        if not tpl:
            return plan

        column_order = final_output_column_order(tpl)
        sort_columns = final_output_sort_columns(tpl)
        if not column_order and not sort_columns:
            return plan

        tool_calls: List[Dict[str, Any]] = [
            dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)
        ]
        existing = {
            normalize_tool_name(str(t.get("tool") or "").strip())[0] for t in tool_calls
        }

        for tool in tool_calls:
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            params = dict(tool.get("params") or {})
            if norm == "transform.reorder_columns" and column_order:
                params["column_order"] = list(column_order)
                tool["params"] = params
            if norm == "transform.sort_rows" and sort_columns:
                params["sort_columns"] = list(sort_columns)
                params.setdefault("ascending", True)
                tool["params"] = params

        insert_at = len(tool_calls)
        for idx, tool in enumerate(tool_calls):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm in ("verify.schema", "verify.checksum"):
                insert_at = idx
                break

        to_insert: List[Dict[str, Any]] = []
        if column_order and "transform.reorder_columns" not in existing:
            to_insert.append(
                {
                    "tool": "transform.reorder_columns",
                    "params": {"column_order": list(column_order)},
                    "description": (
                        "Reorder columns to template order (date, uid, supporting, metrics)."
                    ),
                }
            )
        if sort_columns and "transform.sort_rows" not in existing:
            to_insert.append(
                {
                    "tool": "transform.sort_rows",
                    "params": {"sort_columns": list(sort_columns), "ascending": True},
                    "description": (
                        "Sort rows by uid hierarchy (date first, then remaining dimensions)."
                    ),
                }
            )
        if not to_insert:
            return plan

        for offset, step in enumerate(to_insert):
            tool_calls.insert(insert_at + offset, step)
        for step_idx, tool in enumerate(tool_calls, start=1):
            tool["step"] = step_idx
        plan.tool_calls = tool_calls
        note = "Inserted final layout tools (reorder_columns, sort_rows) before validation."
        if note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _ensure_aggregate_weekly_group_by(
        self,
        plan: ExtractionPlan,
        target_template: Optional[Dict[str, Any]],
    ) -> ExtractionPlan:
        """Ensure weekly rollup groups by uid + supporting dimensions, not channel alone."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        tpl = normalize_target_template(target_template) if target_template else {}
        if not tpl:
            return plan

        updated: List[Dict[str, Any]] = []
        changed = False
        for tool in list(plan.tool_calls or []):
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            norm, _ = normalize_tool_name(str(tool_copy.get("tool") or "").strip())
            if norm != "transform.aggregate_weekly":
                updated.append(tool_copy)
                continue
            params = dict(tool_copy.get("params") or {})
            date_col = params.get("date_col")
            expected = weekly_aggregate_group_by_columns(tpl, date_col=date_col)
            if not expected:
                from sia.agent.target_template_utils import sanitize_aggregate_weekly_params

                tool_copy["params"] = sanitize_aggregate_weekly_params(params)
                updated.append(tool_copy)
                continue
            existing = list(params.get("group_by_cols") or [])
            merged: List[str] = []
            seen: Set[str] = set()
            for col in existing + expected:
                name = str(col or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                merged.append(name)
            if merged != existing:
                params["group_by_cols"] = merged
                tool_copy["params"] = params
                changed = True
            from sia.agent.target_template_utils import sanitize_aggregate_weekly_params

            tool_copy["params"] = sanitize_aggregate_weekly_params(
                dict(tool_copy.get("params") or {})
            )
            updated.append(tool_copy)

        plan.tool_calls = updated
        if changed:
            note = (
                "Set aggregate_weekly group_by_cols to template uid hierarchy + supporting columns."
            )
            if note not in (plan.reasoning or ""):
                plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _sparse_dimension_column_names(
        self, context_packet: Optional[Dict[str, Any]]
    ) -> List[str]:
        source_summary = ((context_packet or {}).get("planning_summary") or {}).get(
            "source_summary"
        ) or {}
        sparse_dimension_columns = list(source_summary.get("sparse_dimension_columns") or [])
        names: List[str] = []
        for item in sparse_dimension_columns:
            if isinstance(item, dict):
                col = str(item.get("source_column") or item.get("column") or "").strip()
            else:
                col = str(item or "").strip()
            if col:
                names.append(col)
        return names

    def _sparse_block_start_column_names(
        self,
        context_packet: Optional[Dict[str, Any]],
        *,
        min_blank_ratio: float = 0.15,
    ) -> List[str]:
        """Columns sparse enough to delimit blocks (exclude dense cols like publisher on every row)."""
        source_summary = ((context_packet or {}).get("planning_summary") or {}).get(
            "source_summary"
        ) or {}
        sparse_dimension_columns = list(source_summary.get("sparse_dimension_columns") or [])
        names: List[str] = []
        for item in sparse_dimension_columns:
            if not isinstance(item, dict):
                continue
            col = str(item.get("source_column") or item.get("column") or "").strip()
            if not col:
                continue
            try:
                blank_ratio = float(item.get("blank_ratio") or 0.0)
            except (TypeError, ValueError):
                blank_ratio = 0.0
            if blank_ratio >= min_blank_ratio:
                names.append(col)
        return names

    def _summarized_or_sparse_layout_expand_warranted(
        self, structure_analysis: Optional[Dict[str, Any]]
    ) -> bool:
        """
        Only inject ``expand_grouped_block`` for sparse-dimension cleanup when deterministic
        signals say block-level metric rows exist. Otherwise use ``fill_merged`` on sparse
        dims only (flat tables with blank optional columns like clicks are not block-spend).
        """
        return bool(_block_sparse_metric_entries(structure_analysis))

    def _expand_step_needs_spend_allocation_review(self, tool_call: Dict[str, Any]) -> bool:
        params = tool_call.get("params") if isinstance(tool_call.get("params"), dict) else {}
        allocs = params.get("allocations")
        if isinstance(allocs, list) and any(isinstance(a, dict) for a in allocs):
            return True
        mmr = params.get("merged_metric_ranges")
        if isinstance(mmr, list) and mmr:
            return True
        return False

    def _plan_expand_needs_block_spend_allocation_hitl(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]],
        structure_analysis: Optional[Dict[str, Any]],
    ) -> bool:
        """Spare expand from optional-column sparsity must not trigger spend allocation HITL."""
        if self._context_merged_metric_ranges(context_packet):
            return True
        for m in _block_sparse_metric_entries(structure_analysis):
            if m.get("spend_like"):
                return True
        for t in plan.tool_calls or []:
            if not isinstance(t, dict):
                continue
            norm, _ = normalize_tool_name(str(t.get("tool") or "").strip())
            if norm != "transform.expand_grouped_block":
                continue
            if self._expand_step_needs_spend_allocation_review(t):
                return True
        return False

    def _layout_dimension_columns_for_expand(
        self,
        context_packet: Optional[Dict[str, Any]],
        target_template: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Uid / hierarchy columns to forward-fill within each block (may include dense parents)."""
        seen: Set[str] = set()
        ordered: List[str] = []
        tpl = normalize_target_template(target_template) if target_template else {}
        scope = tpl.get("x_scope") if isinstance(tpl.get("x_scope"), dict) else {}
        for col in list(scope.get("uid_hierarchy") or []) + list(
            scope.get("supporting_columns") or []
        ):
            name = str(col or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            ordered.append(name)
        src_to_tgt: Dict[str, str] = {}
        for item in list((context_packet or {}).get("approved_mappings") or []):
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision", "")).strip().lower()
            src = str(item.get("source_column") or "").strip()
            tgt = str(item.get("target_column") or "").strip()
            if decision not in KEEP_DECISIONS or not src or not tgt or tgt == "No match":
                continue
            src_to_tgt[src] = tgt
        for raw in self._sparse_dimension_column_names(context_packet):
            name = src_to_tgt.get(raw, raw)
            lk = name.lower()
            if lk in seen:
                continue
            seen.add(lk)
            ordered.append(name)
        return ordered

    def _resolve_default_weight_column_for_spend(
        self,
        context_packet: Optional[Dict[str, Any]],
        target_template: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Post-rename column to use as allocation weight (typically impressions)."""
        candidates: List[str] = []
        tpl = normalize_target_template(target_template) if target_template else {}
        scope = tpl.get("x_scope") if isinstance(tpl.get("x_scope"), dict) else {}
        for m in scope.get("metrics") or []:
            name = str(m or "").strip()
            if name and TransformationTools._weight_like_column_name(name):
                candidates.append(name)
        for item in (context_packet or {}).get("approved_mappings") or []:
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision", "")).strip().lower()
            if decision not in KEEP_DECISIONS:
                continue
            for key in ("target_column", "source_column"):
                col = str(item.get(key) or "").strip()
                if col and TransformationTools._weight_like_column_name(col):
                    candidates.append(col)
        seen: Set[str] = set()
        ordered: List[str] = []
        for c in candidates:
            lk = c.lower()
            if lk in seen:
                continue
            seen.add(lk)
            ordered.append(c)
        for c in ordered:
            if "impression" in c.lower():
                return c
        return ordered[0] if ordered else None

    def _ensure_expand_weighted_spend_allocation(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Use impression-like weights when expanding block-level spend metrics."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        weight_col = self._resolve_default_weight_column_for_spend(
            context_packet, target_template
        )
        if not weight_col:
            return plan

        changed = False
        updated: List[Dict[str, Any]] = []
        for tool in plan.tool_calls or []:
            if not isinstance(tool, dict):
                continue
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm != "transform.expand_grouped_block":
                updated.append(tool)
                continue

            tool_copy = dict(tool)
            params = dict(tool_copy.get("params") or {})
            allocations = params.get("allocations")
            if not isinstance(allocations, list):
                updated.append(tool_copy)
                continue

            new_allocations: List[Dict[str, Any]] = []
            for spec in allocations:
                if not isinstance(spec, dict):
                    continue
                spec_copy = dict(spec)
                metric_col = str(spec_copy.get("metric_col") or "").strip()
                if (
                    metric_col
                    and TransformationTools._spend_like_column_name(metric_col)
                    and str(spec_copy.get("method") or "equal").strip().lower() == "equal"
                ):
                    spec_copy["method"] = "by_weight"
                    spec_copy["weight_col"] = weight_col
                    changed = True
                new_allocations.append(spec_copy)

            params["allocations"] = new_allocations
            tool_copy["params"] = params
            updated.append(tool_copy)

        if changed:
            plan.tool_calls = updated
            note = (
                "Updated block-level spend allocation to use impression-like weights."
            )
            if note not in (plan.reasoning or ""):
                plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _context_merged_metric_ranges(
        self, context_packet: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        cp = context_packet or {}
        for key in ("merged_metric_ranges",):
            top = cp.get(key)
            if isinstance(top, list) and top:
                return list(top)
        supplement = cp.get("mapping_supplement") if isinstance(cp.get("mapping_supplement"), dict) else {}
        merged = list(supplement.get("merged_metric_ranges") or [])
        if merged:
            return merged
        planning = cp.get("planning_summary") if isinstance(cp.get("planning_summary"), dict) else {}
        for key in ("merged_metric_ranges",):
            if planning.get(key):
                return list(planning.get(key) or [])
        src = planning.get("source_summary") if isinstance(planning.get("source_summary"), dict) else {}
        return list(src.get("merged_metric_ranges") or [])

    def _inject_merged_metric_ranges_into_expand(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Attach Excel merged metric spans to expand_grouped_block when available."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        merged = self._context_merged_metric_ranges(context_packet)
        if not merged:
            return plan
        updated: List[Dict[str, Any]] = []
        for tool in list(plan.tool_calls or []):
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            norm, _ = normalize_tool_name(str(tool_copy.get("tool") or "").strip())
            if norm != "transform.expand_grouped_block":
                updated.append(tool_copy)
                continue
            params = dict(tool_copy.get("params") or {})
            params["merged_metric_ranges"] = merged
            params = self._remap_expand_params_to_template_names(params, context_packet)
            tool_copy["params"] = params
            updated.append(tool_copy)
        plan.tool_calls = updated
        return plan

    def _finalize_merged_layout_expand_params(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Last pass: template-named merged spans and sparse block_start on expand_grouped_block."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        merged = self._context_merged_metric_ranges(context_packet)
        if not merged:
            return plan
        sparse_bs = self._sparse_block_start_column_names(context_packet)
        if sparse_bs:
            sparse_bs = list(
                self._remap_expand_params_to_template_names(
                    {"block_start_columns": sparse_bs},
                    context_packet,
                ).get("block_start_columns")
                or sparse_bs
            )
        dim_defaults = self._layout_dimension_columns_for_expand(context_packet, target_template)
        merge_dims = self._merged_layout_dimension_columns(context_packet, merged)
        dim_union: List[str] = []
        seen_dim: Set[str] = set()
        for col in list(dim_defaults) + list(merge_dims):
            lk = str(col or "").strip().lower()
            if not lk or lk in seen_dim:
                continue
            seen_dim.add(lk)
            dim_union.append(str(col).strip())
        dim_defaults = dim_union
        updated: List[Dict[str, Any]] = []
        changed = False
        for tool in list(plan.tool_calls or []):
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            norm, _ = normalize_tool_name(str(tool_copy.get("tool") or "").strip())
            if norm != "transform.expand_grouped_block":
                updated.append(tool_copy)
                continue
            params = dict(tool_copy.get("params") or {})
            params["merged_metric_ranges"] = merged
            params = self._remap_expand_params_to_template_names(params, context_packet)
            bs = params.get("block_start_columns")
            bs_list = (
                [str(c).strip() for c in bs if c is not None and str(c).strip()]
                if isinstance(bs, list)
                else []
            )
            if sparse_bs and bs_list != sparse_bs:
                params["block_start_columns"] = list(sparse_bs)
                changed = True
            elif sparse_bs and not bs_list:
                params["block_start_columns"] = list(sparse_bs)
                changed = True
            dim_cols = params.get("dimension_columns")
            if not isinstance(dim_cols, list) or not [
                str(c).strip() for c in dim_cols if c is not None and str(c).strip()
            ]:
                if dim_defaults:
                    params["dimension_columns"] = list(dim_defaults)
                    changed = True
            tool_copy["params"] = params
            updated.append(tool_copy)
            changed = True
        if changed:
            plan.tool_calls = updated
            note = (
                "Finalized expand_grouped_block for Excel merged layout "
                "(merged_metric_ranges + sparse block_start_columns)."
            )
            if note not in (plan.reasoning or ""):
                plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _merged_layout_dimension_columns(
        self,
        context_packet: Optional[Dict[str, Any]],
        merged_ranges: List[Dict[str, Any]],
    ) -> List[str]:
        """Dimension columns to forward-fill when Excel vertical merges are present."""
        dims: List[str] = []
        seen: set = set()
        for col in self._sparse_dimension_column_names(context_packet):
            if col and col not in seen:
                seen.add(col)
                dims.append(col)
        for entry in merged_ranges or []:
            if not isinstance(entry, dict):
                continue
            cn = str(entry.get("column_name") or "").strip()
            if not cn or cn in seen:
                continue
            if TransformationTools._spend_like_column_name(cn):
                continue
            tlv = entry.get("top_left_value")
            if tlv is not None:
                try:
                    float(str(tlv).replace(",", "").strip())
                    continue
                except (TypeError, ValueError):
                    pass
            seen.add(cn)
            dims.append(cn)
        return dims

    def _ensure_expand_grouped_block_for_merged_layout(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Inject expand_grouped_block when workbook merged ranges exist but the plan has no layout tool."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        merged = self._context_merged_metric_ranges(context_packet)
        if not merged:
            return plan
        tools = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
        block_tools = {
            "transform.expand_grouped_block",
            "transform.fill_merged",
            "transform.allocate_block_metric",
        }
        if any(
            normalize_tool_name(str(t.get("tool") or "").strip())[0] in block_tools
            for t in tools
        ):
            return plan
        dim_cols = self._merged_layout_dimension_columns(context_packet, merged)
        if not dim_cols:
            dim_cols = [
                str(m.get("column_name") or "").strip()
                for m in merged
                if isinstance(m, dict) and str(m.get("column_name") or "").strip()
            ]
        if not dim_cols:
            return plan
        block_start = self._sparse_block_start_column_names(context_packet) or [
            c for c in dim_cols if c in self._sparse_dimension_column_names(context_packet)
        ]
        if not block_start and dim_cols:
            block_start = [dim_cols[0]]
        expand_step = {
            "tool": "transform.expand_grouped_block",
            "params": {
                "dimension_columns": dim_cols,
                "block_start_columns": block_start,
                "allocations": None,
                "auto_detect_block_metrics": True,
                "merged_metric_ranges": merged,
            },
            "description": (
                "Excel merged cells detected: forward-fill hierarchy dimensions within each block "
                "and split merged metric totals (not fill_merged on spend columns)."
            ),
        }
        expand_step["params"] = self._remap_expand_params_to_template_names(
            expand_step["params"],
            context_packet,
        )
        insert_at = self._insert_index_after_column_typing(tools)
        tools.insert(insert_at, expand_step)
        for step_idx, tool in enumerate(tools, start=1):
            tool["step"] = step_idx
        plan.tool_calls = tools
        note = (
            "Inserted transform.expand_grouped_block for Excel merged layout "
            "(dimensions ffilled; block metrics allocated from merged ranges)."
        )
        if note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _sanitize_expand_grouped_block_params(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Ensure block_start_columns are sparse delimiters only (not dense parent columns)."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        merged = self._context_merged_metric_ranges(context_packet)
        block_start = self._sparse_block_start_column_names(context_packet)
        dim_defaults = self._layout_dimension_columns_for_expand(context_packet, target_template)
        if not block_start and not dim_defaults and not merged:
            return plan
        updated: List[Dict[str, Any]] = []
        changed = False
        for tool in list(plan.tool_calls or []):
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            norm, _ = normalize_tool_name(str(tool_copy.get("tool") or "").strip())
            if norm != "transform.expand_grouped_block":
                updated.append(tool_copy)
                continue
            params = dict(tool_copy.get("params") or {})
            dim_cols = params.get("dimension_columns")
            if not isinstance(dim_cols, list) or not [
                str(c).strip() for c in dim_cols if c is not None and str(c).strip()
            ]:
                if dim_defaults:
                    params["dimension_columns"] = list(dim_defaults)
                    changed = True
            bs = params.get("block_start_columns")
            bs_list = (
                [str(c).strip() for c in bs if c is not None and str(c).strip()]
                if isinstance(bs, list)
                else []
            )
            sparse_bs = block_start or self._sparse_block_start_column_names(context_packet)
            if sparse_bs and bs_list != sparse_bs:
                params["block_start_columns"] = list(sparse_bs)
                changed = True
            elif sparse_bs and not bs_list:
                params["block_start_columns"] = list(sparse_bs)
                changed = True
            if merged:
                params["merged_metric_ranges"] = merged
                params = self._remap_expand_params_to_template_names(params, context_packet)
                changed = True
            tool_copy["params"] = params
            updated.append(tool_copy)
        if changed:
            plan.tool_calls = updated
            note = (
                "Adjusted expand_grouped_block: block_start_columns use sparse delimiters only "
                "(dense columns such as publisher on every row are not block keys)."
            )
            if note not in (plan.reasoning or ""):
                plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _ensure_sparse_dimension_layout_cleanup(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
        structure_analysis: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Inject sparse-dimension cleanup; reserve expand for true block-metric layouts."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        sparse_cols = self._sparse_dimension_column_names(context_packet)
        if not sparse_cols:
            return plan
        if self._context_merged_metric_ranges(context_packet):
            return plan
        block_tools = {
            "transform.expand_grouped_block",
            "transform.fill_merged",
            "transform.unmerge_and_fill",
        }
        tools = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
        if any(
            normalize_tool_name(str(t.get("tool") or "").strip())[0] in block_tools
            for t in tools
        ):
            return plan

        should_expand = self._summarized_or_sparse_layout_expand_warranted(
            structure_analysis
        )
        if should_expand:
            dim_cols = self._layout_dimension_columns_for_expand(context_packet, target_template)
            if not dim_cols:
                dim_cols = list(sparse_cols)
            block_start = self._sparse_block_start_column_names(context_packet) or list(sparse_cols)
            cleanup_step = {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": dim_cols,
                    "block_start_columns": block_start,
                    "allocations": None,
                    "auto_detect_block_metrics": True,
                },
                "description": (
                    "Sparse dimension columns + block-sparse metrics detected: forward-fill hierarchy "
                    "within each block and split block-level metrics."
                ),
            }
            cleanup_step["params"] = self._remap_expand_params_to_template_names(
                cleanup_step["params"],
                context_packet,
            )
            note = (
                "Inserted transform.expand_grouped_block for sparse dimension columns "
                "with block-sparse metric signals."
            )
        else:
            dim_cols = self._layout_dimension_columns_for_expand(context_packet, target_template)
            if not dim_cols:
                dim_cols = list(sparse_cols)
            cleanup_step = {
                "tool": "transform.fill_merged",
                "params": {
                    "direction": "down",
                    "columns": dim_cols,
                },
                "description": (
                    "Sparse dimensions detected without block-metric evidence: forward-fill only "
                    "dimension columns before downstream transforms."
                ),
            }
            note = (
                "Inserted transform.fill_merged for sparse dimensions "
                "(no block-level metric evidence)."
            )
        insert_at = self._insert_index_after_column_typing(tools)
        tools.insert(insert_at, cleanup_step)
        for step_idx, tool in enumerate(tools, start=1):
            tool["step"] = step_idx
        plan.tool_calls = tools
        if note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _dedupe_weight_column_options(
        self,
        weight_cols: List[str],
        context_packet: Optional[Dict[str, Any]],
    ) -> List[str]:
        """Prefer post-rename (target) names; drop raw prepared_columns that map to the same metric."""
        src_to_tgt: Dict[str, str] = {}
        for item in list((context_packet or {}).get("approved_mappings") or []):
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision", "")).strip().lower()
            src = str(item.get("source_column") or "").strip()
            tgt = str(item.get("target_column") or "").strip()
            if decision not in KEEP_DECISIONS or not src or not tgt or tgt == "No match":
                continue
            src_to_tgt[src] = tgt
        out: List[str] = []
        seen: Set[str] = set()
        for col in weight_cols:
            canonical = src_to_tgt.get(col, col)
            key = canonical.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(canonical)
        return out

    def _weight_column_candidates(
        self,
        context_packet: Optional[Dict[str, Any]],
        target_template: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        candidates: List[str] = []
        wc = self._resolve_default_weight_column_for_spend(context_packet, target_template)
        if wc:
            candidates.append(wc)
        supplement = (context_packet or {}).get("mapping_supplement") or {}
        for col in supplement.get("prepared_columns") or []:
            cname = str(col).strip()
            if cname and TransformationTools._weight_like_column_name(cname):
                if cname not in candidates:
                    candidates.append(cname)
        return candidates

    def _ensure_block_metric_allocation_approval_items(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
        structure_analysis: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """
        Add planner-review question for block/merged metric split method when needed.
        Analyst must choose equal vs by_weight (no automatic default to weighted).
        """
        if not isinstance(plan, ExtractionPlan):
            return plan
        merged = self._context_merged_metric_ranges(context_packet)
        has_expand = any(
            normalize_tool_name(str(t.get("tool") or "").strip())[0]
            == "transform.expand_grouped_block"
            for t in (plan.tool_calls or [])
            if isinstance(t, dict)
        )
        has_allocate = any(
            normalize_tool_name(str(t.get("tool") or "").strip())[0]
            == "transform.allocate_block_metric"
            for t in (plan.tool_calls or [])
            if isinstance(t, dict)
        )
        if not merged and not has_expand and not has_allocate:
            return plan
        if has_expand and not has_allocate and not merged and not self._plan_expand_needs_block_spend_allocation_hitl(
            plan, context_packet, structure_analysis
        ):
            return plan

        spend_targets: List[str] = []
        tpl = normalize_target_template(target_template) if target_template else {}
        scope = tpl.get("x_scope") if isinstance(tpl.get("x_scope"), dict) else {}
        for m in scope.get("metrics") or []:
            mname = str(m or "").strip()
            if mname and TransformationTools._spend_like_column_name(mname):
                spend_targets.append(mname)
        if not spend_targets:
            spend_targets = ["spends"]

        weight_cols = self._dedupe_weight_column_options(
            self._weight_column_candidates(context_packet, target_template),
            context_packet,
        )
        items = list(plan.approval_items or [])
        existing_targets = {
            str(it.get("target_column") or it.get("target") or "").strip().lower()
            for it in items
            if isinstance(it, dict)
        }

        for target_metric in spend_targets:
            if target_metric.lower() in existing_targets:
                continue
            options: List[Dict[str, str]] = [
                {
                    "id": "equal",
                    "label": (
                        f"Split {target_metric} equally across rows in each block "
                        "(or merged-cell range)."
                    ),
                },
            ]
            for wc in weight_cols:
                options.append(
                    {
                        "id": f"by_weight__{wc}",
                        "label": (
                            f"Split {target_metric} by weight using {wc!r} "
                            "(row_metric = block_total × row_weight / sum_weights)."
                        ),
                    }
                )
            if len(options) < 2:
                continue
            items.append(
                {
                    "target_column": target_metric,
                    "question": (
                        f"How should block-level {target_metric} be allocated to child rows "
                        f"when the source uses merged cells or sparse section headers?"
                    ),
                    "summary": f"Block metric allocation for {target_metric}",
                    "options": options,
                    "decision_type": "block_metric_allocation",
                }
            )
            existing_targets.add(target_metric.lower())

        if items != list(plan.approval_items or []):
            plan.approval_items = items
            plan.requires_human_review = True
            if not plan.review_reason:
                plan.review_reason = (
                    "Block-level metric allocation method must be confirmed in Decisions needed."
                )
        return plan

    def _template_protected_drop_names(
        self,
        target_template: Optional[Dict[str, Any]],
        context_packet: Optional[Dict[str, Any]] = None,
    ) -> Set[str]:
        """Lowercase names that must not appear in transform.drop_columns."""
        protected: Set[str] = set()
        if isinstance(target_template, dict):
            tpl = normalize_target_template(target_template)
            protected.update(str(c) for c in primary_target_columns(tpl))
            protected.update(str(c) for c in pre_transform_target_columns(tpl))
        for item in (context_packet or {}).get("approved_mappings") or []:
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision", "")).strip().lower()
            if decision not in KEEP_DECISIONS:
                continue
            tgt = str(item.get("target_column") or "").strip()
            if tgt and not is_no_match_target(tgt):
                protected.add(tgt)
        return {str(x).strip().lower() for x in protected if str(x).strip()}

    def _enrich_tool_calls_before_validation(
        self,
        tool_calls: List[Dict[str, Any]],
        context_packet: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Repair common LLM omissions so expand_grouped_block is not dropped at validation."""
        sparse_cols = self._sparse_dimension_column_names(context_packet)
        enriched: List[Dict[str, Any]] = []
        for tool in tool_calls or []:
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            norm, _ = normalize_tool_name(str(tool_copy.get("tool") or "").strip())
            if norm != "transform.expand_grouped_block":
                enriched.append(tool_copy)
                continue
            params = dict(tool_copy.get("params") or {})
            dim_cols = params.get("dimension_columns")
            if not isinstance(dim_cols, list) or not [
                str(c).strip() for c in dim_cols if c is not None and str(c).strip()
            ]:
                if sparse_cols:
                    params["dimension_columns"] = list(sparse_cols)
            sparse_bs = self._sparse_block_start_column_names(context_packet)
            block_cols = params.get("block_start_columns")
            if not isinstance(block_cols, list) or not [
                str(c).strip() for c in block_cols if c is not None and str(c).strip()
            ]:
                if sparse_bs:
                    params["block_start_columns"] = list(sparse_bs)
            elif sparse_bs:
                params["block_start_columns"] = list(sparse_bs)
            if params.get("allocations") is None and params.get("auto_detect_block_metrics") is None:
                params["auto_detect_block_metrics"] = True
            tool_copy["params"] = params
            enriched.append(tool_copy)
        return enriched

    def _align_plan_to_approved_mappings(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]],
        target_template: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        approved_mappings = list((context_packet or {}).get("approved_mappings") or [])
        if not approved_mappings or not isinstance(plan, ExtractionPlan):
            return plan

        target_to_source: Dict[str, str] = {}
        approved_targets = set()
        for item in approved_mappings:
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision", "")).strip().lower()
            source_col = str(item.get("source_column") or "").strip()
            target_col = str(item.get("target_column") or "").strip()
            if decision not in KEEP_DECISIONS or not source_col or not target_col or target_col == "No match":
                continue
            approved_targets.add(target_col)
            # Keep the first approved source per target to avoid conflicting rewrite rules.
            target_to_source.setdefault(target_col, source_col)

        if not target_to_source:
            return plan

        updated_tools: List[Dict[str, Any]] = []
        rewrite_notes: List[str] = []
        for tool in list(plan.tool_calls or []):
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            tool_name = str(tool_copy.get("tool") or "").strip().lower()
            params = dict(tool_copy.get("params") or {})
            if tool_name == "transform.rename" and isinstance(params.get("mapping"), dict):
                rewritten_mapping: Dict[str, Any] = {}
                for raw_source, raw_target in params.get("mapping", {}).items():
                    target = str(raw_target or "").strip()
                    if not target or is_no_match_target(target):
                        continue
                    approved_source = target_to_source.get(target)
                    if approved_source:
                        rewritten_mapping[approved_source] = target
                        if str(raw_source).strip() != approved_source:
                            rewrite_notes.append(f"{raw_source} -> {approved_source} for target {target}")
                    elif target in approved_targets:
                        rewritten_mapping[str(raw_source)] = target
                rewritten_mapping = sanitize_rename_mapping(rewritten_mapping)
                if not rewritten_mapping:
                    continue
                params["mapping"] = rewritten_mapping
                tool_copy["params"] = params
            updated_tools.append(tool_copy)

        plan.tool_calls = updated_tools
        if rewrite_notes:
            note = "Adjusted rename mappings to the approved semantic mapping source columns."
            if note not in plan.reasoning:
                plan.reasoning = f"{plan.reasoning} {note}".strip()
        plan = self._align_plan_params_to_context(plan, context_packet)
        return self._sanitize_plan_rename_tools(plan)

    def _ensure_layout_stack_in_plan(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        structure_analysis: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """
        Per-block runs use ``layout.extract`` only (blocks are collated like multi-sheet).
        Otherwise require explicit stack coordinates or fall back to extract.
        """
        if not isinstance(plan, ExtractionPlan):
            return plan
        from sia.agent.layout_stack_utils import (
            build_blocks_from_main_blocks,
            sanitize_layout_stack_params,
            should_prefer_stack_over_single_extract,
        )
        from sia.agent.multi_block_sheet import layout_extract_params_for_block

        tools = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
        if not tools:
            return plan

        scoped = {}
        cp = context_packet if isinstance(context_packet, dict) else {}
        if isinstance(cp.get("scoped_source"), dict):
            scoped = cp["scoped_source"]

        if cp.get("process_blocks_separately") or scoped.get("process_blocks_separately"):
            main_blocks = list(scoped.get("main_blocks") or (cp.get("approved_layout") or {}).get("main_blocks") or [])
            extract_params = (
                layout_extract_params_for_block(main_blocks[0], scoped)
                if main_blocks
                else self._default_layout_extract_params(scoped, structure_analysis)
            )
            new_tools: List[Dict[str, Any]] = []
            for tc in tools:
                norm, _ = normalize_tool_name(str(tc.get("tool") or "").strip())
                if norm == "layout.stack":
                    continue
                if norm == "layout.extract":
                    new_tools.append({**tc, "params": extract_params})
                else:
                    new_tools.append(tc)
            if not any(
                normalize_tool_name(str(t.get("tool") or "").strip())[0] == "layout.extract"
                for t in new_tools
            ):
                new_tools.insert(
                    0,
                    {
                        "tool": "layout.extract",
                        "params": extract_params,
                        "description": "Extract this main-data block (multi-block sheet; collate after all blocks)",
                    },
                )
            plan.tool_calls = new_tools
            plan.reasoning = (
                f"{plan.reasoning} [Planner] Multi-block sheet: layout.stack removed; "
                "layout.extract scoped to this block; frames union-collated after all blocks."
            ).strip()
            return plan

        prefer_stack = should_prefer_stack_over_single_extract(structure_analysis, scoped)

        changed = False
        for i, tc in enumerate(tools):
            norm, _ = normalize_tool_name(str(tc.get("tool") or "").strip())
            if norm != "layout.stack":
                continue
            params, _warns = sanitize_layout_stack_params(
                dict(tc.get("params") or {}),
                structure_analysis=structure_analysis,
                scoped_source=scoped,
                context_packet=cp,
            )
            blocks = params.get("blocks") if isinstance(params.get("blocks"), list) else []
            if len(blocks) < 2:
                if prefer_stack:
                    bounds = scoped.get("analysis_bounds") or {}
                    main_blocks = list(scoped.get("main_blocks") or [])
                    approved = cp.get("approved_layout") or {}
                    if isinstance(approved, dict) and not main_blocks:
                        main_blocks = list(approved.get("main_blocks") or [])
                    built = build_blocks_from_main_blocks(
                        main_blocks,
                        header_row=int(params.get("header_row") or scoped.get("header_row") or 0),
                        data_end_row=bounds.get("end_row") if isinstance(bounds, dict) else None,
                    )
                    if len(built) >= 2:
                        params["blocks"] = built
                        blocks = built
                if len(blocks) < 2:
                    extract_params = self._default_layout_extract_params(scoped, structure_analysis)
                    tools[i] = {
                        **tc,
                        "tool": "layout.extract",
                        "params": extract_params,
                        "description": (
                            tc.get("description")
                            or "Extract scoped table (stack skipped: block boundaries not verified)"
                        ),
                    }
                    changed = True
                    if prefer_stack:
                        plan.approval_items = list(plan.approval_items or [])
                        plan.approval_items.append(
                            {
                                "question": (
                                    "Side-by-side blocks were detected but stack coordinates were "
                                    "missing. The plan uses layout.extract on the scoped region instead. "
                                    "Should we add per-block column ranges and retry layout.stack?"
                                ),
                                "options": [
                                    {
                                        "id": "extract_only",
                                        "label": "Keep layout.extract on the approved scope (current plan)",
                                    },
                                    {
                                        "id": "define_blocks_stack",
                                        "label": "Define col_start/col_end per block and use layout.stack",
                                    },
                                ],
                                "summary": "Stack vs extract for repeating columns",
                            }
                        )
                        plan.requires_human_review = True
                    continue
            tools[i] = {**tc, "params": params}
            changed = True

        if changed:
            plan.tool_calls = tools
            note = "[Planner] layout.stack normalized to verified blocks or replaced with layout.extract."
            plan.reasoning = f"{plan.reasoning} {note}".strip()
        return plan

    @staticmethod
    def _default_layout_extract_params(
        scoped_source: Optional[Dict[str, Any]],
        structure_analysis: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build layout.extract coords from scoped analysis bounds or the first structure table."""
        scoped = scoped_source if isinstance(scoped_source, dict) else {}
        bounds = scoped.get("analysis_bounds") or {}
        if isinstance(bounds, dict) and bounds:
            return {
                "start_row": int(bounds.get("start_row", 0)),
                "end_row": int(bounds.get("end_row", bounds.get("start_row", 0))),
                "start_col": int(bounds.get("start_col", 0)),
                "end_col": int(bounds.get("end_col", bounds.get("start_col", 0))),
                "header_row": int(
                    scoped.get("header_row", bounds.get("start_row", 0))
                ),
            }
        tables = (structure_analysis or {}).get("tables") or []
        if tables and isinstance(tables[0], dict):
            coords = tables[0].get("coordinates", tables[0])
            if isinstance(coords, dict):
                block_top = int(coords.get("start_row", 0))
                return {
                    "start_row": block_top,
                    "end_row": int(coords.get("data_end_row", coords.get("end_row", block_top))),
                    "start_col": int(coords.get("col_start", coords.get("start_col", 0))),
                    "end_col": int(coords.get("col_end", coords.get("end_col", 0))),
                    "header_row": int(
                        coords.get("header_row")
                        or scoped.get("header_row")
                        or block_top
                    ),
                }
        return {"start_row": 0, "end_row": 0, "start_col": 0, "end_col": 0, "header_row": 0}

    def _insert_index_after_layout_extract(self, tools: List[Dict[str, Any]]) -> Optional[int]:
        """Index to insert flat-table cleanup (filter_summaries) immediately after extract/stack."""
        last_extract = -1
        for i, tool in enumerate(tools):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm in _LAYOUT_EXTRACT_TOOLS:
                last_extract = i
        return (last_extract + 1) if last_extract >= 0 else None

    def _ensure_filter_summaries_after_extract(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
        structure_analysis: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """
        Add or remove transform.filter_summaries after layout.extract/stack.

        Only keeps the step when structure analysis or a scoped preview suggests
        Total/Subtotal/Grand Total rows exist (avoids pointless per-sheet plan review).
        """
        if not isinstance(plan, ExtractionPlan):
            return plan
        tools = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
        if not tools:
            return plan

        likely_summaries = sheet_likely_has_summary_rows(structure_analysis, context_packet)
        kept: List[Dict[str, Any]] = []
        removed_filter = False
        for tool in tools:
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm == "transform.filter_summaries" and not likely_summaries:
                removed_filter = True
                continue
            kept.append(tool)

        if likely_summaries and not any(
            normalize_tool_name(str(t.get("tool") or "").strip())[0] == "transform.filter_summaries"
            for t in kept
        ):
            insert_at = self._insert_index_after_layout_extract(kept)
            if insert_at is not None:
                filter_step = {
                    "tool": "transform.filter_summaries",
                    "params": {
                        "keywords": list(_DEFAULT_SUMMARY_FILTER_KEYWORDS),
                        "use_structural_detection": True,
                    },
                    "description": (
                        "Remove Total/Subtotal/Grand Total rows after extract, before rename and "
                        "block metric tools (flat-table cleanup)."
                    ),
                }
                kept.insert(insert_at, filter_step)
                note = (
                    "Inserted transform.filter_summaries after layout extract for flat-table cleanup."
                )
                if note not in (plan.reasoning or ""):
                    plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()

        if removed_filter:
            prune_note = (
                "Omitted transform.filter_summaries — no Total/Subtotal rows detected on this sheet."
            )
            if prune_note not in (plan.reasoning or ""):
                plan.reasoning = f"{(plan.reasoning or '').strip()} {prune_note}".strip()

        if kept != tools:
            for step_idx, tool in enumerate(kept, start=1):
                tool["step"] = step_idx
            plan.tool_calls = kept
        return plan

    def _reconcile_destructive_plan_review(self, plan: ExtractionPlan) -> ExtractionPlan:
        """Drop plan-review pause when the only destructive step is a no-op filter_summaries."""
        if not isinstance(plan, ExtractionPlan):
            return plan

        destructive = get_destructive_tools(list(plan.tool_calls or []))
        norm_destructive = {
            normalize_tool_name(str(name or "").strip())[0] for name in destructive
        }
        destructive_only_filter = norm_destructive and norm_destructive <= {"transform.filter_summaries"}
        reason = str(plan.review_reason or "").strip()
        destructive_reason = reason.startswith("Plan includes destructive tools")

        if destructive_only_filter and destructive_reason:
            if plan.approval_items:
                plan.review_reason = (
                    f"Plan has {len(plan.approval_items)} approval items that need analyst review"
                )
                plan.requires_human_review = True
            elif plan.confidence >= 0.7:
                plan.requires_human_review = False
                plan.review_reason = ""
            else:
                plan.review_reason = (
                    f"Plan confidence ({plan.confidence:.1%}) is below threshold (70%)"
                )

        if not destructive and destructive_reason:
            if plan.approval_items:
                plan.review_reason = (
                    f"Plan has {len(plan.approval_items)} approval items that need analyst review"
                )
                plan.requires_human_review = True
            elif plan.confidence >= 0.7:
                plan.requires_human_review = False
                plan.review_reason = ""

        return plan

    def _insert_index_after_column_typing(self, tools: List[Dict[str, Any]]) -> int:
        """Index to insert expand_grouped_block: immediately after last rename/type_cast."""
        last_typing = -1
        for i, tool in enumerate(tools):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm in _COLUMN_TYPING_BEFORE_EXPAND:
                last_typing = i
        if last_typing >= 0:
            return last_typing + 1
        for i, tool in enumerate(tools):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm in _LAYOUT_EXTRACT_TOOLS:
                return i + 1
        return 0

    def _remap_expand_params_to_template_names(
        self,
        params: Dict[str, Any],
        context_packet: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Use post-rename (target) column names in expand params when mappings exist."""
        out = dict(params or {})
        src_to_tgt: Dict[str, str] = {}
        for item in list((context_packet or {}).get("approved_mappings") or []):
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision", "")).strip().lower()
            src = str(item.get("source_column") or "").strip()
            tgt = str(item.get("target_column") or "").strip()
            if decision not in KEEP_DECISIONS or not src or not tgt or tgt == "No match":
                continue
            src_to_tgt[src] = tgt

        def _map_names(names: Any) -> Any:
            if not isinstance(names, list):
                return names
            mapped: List[str] = []
            for n in names:
                key = str(n or "").strip()
                mapped.append(src_to_tgt.get(key, key))
            return mapped

        out["dimension_columns"] = _map_names(out.get("dimension_columns"))
        out["block_start_columns"] = _map_names(out.get("block_start_columns"))
        alloc_list = out.get("allocations")
        if isinstance(alloc_list, list):
            new_allocs = []
            for spec in alloc_list:
                if not isinstance(spec, dict):
                    continue
                spec_copy = dict(spec)
                for key in ("metric_col", "weight_col"):
                    raw = spec_copy.get(key)
                    if raw is not None:
                        s = str(raw).strip()
                        spec_copy[key] = src_to_tgt.get(s, s)
                new_allocs.append(spec_copy)
            out["allocations"] = new_allocs
        mmr = out.get("merged_metric_ranges")
        if isinstance(mmr, list) and mmr:
            remapped_ranges: List[Dict[str, Any]] = []
            for spec in mmr:
                if not isinstance(spec, dict):
                    continue
                spec_copy = dict(spec)
                for key in ("column_name", "metric_col"):
                    raw = spec_copy.get(key)
                    if raw is not None:
                        s = str(raw).strip()
                        spec_copy[key] = src_to_tgt.get(s, s)
                remapped_ranges.append(spec_copy)
            out["merged_metric_ranges"] = remapped_ranges
        return out

    def _ensure_expand_after_column_typing(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Move expand_grouped_block to after rename/type_cast (template column names)."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        tools = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
        expand_i: Optional[int] = None
        for i, tool in enumerate(tools):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm == "transform.expand_grouped_block":
                expand_i = i
                break
        if expand_i is None:
            return plan
        insert_at = self._insert_index_after_column_typing(tools)
        tool = tools.pop(expand_i)
        if expand_i < insert_at:
            insert_at -= 1
        params = self._remap_expand_params_to_template_names(
            dict(tool.get("params") or {}),
            context_packet,
        )
        tool["params"] = params
        tools.insert(insert_at, tool)
        for step_idx, t in enumerate(tools, start=1):
            t["step"] = step_idx
        plan.tool_calls = tools
        return plan

    def _prefer_expand_grouped_block(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
        structure_analysis: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """
        Replace separate ``fill_merged`` + ``allocate_block_metric`` with one
        ``expand_grouped_block`` so dimensions are forward-filled inside each block
        before block totals are split (sparse headers preserved for segmentation).
        """
        if not isinstance(plan, ExtractionPlan):
            return plan

        tools = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
        alloc_idxs: List[int] = []
        fill_idxs: List[int] = []
        for i, tool in enumerate(tools):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm == "transform.allocate_block_metric":
                alloc_idxs.append(i)
            elif norm == "transform.fill_merged":
                fill_idxs.append(i)
            elif norm == "transform.expand_grouped_block":
                return plan

        has_expand = False
        for t in tools:
            norm, _ = normalize_tool_name(str(t.get("tool") or "").strip())
            if norm == "transform.expand_grouped_block":
                has_expand = True
                break

        if not alloc_idxs:
            if not fill_idxs or has_expand:
                return plan
            if not self._context_merged_metric_ranges(context_packet) and not self._summarized_or_sparse_layout_expand_warranted(structure_analysis):
                return plan
            dim_cols: List[str] = []
            for fi in fill_idxs:
                params = tools[fi].get("params") if isinstance(tools[fi].get("params"), dict) else {}
                cols = params.get("columns")
                if isinstance(cols, list):
                    for c in cols:
                        if c is not None and str(c).strip():
                            dim_cols.append(str(c).strip())
            if not dim_cols:
                dim_cols = self._sparse_dimension_column_names(context_packet)
            if not dim_cols:
                return plan
            expand_step = {
                "tool": "transform.expand_grouped_block",
                "params": {
                    "dimension_columns": dim_cols,
                    "block_start_columns": dim_cols,
                    "allocations": None,
                    "auto_detect_block_metrics": True,
                },
                "description": (
                    "Forward-fill sparse dimensions within each block and split block-level metrics "
                    "(replaces dimension-only fill_merged)."
                ),
            }
            expand_step["params"] = self._remap_expand_params_to_template_names(
                expand_step["params"],
                context_packet,
            )
            merged_only = self._context_merged_metric_ranges(context_packet)
            if merged_only:
                expand_step["params"]["merged_metric_ranges"] = merged_only
            new_tools = [t for i, t in enumerate(tools) if i not in set(fill_idxs)]
            insert_at = self._insert_index_after_column_typing(new_tools)
            new_tools.insert(insert_at, expand_step)
            for step_idx, tool in enumerate(new_tools, start=1):
                tool["step"] = step_idx
            plan.tool_calls = new_tools
            note = (
                "Replaced transform.fill_merged with transform.expand_grouped_block "
                "for grouped influencer layout."
            )
            if note not in (plan.reasoning or ""):
                plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
            return plan

        dim_cols: List[str] = []
        for fi in fill_idxs:
            params = tools[fi].get("params") if isinstance(tools[fi].get("params"), dict) else {}
            cols = params.get("columns")
            if isinstance(cols, list):
                for c in cols:
                    if c is not None and str(c).strip():
                        dim_cols.append(str(c).strip())

        allocations: List[Dict[str, Any]] = []
        block_start: List[str] = []
        for ai in alloc_idxs:
            params = tools[ai].get("params") if isinstance(tools[ai].get("params"), dict) else {}
            mc = params.get("metric_col")
            if not mc:
                continue
            method = str(params.get("method") or "equal").strip().lower()
            if method not in ("equal", "by_weight"):
                method = "equal"
            spec: Dict[str, Any] = {
                "metric_col": mc,
                "method": method,
            }
            wc = params.get("weight_col")
            if wc and method == "by_weight":
                spec["weight_col"] = wc
            allocations.append(spec)
            if not block_start and isinstance(params.get("block_start_columns"), list):
                block_start = [str(c) for c in params["block_start_columns"] if c]

        if not block_start:
            block_start = list(dim_cols)
        if not dim_cols:
            dim_cols = list(block_start)

        expand_step = {
            "tool": "transform.expand_grouped_block",
            "params": {
                "dimension_columns": dim_cols,
                "block_start_columns": block_start or dim_cols,
                "allocations": allocations,
                "auto_detect_block_metrics": not allocations,
            },
            "description": (
                "Forward-fill sparse dimensions within each block, then allocate block-level metrics."
            ),
        }

        remove = set(alloc_idxs) | set(fill_idxs)
        new_tools = [t for i, t in enumerate(tools) if i not in remove]
        insert_at = self._insert_index_after_column_typing(new_tools)
        expand_step["params"] = self._remap_expand_params_to_template_names(
            expand_step["params"],
            context_packet,
        )
        merged = self._context_merged_metric_ranges(context_packet)
        if merged:
            expand_step["params"]["merged_metric_ranges"] = merged
        new_tools.insert(insert_at, expand_step)
        for step_idx, tool in enumerate(new_tools, start=1):
            tool["step"] = step_idx
        plan.tool_calls = new_tools
        note = (
            "Consolidated fill_merged + allocate_block_metric into transform.expand_grouped_block."
        )
        if note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _build_rename_mapping_from_context(
        self,
        context_packet: Optional[Dict[str, Any]],
    ) -> Dict[str, str]:
        """Physical source column name → approved template target (for transform.rename)."""
        mapping = build_rename_mapping_from_approved_mappings(
            list((context_packet or {}).get("approved_mappings") or [])
        )

        gaps = (context_packet or {}).get("column_gaps")
        if isinstance(gaps, dict):
            for pair in gaps.get("rename_needed") or []:
                if not isinstance(pair, dict):
                    continue
                src = str(pair.get("from") or "").strip()
                tgt = str(pair.get("to") or "").strip()
                if src and tgt and src != tgt and not is_no_match_target(tgt):
                    mapping.setdefault(src, tgt)
        return sanitize_rename_mapping(mapping)

    def _collect_block_start_columns_from_plan(
        self, tool_calls: List[Dict[str, Any]]
    ) -> Set[str]:
        protected: Set[str] = set()
        for tool in tool_calls or []:
            if not isinstance(tool, dict):
                continue
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm not in (
                "transform.classify_metric_level",
                "transform.allocate_block_metric",
                "transform.infer_block_boundary_columns",
            ):
                continue
            params = tool.get("params") if isinstance(tool.get("params"), dict) else {}
            for key in ("block_start_columns", "columns"):
                raw = params.get(key)
                if isinstance(raw, list):
                    for col in raw:
                        name = str(col or "").strip()
                        if name:
                            protected.add(name)
        return protected

    def _adjust_drop_columns_for_block_metrics(
        self,
        plan: ExtractionPlan,
        target_template: Optional[Dict[str, Any]] = None,
        context_packet: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """
        Do not drop block delimiter columns before classify/allocate; defer drop_columns
        to after block metric tools when present. Never drop template primary / mapped targets.
        """
        if not isinstance(plan, ExtractionPlan):
            return plan
        tool_calls = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
        if not tool_calls:
            return plan

        protected = self._collect_block_start_columns_from_plan(tool_calls)
        template_protected = self._template_protected_drop_names(target_template, context_packet)
        has_block_tools = False
        for t in tool_calls:
            norm, _ = normalize_tool_name(str(t.get("tool") or "").strip())
            if norm in (
                "transform.classify_metric_level",
                "transform.allocate_block_metric",
                "transform.expand_grouped_block",
                "transform.infer_block_boundary_columns",
            ):
                has_block_tools = True
                break

        updated: List[Dict[str, Any]] = []
        drop_steps: List[Dict[str, Any]] = []
        other_steps: List[Dict[str, Any]] = []

        for tool in tool_calls:
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm == "transform.drop_columns":
                params = dict(tool.get("params") or {})
                cols = params.get("columns")
                if isinstance(cols, list):
                    block_protected = {str(c).strip() for c in protected if str(c).strip()}
                    filtered = [
                        str(c)
                        for c in cols
                        if str(c).strip()
                        and str(c).strip() not in block_protected
                        and str(c).strip().lower() not in template_protected
                    ]
                    if not filtered:
                        continue
                    params["columns"] = filtered
                    tool = {**tool, "params": params}
                drop_steps.append(tool)
            else:
                other_steps.append(tool)

        if not drop_steps:
            plan.tool_calls = other_steps
            return plan

        if not has_block_tools:
            plan.tool_calls = other_steps + drop_steps
            return plan

        # Insert drop_columns after the last block-metric tool in the non-drop sequence.
        insert_at = len(other_steps)
        for idx, tool in enumerate(other_steps):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm in (
                "transform.classify_metric_level",
                "transform.allocate_block_metric",
                "transform.expand_grouped_block",
                "transform.infer_block_boundary_columns",
            ):
                insert_at = idx + 1

        merged = list(other_steps)
        for drop_tool in drop_steps:
            merged.insert(insert_at, drop_tool)
            insert_at += 1

        for step_idx, tool in enumerate(merged, start=1):
            tool["step"] = step_idx
        plan.tool_calls = merged
        note = (
            "Deferred transform.drop_columns until after block metric tools and "
            f"protected delimiter column(s): {', '.join(sorted(protected)[:8])}."
        )
        if protected and note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        if template_protected:
            tpl_note = (
                "Protected template/mapped columns from drop_columns: "
                + ", ".join(sorted(template_protected)[:12])
                + ("..." if len(template_protected) > 12 else "")
            )
            if tpl_note not in (plan.reasoning or ""):
                plan.reasoning = f"{(plan.reasoning or '').strip()} {tpl_note}".strip()
        return plan

    def _collapse_plan_rename_tools(self, plan: ExtractionPlan) -> ExtractionPlan:
        """Merge multiple ``transform.rename`` steps into one (after the first layout extract)."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        tool_calls: List[Dict[str, Any]] = [
            dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)
        ]
        rename_count = 0
        merged_mapping: Dict[str, str] = {}
        for tool in tool_calls:
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm != "transform.rename":
                continue
            rename_count += 1
            params = dict(tool.get("params") or {})
            merged_mapping.update(sanitize_rename_mapping(params.get("mapping")))
        if rename_count <= 1:
            return plan

        without_rename: List[Dict[str, Any]] = []
        for tool in tool_calls:
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm != "transform.rename":
                without_rename.append(tool)

        insert_at = 0
        for idx, tool in enumerate(without_rename):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm in _LAYOUT_EXTRACT_TOOLS:
                insert_at = idx + 1

        merged_step = {
            "tool": "transform.rename",
            "params": {"mapping": merged_mapping},
            "description": "Apply approved column mappings (source → template names).",
        }
        without_rename.insert(insert_at, merged_step)

        for step_idx, tool in enumerate(without_rename, start=1):
            tool["step"] = step_idx
        plan.tool_calls = without_rename
        note = (
            f"Collapsed {rename_count} transform.rename step(s) into one mapping "
            f"({len(merged_mapping)} column(s))."
        )
        if note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _sanitize_plan_rename_tools(self, plan: ExtractionPlan) -> ExtractionPlan:
        """Remove ``No match`` rename targets from every transform.rename in the plan."""
        if not isinstance(plan, ExtractionPlan):
            return plan
        updated: List[Dict[str, Any]] = []
        for tool in list(plan.tool_calls or []):
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            norm, _ = normalize_tool_name(str(tool_copy.get("tool") or "").strip())
            if norm == "transform.rename":
                params = dict(tool_copy.get("params") or {})
                cleaned = sanitize_rename_mapping(params.get("mapping"))
                if not cleaned:
                    continue
                params["mapping"] = cleaned
                tool_copy["params"] = params
            updated.append(tool_copy)
        plan.tool_calls = updated
        return plan

    def _ensure_rename_from_mappings(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]],
    ) -> ExtractionPlan:
        """
        Insert ``transform.rename`` immediately after the first layout extract/stack when
        approved mappings (or ``column_gaps.rename_needed``) require physical renames.
        """
        if not isinstance(plan, ExtractionPlan):
            return plan

        rename_mapping = self._build_rename_mapping_from_context(context_packet)
        if not rename_mapping:
            return plan

        tool_calls: List[Dict[str, Any]] = [
            dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)
        ]
        if not tool_calls:
            tool_calls = [
                {
                    "tool": "transform.rename",
                    "params": {"mapping": dict(rename_mapping)},
                    "description": "Apply approved column mappings (source → template names).",
                }
            ]
            plan.tool_calls = tool_calls
            note = (
                f"Inserted transform.rename for {len(rename_mapping)} approved mapping(s) "
                "(no layout step in plan)."
            )
            if note not in (plan.reasoning or ""):
                plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
            return plan

        insert_at = 0
        for idx, tool in enumerate(tool_calls):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm in _LAYOUT_EXTRACT_TOOLS:
                insert_at = idx + 1
                break

        existing = tool_calls[insert_at] if insert_at < len(tool_calls) else None
        existing_norm = ""
        if isinstance(existing, dict):
            existing_norm, _ = normalize_tool_name(str(existing.get("tool") or "").strip())

        if existing_norm == "transform.rename":
            params = dict(existing.get("params") or {})
            merged = sanitize_rename_mapping(params.get("mapping"))
            merged.update(rename_mapping)
            params["mapping"] = merged
            existing["params"] = params
            note = f"Merged {len(rename_mapping)} approved rename(s) into transform.rename after layout."
        else:
            rename_step = {
                "tool": "transform.rename",
                "params": {"mapping": dict(rename_mapping)},
                "description": "Apply approved column mappings (source → template names) after extraction.",
            }
            tool_calls.insert(insert_at, rename_step)
            note = (
                f"Inserted transform.rename after layout for {len(rename_mapping)} approved mapping(s): "
                + ", ".join(f"{k!r}→{v!r}" for k, v in list(rename_mapping.items())[:6])
                + ("..." if len(rename_mapping) > 6 else "")
            )

        for step_idx, tool in enumerate(tool_calls, start=1):
            tool["step"] = step_idx
        plan.tool_calls = tool_calls
        plan = self._sanitize_plan_rename_tools(plan)
        if note and note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _plan_already_scales_column(self, tools: List[Dict[str, Any]], column: str) -> bool:
        """True if calculate/scale_values already adjusts this target column."""
        col_l = str(column or "").strip().lower()
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            params = dict(tool.get("params") or {})
            if norm == "transform.scale_values":
                scales = params.get("scales") or params.get("columns") or {}
                if isinstance(scales, dict):
                    for key in scales:
                        if str(key).strip().lower() == col_l:
                            return True
            if norm == "transform.calculate":
                target = str(params.get("target_column") or "").strip().lower()
                expr = str(params.get("expression") or "").lower()
                if target == col_l and ("* 1000" in expr or "*1000" in expr):
                    return True
        return False

    def _ensure_value_scale_from_mappings(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]],
    ) -> ExtractionPlan:
        """Insert ``transform.scale_values`` after rename/type_cast when mappings carry header scale."""
        if not isinstance(plan, ExtractionPlan):
            return plan

        scales = build_target_scales_from_mappings(
            list((context_packet or {}).get("approved_mappings") or [])
        )
        if not scales:
            return plan

        tool_calls: List[Dict[str, Any]] = [
            dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)
        ]
        pending = {
            col: factor
            for col, factor in scales.items()
            if not self._plan_already_scales_column(tool_calls, col)
        }
        if not pending:
            return plan

        insert_at = self._insert_index_after_column_typing(tool_calls)
        existing = tool_calls[insert_at] if insert_at < len(tool_calls) else None
        existing_norm = ""
        if isinstance(existing, dict):
            existing_norm, _ = normalize_tool_name(str(existing.get("tool") or "").strip())

        note = ""
        if existing_norm == "transform.scale_values":
            params = dict(existing.get("params") or {})
            merged = dict(params.get("scales") or params.get("columns") or {})
            merged.update(pending)
            params["scales"] = merged
            existing["params"] = params
            note = f"Merged value-scale factors for {len(pending)} column(s) into transform.scale_values."
        else:
            scale_step = {
                "tool": "transform.scale_values",
                "params": {"scales": dict(pending)},
                "description": "Apply header denomination factors from approved mappings (e.g. thousands).",
            }
            tool_calls.insert(insert_at, scale_step)
            parts = [f"{c}×{f:g}" for c, f in list(pending.items())[:6]]
            note = (
                f"Inserted transform.scale_values for header denomination: {', '.join(parts)}"
                + ("..." if len(pending) > 6 else "")
                + "."
            )

        for step_idx, tool in enumerate(tool_calls, start=1):
            tool["step"] = step_idx
        plan.tool_calls = tool_calls
        if note and note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
        return plan

    def _context_available_columns(self, context_packet: Optional[Dict[str, Any]]) -> Tuple[List[str], Dict[str, str]]:
        return context_available_columns(context_packet)

    def _resolve_plan_column_name(
        self,
        column: Any,
        alias_lookup: Dict[str, str],
        available_columns: List[str],
    ) -> Optional[str]:
        name = str(column or "").strip()
        if not name:
            return None
        resolved = alias_lookup.get(name, name)
        if resolved in available_columns:
            return resolved
        return None

    def _align_plan_params_to_context(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]],
    ) -> ExtractionPlan:
        if not isinstance(plan, ExtractionPlan):
            return plan

        available_columns, alias_lookup = self._context_available_columns(context_packet)
        if not available_columns:
            return plan

        weekly_tools = {
            "transform.aggregate_weekly",
            "transform.expand_period_to_daily",
            "transform.infer_granularity_expand_to_daily",
            "transform.date_range_to_weekly",
        }

        updated_tools: List[Dict[str, Any]] = []
        notes: List[str] = []

        for tool in list(plan.tool_calls or []):
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            tool_name = str(tool_copy.get("tool") or "").strip().lower()
            params = dict(tool_copy.get("params") or {})

            if tool_name in weekly_tools:
                for scalar_key in ("date_col", "start_date_col", "end_date_col"):
                    if scalar_key in params:
                        resolved_name = self._resolve_plan_column_name(params.get(scalar_key), alias_lookup, available_columns)
                        if resolved_name:
                            if str(params.get(scalar_key)).strip() != resolved_name:
                                notes.append(f"{tool_name}:{scalar_key} -> {resolved_name}")
                            params[scalar_key] = resolved_name

                for list_key in ("group_by_cols", "id_cols", "value_cols"):
                    if isinstance(params.get(list_key), list):
                        resolved_items: List[str] = []
                        for raw_name in params.get(list_key) or []:
                            resolved_name = self._resolve_plan_column_name(raw_name, alias_lookup, available_columns)
                            if resolved_name and resolved_name not in resolved_items:
                                if str(raw_name).strip() != resolved_name:
                                    notes.append(f"{tool_name}:{list_key} {raw_name} -> {resolved_name}")
                                resolved_items.append(resolved_name)
                        params[list_key] = resolved_items

                if isinstance(params.get("metric_rules"), dict):
                    resolved_rules: Dict[str, Any] = {}
                    for raw_name, raw_rule in (params.get("metric_rules") or {}).items():
                        resolved_name = self._resolve_plan_column_name(raw_name, alias_lookup, available_columns)
                        if not resolved_name:
                            notes.append(f"{tool_name}: dropped unresolved metric {raw_name}")
                            continue
                        resolved_rules[resolved_name] = raw_rule
                        if str(raw_name).strip() != resolved_name:
                            notes.append(f"{tool_name}:metric_rules {raw_name} -> {resolved_name}")
                    params["metric_rules"] = resolved_rules

                tool_copy["params"] = params

            updated_tools.append(tool_copy)

        plan.tool_calls = updated_tools
        if notes:
            note = "Aligned weekly/date tool params to the approved mapping context."
            if note not in plan.reasoning:
                plan.reasoning = f"{plan.reasoning} {note}".strip()
        return plan

    def _ensure_weekly_fill_forward(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]],
        target_template: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        if not isinstance(plan, ExtractionPlan):
            return plan

        template_scope = (target_template or {}).get("x_scope") if isinstance(target_template, dict) else {}
        eff_scope = template_scope if isinstance(template_scope, dict) else {}
        target_granularity = str(effective_target_date_granularity(eff_scope) or "").strip().lower()
        if target_granularity not in {"weekly", "week"}:
            return plan

        source_summary = ((context_packet or {}).get("planning_summary") or {}).get("source_summary") or {}
        sparse_dimension_columns = list(source_summary.get("sparse_dimension_columns") or [])
        sparse_columns = [
            str(item.get("source_column") or "").strip()
            for item in sparse_dimension_columns
            if isinstance(item, dict) and str(item.get("source_column") or "").strip()
        ]
        if not sparse_columns:
            return plan

        tool_names = [
            normalize_tool_name(str((tool or {}).get("tool") or "").strip())[0]
            for tool in (plan.tool_calls or [])
            if isinstance(tool, dict)
        ]
        if "transform.fill_merged" in tool_names:
            return plan
        if "transform.expand_grouped_block" in tool_names:
            return plan

        weekly_idx = next((idx for idx, name in enumerate(tool_names) if name == "transform.aggregate_weekly"), None)
        if weekly_idx is None:
            return plan

        fill_step = {
            "tool": "transform.fill_merged",
            "params": {
                "direction": "down",
                "columns": sparse_columns,
            },
            "description": "Forward-fill sparse dimension columns before weekly aggregation so context columns stay populated within each block.",
        }
        tool_calls = list(plan.tool_calls or [])
        tool_calls.insert(weekly_idx, fill_step)
        for idx, tool in enumerate(tool_calls, start=1):
            if isinstance(tool, dict):
                tool["step"] = idx
        plan.tool_calls = tool_calls
        note = "Added fill-forward for sparse dimension columns before weekly aggregation."
        if note not in plan.reasoning:
            plan.reasoning = f"{plan.reasoning} {note}".strip()
        return plan

    def _finalize_expected_columns(
        self,
        plan: ExtractionPlan,
        context_packet: Optional[Dict[str, Any]],
        target_template: Optional[Dict[str, Any]],
    ) -> ExtractionPlan:
        """Reconcile LLM `expected_schema` with template + approved mappings.

        The planner prompt asks the model for `expected_schema`, but models often
        omit supporting / metadata-mapped columns even though they remain in the
        final dataframe (and in `verify.schema`). We deterministically union the
        model output with the same allowlist used by `prune_dataframe_to_template`,
        ordered by `pre_transform_target_columns` when possible.
        """
        if not isinstance(plan, ExtractionPlan):
            return plan

        llm_cols = [str(c).strip() for c in (plan.expected_columns or []) if str(c).strip()]
        if not isinstance(target_template, dict) or not (target_template.get("properties") or {}):
            plan.expected_columns = llm_cols
            return plan

        template = normalize_target_template(target_template)
        approved_mappings = list((context_packet or {}).get("approved_mappings") or [])
        business_rules = list((context_packet or {}).get("business_rules") or [])
        allowed = allowed_post_transform_columns(
            template,
            approved_mappings=approved_mappings,
            business_rules=business_rules,
        )
        if not allowed:
            plan.expected_columns = llm_cols
            return plan

        lower_to_canon = {str(k).lower(): str(k) for k in allowed if k}

        def _canon(name: str) -> Optional[str]:
            key = str(name).strip().lower()
            return lower_to_canon.get(key)

        merged: List[str] = []
        seen_lower = set()

        for raw in llm_cols:
            canon = _canon(raw)
            if not canon:
                continue
            lk = canon.lower()
            if lk in seen_lower:
                continue
            seen_lower.add(lk)
            merged.append(canon)

        for col in pre_transform_target_columns(template):
            canon = _canon(col)
            if not canon:
                continue
            lk = canon.lower()
            if lk in seen_lower:
                continue
            seen_lower.add(lk)
            merged.append(canon)

        remainder = sorted(
            (c for c in allowed if _canon(c) and c.lower() not in seen_lower),
            key=str.lower,
        )
        for canon in remainder:
            lk = canon.lower()
            if lk in seen_lower:
                continue
            seen_lower.add(lk)
            merged.append(canon)

        plan.expected_columns = merged
        return plan
    
    def generate(
        self,
        structure_analysis: Dict,
        examples: List[Dict] = None,
        target_template: Dict = None,
        context_packet: Dict = None,
    ) -> ExtractionPlan:
        """
        Generate extraction plan with tool calls from structure analysis.
        
        Args:
            structure_analysis: Output from StructureAnalyzer
            examples: Similar examples from RAG
            target_template: Optional JSON Schema for target output alignment
            context_packet: Optional metadata packet with approved mappings and rules
        
        Returns:
            ExtractionPlan with tool_calls sequence and confidence
        """
        observer = get_observer()
            
        context_summary = (context_packet or {}).get("planning_summary", {})
        if not isinstance(context_summary, dict):
            context_summary = {}
        approved_mapping_summary = context_summary.get("mapping_summary", {}) or {}
        rules_summary = list(context_summary.get("rules_summary") or [])

        prompt_result = build_planner_prompt_result(
            structure_analysis,
            context_packet,
            target_template,
            examples=examples,
        )
        user_prompt = prompt_result["user_prompt"]
        context_briefing = prompt_result.get("briefing") or {}

        if observer:
            return self._generate_with_observer(
                observer,
                user_prompt,
                context_briefing,
                structure_analysis,
                context_packet,
                    target_template,
                examples,
                approved_mapping_summary,
                rules_summary,
            )

        full_prompt = f"{augment_system_prompt_with_catalog(self.SYSTEM_PROMPT)}\n\n{user_prompt}"
        try:
            response = self.llm_wrapper.generate_content(full_prompt)
            response_text = response.text
        except Exception as e:
            logger.error(f"PlanGenerator LLM call failed: {e}")
            response_text = ""

        plan = self._parse_plan(
            response_text, structure_analysis, context_packet=context_packet
        )
        plan.raw_tool_calls = [
            dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)
        ]
        return self.finalize_plan(
            plan, context_packet, target_template, structure_analysis
        )

    def _generate_with_observer(
        self,
        observer,
        user_prompt: str,
        context_briefing: Dict[str, Any],
        structure_analysis: Dict,
        context_packet: Optional[Dict[str, Any]],
        target_template: Optional[Dict[str, Any]],
        examples: Optional[List[Dict]],
        approved_mapping_summary: Dict[str, Any],
        rules_summary: List[str],
    ) -> ExtractionPlan:
        with observer.trace("plan_generator") as t:
            t.system_prompt = self.SYSTEM_PROMPT
            t.user_prompt = user_prompt
            t.full_prompt = f"{augment_system_prompt_with_catalog(self.SYSTEM_PROMPT)}\n\n{user_prompt}"
            t.prompt_version = self.PROMPT_VERSION
            t.retrieved_examples = examples or []
            t.input_context = {
                "tables_count": len(structure_analysis.get("tables", [])),
                "columns_count": len(structure_analysis.get("column_analysis", [])),
                "approved_mappings_count": len(approved_mapping_summary.get("mapped_columns", [])),
                "business_rules_count": len(rules_summary),
                "context_briefing": context_briefing,
            }

            try:
                gen_config = {"max_output_tokens": 8192, "temperature": 0.0}
                if hasattr(self.llm_client, "generation_config") and self.llm_client.generation_config:
                    client_config = self.llm_client.generation_config
                    if isinstance(client_config, dict):
                        if client_config.get("max_output_tokens", 0) > gen_config["max_output_tokens"]:
                            gen_config["max_output_tokens"] = client_config["max_output_tokens"]
                        if "temperature" in client_config:
                            gen_config["temperature"] = client_config["temperature"]

                response = self.llm_wrapper.generate_content(t.full_prompt, generation_config=gen_config)
                response_text = response.text
                t.raw_response = response_text

                if hasattr(self.llm_client, "model_name"):
                    t.model_id = self.llm_client.model_name.replace("models/", "")

                logger.info(
                    "Captured LLM response for plan_generator: %s chars",
                    len(response_text),
                )
            except Exception as e:
                logger.error(f"PlanGenerator unexpected error: {e}")
                t.error_message = str(e)
                t.success = False
                if not t.raw_response:
                    t.raw_response = f"Unexpected Error: {e}"
                response = type("obj", (object,), {"text": ""})()

            try:
                plan = self._parse_plan(
                    response.text, structure_analysis, context_packet=context_packet
                )
                plan.raw_tool_calls = [
                    dict(item) for item in (plan.tool_calls or []) if isinstance(item, dict)
                ]
                plan = self.finalize_plan(
                    plan, context_packet, target_template, structure_analysis
                )
                t.success = True
            except Exception as e:
                logger.error(f"Failed to parse plan: {e}")
                plan = ExtractionPlan(
                    tool_calls=[],
                    confidence=0.0,
                    reasoning=f"Parsing Error: {str(e)}",
                    requires_human_review=True,
                    review_reason="AI failed to generate a valid plan JSON.",
                )
                t.success = False
                t.error_message = str(e)

            t.parsed_output = {
                "tool_calls": plan.tool_calls,
                "business_rule_actions": plan.business_rule_actions,
                "approval_items": plan.approval_items,
                "expected_columns": plan.expected_columns,
                "confidence": plan.confidence,
                "reasoning": plan.reasoning,
            }
            t.confidence_score = float(plan.confidence)
            return plan
    
    def _parse_plan(
        self,
        response_text: str,
        fallback_analysis: Dict,
        context_packet: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Parse LLM response into ExtractionPlan with tool calls and validation."""
        try:
            # Use robust JSON parser
            try:
                data = robust_json_parse(response_text)
            except Exception as parse_err:
                # Log the raw response that failed to parse for debugging
                with open("failed_plan_response.log", "w", encoding="utf-8") as f:
                    f.write(response_text)
                logger.error(f"Failed to parse LLM plan response. Raw saved to failed_plan_response.log")
                raise parse_err
            
            # CRITICAL FIX: Handle case where robust_json_parse returns a string (double encoded JSON)
            if isinstance(data, str):
                try:
                    import json
                    data = json.loads(data)
                except:
                    # If it's still a string and not JSON, it might be raw text explanation.
                    # We can't do much with it.
                    logger.error(f"Plan parsed as string, not JSON/Dict. Content: {data[:100]}...")
                    raise ValueError(f"LLM returned raw string, not JSON object.")

            # Handle list response (LLM returned list of tools directly)
            if isinstance(data, list):
                logger.warning("LLM returned a list of tools directly (not JSON object). wrapping it.")
                data = {
                    "tool_calls": data,
                    "confidence": 0.5, # Default confidence for malformed but usable response
                    "reasoning": "Parsed from direct tool list"
                }

            # Final safety check
            if not isinstance(data, dict):
                 raise ValueError(f"Parsed plan data is {type(data)}, expected dict. Content: {str(data)[:100]}")

            # Log parsed confidence for debugging
            extracted_conf = data.get("confidence", data.get("overall_confidence"))
            logger.info(f"Extracted confidence from JSON: {extracted_conf} (type: {type(extracted_conf)})")
            
            from .llm_handler import safe_get
            
            # Extract tool_calls or create from blocks with normalization
            tool_calls = safe_get(data, "tool_calls", list)
            if tool_calls is None:
                # Key normalization for common LLM deviants
                for key in ["steps", "actions", "tasks", "plan_steps", "transformations"]:
                    if key in data and isinstance(data[key], list):
                        logger.info(f"Normalizing '{key}' to 'tool_calls'")
                        tool_calls = data[key]
                        break
            
            if tool_calls is None:
                tool_calls = []
                
            if not tool_calls and data.get("blocks"):
                # Convert old-style blocks to tool_calls
                for block in data.get("blocks", []):
                    tool_calls.append({
                        "step": len(tool_calls) + 1,
                        "tool": "layout.extract",
                        "params": {
                            "start_row": safe_get(block, "start_row", int, 0),
                            "end_row": safe_get(block, "end_row", int, 1000), 
                            "start_col": safe_get(block, "start_col", int, 0),
                            "end_col": safe_get(block, "end_col", int, 20),
                            "header_row": safe_get(block, "header_row", int, 0)
                        },
                        "description": "Extract data block"
                    })
            
            tool_calls = self._enrich_tool_calls_before_validation(
                list(tool_calls or []), context_packet
            )

            # Validate and normalize tool calls
            validated_tools, validation_errors = validate_tool_sequence(tool_calls)
            if validation_errors:
                logger.warning(f"Tool validation errors: {validation_errors}")
            valid_tools = [t for t in validated_tools if isinstance(t, dict) and t.get("_valid", True)]
            dropped_invalid = [
                t for t in validated_tools
                if isinstance(t, dict) and t.get("_valid") is False
            ]
            if dropped_invalid:
                dropped_names = [
                    str(t.get("tool") or "?") for t in dropped_invalid if isinstance(t, dict)
                ]
                logger.warning(
                    "Dropped %s invalid tool call(s) at parse: %s",
                    len(dropped_invalid),
                    dropped_names,
                )
            if valid_tools:
                valid_tools = sort_tool_calls_by_pipeline_stage(valid_tools)
            if tool_calls and not valid_tools:
                raise ValueError(
                    "Plan contained tool calls but none were valid after validation. "
                    f"Validation errors: {validation_errors}"
                )
            
            # Check for destructive tools that require HITL
            destructive = get_destructive_tools(validated_tools)
            requires_review = safe_get(data, "requires_human_review", bool, False)
            review_reason = safe_get(data, "review_reason", str, "")
            
            if destructive:
                requires_review = True
                review_reason = f"Plan includes destructive tools: {', '.join(destructive)}"
            
            # Extract confidence safely
            confidence = safe_get(data, "confidence", float) or \
                         safe_get(data, "overall_confidence", float) or \
                         safe_get(data, "score", float, 0.7)
            
            # Ensure it's not exactly 0.0 unless intended
            if confidence == 0 and tool_calls:
                confidence = 0.7
            
            reasoning = safe_get(data, "reasoning", str, "")
            if dropped_invalid:
                drop_note = (
                    f"Planner omitted {len(dropped_invalid)} invalid tool call(s) after validation: "
                    f"{', '.join(dropped_names)}."
                )
                reasoning = f"{reasoning} {drop_note}".strip()

            return ExtractionPlan(
                blocks=data.get("blocks", []),
                tool_calls=valid_tools,
                column_mappings=safe_get(data, "column_mappings", list, []),
                business_rule_actions=safe_get(data, "business_rule_actions", list, []),
                approval_items=safe_get(data, "approval_items", list, []),
                expected_columns=safe_get(data, "expected_schema", list) or safe_get(data, "expected_columns", list, []),
                confidence=confidence,
                reasoning=reasoning,
                requires_human_review=requires_review,
                review_reason=review_reason,
                raw_analysis=response_text
            )
            
        except JSONParseError as e:
            logger.error(f"JSON parse error after all strategies: {e}")
            raise ValueError(f"LLM Plan Parsing Failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error parsing plan: {e}")
            raise ValueError(f"LLM Plan Parsing Failed: {e}")
