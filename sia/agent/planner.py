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
    }
)

_DEFAULT_SUMMARY_FILTER_KEYWORDS = [
    "Subtotal",
    "SUBTOTAL",
    "Total",
    "Grand Total",
    "Totals",
]

_TEMPLATE_ENRICHMENT_TOOL_ORDER = """
## Fixed enrichment tool order (target template jobs)
Emit explicit tool calls in this order; skip steps that do not apply to this workbook:
1. **Extract / reshape**: `layout.extract` or `layout.stack` only (no `fill_merged` before rename when using expand — see step 4)
2. **Flat-table cleanup (required when sheet may have subtotals)**: `transform.filter_summaries` immediately after extract — removes Total/Subtotal/Grand Total rows **before** rename, merge fill, or block expand (avoids double-counting).
3. **Align names & types**: `transform.rename`, `transform.type_cast` (required before expand so `impressions`, `spends`, `channel` exist)
4. **Grouped blocks (preferred)**: `transform.expand_grouped_block` **after rename/type_cast** — segment on sparse headers, forward-fill **dimensions only**, split block-level metrics. Replaces `fill_merged` + `allocate_block_metric` on the same layout.
5. **Template UID defaults (explicit)**: `transform.apply_column_rules` or `transform.add_column` for each entry in **Column gaps → suggested_add_columns** — never `transform.calculate` for literals
6. **Grain / calendar**: expand/infer/date-range tools when date_granularity alignment requires them
7. **`transform.drop_columns`**: only for columns **not** in `block_start_columns` and **not** needed for allocate — run **after** block metric tools when step 4 ran; never drop `Name`/`Ref`/section ids before `allocate_block_metric`
8. **Rollup**: `transform.aggregate_weekly` when target is weekly (with explicit `metric_rules` for every template metric)
9. **Final layout**: `transform.reorder_columns` then `transform.sort_rows` (template column order + uid sort) immediately before verify
10. **Verify**: `verify.schema` as the final step

Every enrichment must appear as a tool call in the plan. Do not assume hidden executor behavior.
"""

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
        plan = self._collapse_plan_rename_tools(plan)
        plan = self._sanitize_plan_rename_tools(plan)
        plan = self._ensure_layout_stack_in_plan(plan, context_packet, structure_analysis)
        plan = self._ensure_filter_summaries_after_extract(plan, context_packet, target_template)
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
    ) -> ExtractionPlan:
        """
        Inject transform.filter_summaries right after layout.extract/stack so Subtotal/Total
        rows are removed before rename, fill_merged, expand_grouped_block, or weekly rollup.
        """
        if not isinstance(plan, ExtractionPlan):
            return plan
        tools = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
        if not tools:
            return plan
        if any(
            normalize_tool_name(str(t.get("tool") or "").strip())[0]
            == "transform.filter_summaries"
            for t in tools
        ):
            return plan
        insert_at = self._insert_index_after_layout_extract(tools)
        if insert_at is None:
            return plan
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
        tools.insert(insert_at, filter_step)
        for step_idx, tool in enumerate(tools, start=1):
            tool["step"] = step_idx
        plan.tool_calls = tools
        note = (
            "Inserted transform.filter_summaries after layout extract for flat-table cleanup."
        )
        if note not in (plan.reasoning or ""):
            plan.reasoning = f"{(plan.reasoning or '').strip()} {note}".strip()
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

    def _context_available_columns(self, context_packet: Optional[Dict[str, Any]]) -> Tuple[List[str], Dict[str, str]]:
        approved_mappings = list((context_packet or {}).get("approved_mappings") or [])
        business_rules = list((context_packet or {}).get("business_rules") or [])
        source_summary = ((context_packet or {}).get("planning_summary") or {}).get("source_summary") or {}
        prepared_columns = [str(col) for col in (source_summary.get("prepared_columns") or []) if str(col).strip()]

        discard_columns = {
            str(item.get("source_column") or "").strip()
            for item in approved_mappings
            if isinstance(item, dict) and str(item.get("decision", "")).strip().lower() == "discard"
        }
        keep_decisions = {"keep", "approved", "primary", "supporting", "metadata", "context", "use as context"}

        available_columns: List[str] = [col for col in prepared_columns if col and col not in discard_columns]
        alias_lookup: Dict[str, str] = {}

        for item in approved_mappings:
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision", "")).strip().lower()
            source_col = str(item.get("source_column") or "").strip()
            target_col = str(item.get("target_column") or "").strip()
            if decision not in keep_decisions or not source_col:
                continue

            resolved_name = source_col
            if target_col and target_col != "No match":
                resolved_name = target_col
                if source_col in available_columns:
                    available_columns = [resolved_name if col == source_col else col for col in available_columns]
                elif resolved_name not in available_columns:
                    available_columns.append(resolved_name)
            elif source_col not in available_columns:
                available_columns.append(source_col)

            for alias in {source_col, target_col, resolved_name}:
                if alias and alias != "No match":
                    alias_lookup[alias] = resolved_name

        for rule in business_rules:
            if not isinstance(rule, dict):
                continue
            dest = str(rule.get("target_column") or rule.get("destination_column") or "").strip()
            if dest and dest not in available_columns:
                available_columns.append(dest)
            if dest:
                alias_lookup[dest] = dest

        deduped_columns: List[str] = []
        seen = set()
        for col in available_columns:
            if col and col not in seen:
                seen.add(col)
                deduped_columns.append(col)
        return deduped_columns, alias_lookup

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
        
        # Build examples text
        examples_text = ""
        if examples:
            examples_text = "Similar examples from database:\n" + \
                           "\n".join([f"- {e.get('description', 'Example')}" for e in examples[:3]])
        
        # Prepare column analysis list safely
        col_analysis = structure_analysis.get('column_analysis', [])
        if isinstance(col_analysis, dict):
            # If LLM returned a dict, convert values to list
            col_analysis = list(col_analysis.values())
        elif not isinstance(col_analysis, list):
            col_analysis = []
        
        # Build tables summary for multi-block awareness
        tables = structure_analysis.get('tables', [])
        tables_summary = ""
        if len(tables) > 1:
            tables_summary = f"""
## IMPORTANT: Multiple Tables Detected ({len(tables)} tables)
This spreadsheet contains {len(tables)} separate data blocks with similar schema placed side-by-side.
You MUST use the `merge_blocks` tool to combine them into a single dataset.

Tables detected:
"""
            for i, t in enumerate(tables):
                if isinstance(t, dict):
                    coords = t.get('coordinates', t)
                    label = t.get('label', f'Table {i+1}')
                    c_start = coords.get('col_start', coords.get('start_col', 0))
                    c_end = coords.get('col_end', coords.get('end_col', 0))
                    tables_summary += f"- {label}: cols {c_start}-{c_end}\n"
        elif len(tables) == 1:
            t = tables[0]
            if isinstance(t, dict):
                coords = t.get('coordinates', t)
                label = t.get('label', 'Main data')
                r_start = coords.get('data_start_row', coords.get('start_row', 0))
                r_end = coords.get('data_end_row', coords.get('end_row', 0))
                c_start = coords.get('col_start', coords.get('start_col', 0))
                c_end = coords.get('col_end', coords.get('end_col', 0))
                tables_summary = f"""
## Single Table Detected
- {label}: rows {r_start}-{r_end}, cols {c_start}-{c_end}
"""
        
        # Build a SUMMARIZED structure analysis to avoid overloading the LLM context
        summarized_analysis = {
            "overall_structure": structure_analysis.get("overall_structure", "unknown"),
            "confidence": structure_analysis.get("confidence", 0),
            "tables": [
                {
                    "label": t.get("label", f"Table_{i}") if isinstance(t, dict) else f"Table_{i}",
                    "coordinates": t.get("coordinates", {}) if isinstance(t, dict) else {},
                    "table_shape": t.get("table_shape", "flat") if isinstance(t, dict) else "flat",
                    "column_pattern": t.get("column_pattern", {}) if isinstance(t, dict) else {} # RESTORE: Crucial for stacking
                }
                for i, t in enumerate(structure_analysis.get("tables", []))
            ],
            "visual_patterns": structure_analysis.get("visual_patterns", {}), # RESTORE: Crucial for physical anchoring
            "column_summary": [
                {"col": c.get("col"), "name": c.get("name", f"col_{c.get('col')}"), "type": c.get("type", "unknown"), "is_blank": c.get("is_blank", False)}
                for c in (col_analysis[:15] if len(col_analysis) > 15 else col_analysis)  # Limit to 15 columns
                if isinstance(c, dict)
            ],
            "reasoning": structure_analysis.get("reasoning", "")[:500]  # Truncate reasoning
        }
        
        # Identify if unpivot is likely needed (temporal columns present)
        temporal_cols = [
            c.get('name', f"col_{c.get('col')}") 
            for c in col_analysis 
            if isinstance(c, dict) and c.get('type') == 'temporal'
        ]
        unpivot_hint = ""
        has_repetitions = any(
            (t.get('column_pattern', {}).get('repetitions', 1) > 1) 
            for t in (structure_analysis.get('tables') or [])
            if isinstance(t, dict)
        )
        
        if temporal_cols and not has_repetitions:
            unpivot_hint = f"\n**Note:** Temporal columns detected ({', '.join(temporal_cols[:5])}). Consider using `transform.unpivot` to melt these into rows.\n"
        elif has_repetitions:
            unpivot_hint = (
                "\n**Note:** Repeating column pattern detected (side-by-side blocks). "
                "Use `layout.stack` only with explicit per-block `col_start`/`col_end` from demarcation "
                "or structure `tables`, after headers align across blocks. Do NOT stack the full sheet "
                "width as one block — extract each block separately if boundaries are unclear.\n"
            )
            
        context_summary = (context_packet or {}).get("planning_summary", {})
        approved_mapping_summary = context_summary.get("mapping_summary", {})
        layout_summary = context_summary.get("layout_summary", {})
        context_block_snippets = list((context_packet or {}).get("context_block_snippets") or [])
        interpreted_context = dict(context_summary.get("interpreted_context") or {})
        rules_summary = context_summary.get("rules_summary", [])
        source_summary = context_summary.get("source_summary", {})
        notes_summary = context_summary.get("user_notes", [])
        excluded_columns = approved_mapping_summary.get("excluded_columns", [])

        # Create user prompt with SUMMARIZED structure analysis
        cols_context_list = [
            c.get('name', f"col_{c.get('col', i)}") 
            for i, c in enumerate(col_analysis[:8])
            if isinstance(c, dict)
        ]
        user_prompt = f"""Given this spreadsheet structure analysis, create a sequence of tool calls to transform the data into a flat, normalized table.

## Structure Analysis (Summary)
{json_safe_dumps(summarized_analysis, indent=2)}
{unpivot_hint}
{examples_text}
{tables_summary}

## Important Context
- Columns include: {', '.join(cols_context_list)}
- Overall structure: {structure_analysis.get('overall_structure', 'Unknown')}

## Your Task
Create a JSON response with this priority order:
1. `confidence` - Your confidence in this plan (0.0 to 1.0)
2. `tool_calls` - Ordered list of transformations to apply
3. `business_rule_actions` - Explicit actions for defaults, formatting, fill-blank rules, or cross-field checks
4. `approval_items` - **Sparse** human decisions only when the plan is ambiguous or confidence is weaker: default to `[]`. Prefer objects with `question` + `options` (2–4 `{{id,label}}` choices that pick a branch). Avoid long lists of Yes/No trivia—see system prompt **4A** (max ~3 items, no routine column-definition checks).
5. `expected_schema` - The column names you expect in the final output (list of strings)

Respond with JSON only, no markdown formatting."""

        if source_summary:
            uid = source_summary.get("uid") or []
            aggregation_logic = source_summary.get("aggregation_logic") or ""
            prepared_columns = source_summary.get("prepared_columns") or []
            header_derivation = source_summary.get("header_derivation") or {}
            sparse_dimension_columns = source_summary.get("sparse_dimension_columns") or []
            user_prompt += f"""

## Source Metadata
- File: {source_summary.get('file_name')}
- Sheet: {source_summary.get('sheet_name')}
- Variable type: {source_summary.get('variable_type') or source_summary.get('source_type') or 'not specified'}
- Modeling period start: {source_summary.get('modeling_period_start') or 'not specified'}
- Modeling period end: {source_summary.get('modeling_period_end') or 'not specified'}
- UID: {', '.join(uid) if isinstance(uid, list) and uid else 'not specified'}
- Date granularity: {source_summary.get('date_granularity') or 'not specified'}
- Aggregation logic: {aggregation_logic or 'not specified'}
"""
            if prepared_columns:
                user_prompt += "\n- Prepared columns after scoped/header preprocessing: " + ", ".join(prepared_columns[:20]) + "\n"
            if isinstance(header_derivation, dict) and header_derivation.get("derived"):
                user_prompt += f"- Header derivation: {header_derivation.get('message') or 'multi-row headers were merged before mapping/planning'}\n"
            if sparse_dimension_columns:
                sparse_text = ", ".join(
                    f"{item.get('source_column')} ({round(float(item.get('blank_ratio') or 0.0) * 100)}% blank)"
                    for item in sparse_dimension_columns[:6]
                    if isinstance(item, dict) and item.get("source_column")
                )
                if sparse_text:
                    user_prompt += f"- Sparse dimension columns detected: {sparse_text}\n"

        gran_align = (context_summary or {}).get("date_granularity_alignment") or {}
        if not gran_align.get("planner_obligation") and isinstance(target_template, dict):
            xs = target_template.get("x_scope")
            xs = xs if isinstance(xs, dict) else {}
            gran_align = compute_date_granularity_alignment(
                str((source_summary or {}).get("date_granularity") or ""),
                effective_target_date_granularity(xs),
                source_date_shape=str((source_summary or {}).get("date_shape") or ""),
            )
        if gran_align.get("planner_obligation"):
            rec = gran_align.get("recommended_primary_tool") or ""
            rec_line = f"\n- **Preferred tool path (after union when deferred)**: {rec}" if rec else ""
            defer_union = bool((context_packet or {}).get("defer_weekly_rollups_to_post_union"))
            if defer_union:
                user_prompt += f"""

## Date granularity (multi-source batch — per-source plan only)
- Source vs template: **{gran_align.get('source_date_granularity') or gran_align.get('normalized_source') or 'not specified'}** → **{gran_align.get('target_date_granularity') or gran_align.get('normalized_target') or 'not specified'}**.
- **Collation duplicate_check runs on the stacked frame before any deferred grain tools.** Include required grain tools (`transform.aggregate_weekly`, `infer_granularity_expand_to_daily`, `expand_period_to_daily`, `date_range_to_weekly`, …) in `tool_calls` when the final combined output needs them; the executor will defer them until after duplicate_check. On **this workbook alone**:
  - **Do not** rely on these grain tools having executed before per-source verification; they are post-collate intent, not per-source execution.
  - **Do not** use `transform.rename` to fabricate a `week_start` column before that column exists.
  - Keep the mapped **physical** date column (often `date` or `calendar_date`); type_cast/format as needed.
  - **Still required per source (not deferred):** `transform.filter_summaries`, then `transform.expand_grouped_block` or dimension-only `transform.fill_merged` when sparse/merged layout columns exist.{rec_line}
"""
            else:
                user_prompt += f"""

## Date granularity alignment (REQUIRED — honor in tool_calls)
Analysts set **source** grain in Guided Setup; the template sets **target** grain. Your plan must satisfy the obligation (do not skip rollup solely because structure analysis looks “flat enough”).
- **Source (job / Guided Setup)**: {gran_align.get('source_date_granularity') or gran_align.get('normalized_source') or 'not specified'}
- **Target (template x_scope)**: {gran_align.get('target_date_granularity') or gran_align.get('normalized_target') or 'not specified'}
- **Planner obligation**: {gran_align.get('planner_obligation')}{rec_line}
"""

        obs_cols = (source_summary or {}).get("date_column_observations") or []
        obs_summary = (source_summary or {}).get("date_cadence_summary") or ""
        obs_mismatch = (source_summary or {}).get("date_granularity_mismatch_note") or ""
        obs_agg_conf = (source_summary or {}).get("date_cadence_aggregate_confidence")
        if obs_cols or obs_summary or obs_mismatch:
            user_prompt += "\n## Observed date cadence from prepared data (sample-based)\n"
            user_prompt += (
                "Textual **start–end** cells are split and parsed; **period starts** drive row-to-row spacing "
                "(median day gap across sorted unique starts). Range span length is summarized as "
                "`median_window_days` when both endpoints parse.\n"
                "Each column includes a heuristic `confidence` (0–1). Low values should appear as "
                "`approval_items` in the review UI — do not override those without analyst confirmation.\n"
                "Use together with Guided Setup `date_granularity`: if they conflict, choose tools that match the "
                "**actual parsed pattern** and record the conflict in `reasoning` or `approval_items`.\n"
            )
            if obs_agg_conf is not None:
                user_prompt += f"- **Aggregate confidence (min across mapped date columns)**: {obs_agg_conf}\n"
            if obs_summary:
                user_prompt += f"- **Summary**: {obs_summary}\n"
            if obs_mismatch:
                user_prompt += f"- **Conflict note**: {obs_mismatch}\n"
            for item in obs_cols[:8]:
                if isinstance(item, dict):
                    user_prompt += f"- {json_safe_dumps(item)}\n"
            pair_hint = (source_summary or {}).get("inferred_date_range_pair")
            if isinstance(pair_hint, dict) and pair_hint.get("start_date_col") and pair_hint.get("end_date_col"):
                user_prompt += (
                    "\n### Inferred two-column date range (from mapped date targets)\n"
                    f"- **Likely start column**: `{pair_hint.get('start_date_col')}`\n"
                    f"- **Likely end column**: `{pair_hint.get('end_date_col')}`\n"
                    f"- **Heuristic confidence**: {pair_hint.get('confidence')} "
                    f"(median inclusive span ≈ {pair_hint.get('median_span_days')} days)\n"
                    "- When the target template is **weekly**, prefer "
                    "`transform.date_range_to_weekly` with `granularity='daily'` then "
                    "`transform.aggregate_weekly` on `calendar_date` (not a single mixed date column).\n"
                )

        if layout_summary:
            context_labels = layout_summary.get("context_block_labels") or []
            user_prompt += f"""

## Approved Layout Scope
- Scope type: {layout_summary.get('scope_type') or 'not specified'}
- Header row: {layout_summary.get('header_row')}
- Analysis bounds: {json_safe_dumps(layout_summary.get('analysis_bounds') or {})}
- Main blocks approved: {layout_summary.get('main_blocks_count', 0)}
- Context blocks approved: {layout_summary.get('context_blocks_count', 0)}
"""
            if context_labels:
                user_prompt += "- Context block labels: " + ", ".join(context_labels) + "\n"

        if context_block_snippets:
            user_prompt += """

## Approved Context Block Snippets
These snippets come from blocks the user explicitly approved as context/metadata.
- Use them as semantic guidance for interpretation, mapping, date logic, aggregation logic, and planner assumptions.
- Do NOT treat them as main metric rows to transform.
"""
            for idx, snippet in enumerate(context_block_snippets[:5], start=1):
                label = snippet.get("block_label") or snippet.get("block_id") or f"context_block_{idx}"
                summary = snippet.get("summary") or ""
                text_preview = snippet.get("text_preview") or []
                non_empty_cells = snippet.get("non_empty_cells") or []
                user_prompt += f"""
- Context block {idx}: {label}
  - Summary: {summary or 'n/a'}
  - Text preview: {json_safe_dumps(text_preview[:4])}
  - Non-empty cells: {json_safe_dumps(non_empty_cells[:8])}
"""

        if interpreted_context:
            user_prompt += """

## Interpreted Context
This metadata was deterministically inferred from approved context blocks.
- Prefer these inferred fields over raw snippet guessing when they help with date logic, rollups, source meaning, and planner assumptions.
"""
            if interpreted_context.get("fields"):
                user_prompt += "\n- Inferred fields: " + json_safe_dumps(interpreted_context.get("fields")) + "\n"
            if interpreted_context.get("assumptions"):
                user_prompt += "- Assumptions / notes: " + json_safe_dumps(interpreted_context.get("assumptions")[:8]) + "\n"
            if interpreted_context.get("evidence"):
                user_prompt += "- Evidence: " + json_safe_dumps(interpreted_context.get("evidence")[:8]) + "\n"

        source_graph_view = (context_packet or {}).get("source_graph_view") or context_summary.get("source_graph_view")
        relationship_context = context_summary.get("relationship_context") or {}
        if source_graph_view or relationship_context.get("approved"):
            user_prompt += """

## Source Graph
Approved relationships between sources and the columns available on each source.
- Treat this as the ONLY trusted topology for cross-source references.
- Never invent joins that are not listed here.
- When a derived field requires a column from another source, confirm an edge with explicit `join_keys` exists.
"""
            if isinstance(source_graph_view, dict):
                nodes_preview = source_graph_view.get("nodes") or []
                edges_preview = source_graph_view.get("edges") or []
                if nodes_preview:
                    user_prompt += "\n- Nodes: " + json_safe_dumps(nodes_preview[:8]) + "\n"
                if edges_preview:
                    user_prompt += "- Edges: " + json_safe_dumps(edges_preview[:8]) + "\n"
            elif relationship_context.get("approved"):
                user_prompt += "\n- Approved relationships: " + json_safe_dumps(
                    list(relationship_context.get("approved") or [])[:8]
                ) + "\n"

        if (
            isinstance(context_packet, dict)
            and len(context_packet.get("available_source_summaries") or []) >= 2
        ):
            fc = context_packet.get("file_relationships")
            if not (isinstance(fc, list) and len(fc) > 0):
                user_prompt += """

## Optional tool: cross-source relationships
When **two or more** uploaded sources appear in ``available_source_summaries`` and **file relationships are not yet approved**, you may include an early tool call (typically before heavy layout transforms):
``{"tool": "discovery.propose_file_relationships", "params": {}}``
It records union/join proposals for review and may pause the run. Skip if relationships are already approved or the job defers relationship review to the web layer.
"""

        resolved_decisions_block = format_resolved_decisions_for_prompt(
            list((context_packet or {}).get("resolved_planner_decisions") or [])
        )
        if resolved_decisions_block:
            user_prompt += resolved_decisions_block

        dup_targets = (context_packet or {}).get("duplicate_target_mappings") or {}
        if isinstance(dup_targets, dict) and dup_targets:
            user_prompt += "\n## Multiple source columns → same template target\n"
            user_prompt += (
                "The analyst already mapped more than one physical column to the same template field. "
                "If `resolved_planner_decisions` specifies a strategy, follow it; do not invent "
                "`spends_1` / `spends_2` stub names in `transform.calculate`.\n"
            )
            for tgt, srcs in list(dup_targets.items())[:8]:
                user_prompt += f"- `{tgt}` ← {', '.join(repr(s) for s in srcs)}\n"

        if approved_mapping_summary.get("mapped_columns"):
            user_prompt += """

## Approved Column Mappings
These mappings were approved upstream. Prefer them over semantic guesses.
- Use the exact approved source column names when emitting `transform.rename`.
- Do NOT invent new rename pairs for template targets that are still `No match` upstream.
"""
            user_prompt += "\n" + "\n".join(
                f"- {item}" for item in approved_mapping_summary.get("mapped_columns", [])
            )

        if excluded_columns:
            user_prompt += """

## Approved Exclusions
These source columns were explicitly marked `Discard` upstream and are already removed from the working dataframe.
- Do NOT include them in rename mappings
- Do NOT include them in `expected_schema`
- Do NOT create business rules for them
"""
            user_prompt += "\n" + "\n".join(f"- {item}" for item in excluded_columns)

        user_prompt += """

## Empty-Row Filtering Guardrail
- `xls.data.filter_empty` is ONLY for genuinely blank / incomplete rows.
- Numeric zero values (for example `0` spend or `0` impressions) are valid populated metrics, NOT blanks.
- Do NOT add `xls.data.filter_empty` unless the analyzer/context shows actual empty rows, spacer rows, or rows with missing metric cells.
"""

        if rules_summary:
            user_prompt += """

## Approved Business Rules
Apply these rules deterministically in the plan where relevant.
"""
            user_prompt += "\n" + "\n".join(f"- {item}" for item in rules_summary)

        if notes_summary:
            user_prompt += """

## User Notes
"""
            user_prompt += "\n" + "\n".join(f"- {item}" for item in notes_summary)

        # Inject target template context if available
        if target_template and target_template.get("properties"):
            template_cols = list(target_template["properties"].keys())
            mandatory = list(template_cols)
            pre_cols = pre_transform_target_columns(target_template)
            template_scope = target_template.get("x_scope") if isinstance(target_template.get("x_scope"), dict) else {}
            template_scope = template_scope or {}
            contract = (context_packet or {}).get("template_contract") or build_template_contract(target_template)
            column_gaps = (context_packet or {}).get("column_gaps")
            if not column_gaps:
                available_columns, _ = self._context_available_columns(context_packet)
                column_gaps = compute_column_gaps(
                    target_template,
                    available_columns,
                    approved_mappings=list((context_packet or {}).get("approved_mappings") or []),
                )
            uid_line = ", ".join(contract.get("uid_hierarchy") or template_scope.get("uid_hierarchy") or []) or "not specified"
            metrics_line = ", ".join(contract.get("metrics") or template_scope.get("metrics") or []) or "not specified"
            supporting_line = ", ".join(contract.get("supporting_columns") or template_scope.get("supporting_columns") or []) or "not specified"
            rules_lines = contract.get("column_rule_summary") or template_column_rules_summary(target_template)
            rules_block = "\n".join(f"  - {r}" for r in rules_lines) if rules_lines else "  - (none)"
            gaps_block = json_safe_dumps(column_gaps, indent=2) if column_gaps else "{}"
            suggested_adds = column_gaps.get("suggested_add_columns") if isinstance(column_gaps, dict) else []
            if suggested_adds:
                add_steps = "\n".join(
                    f"  - `{s.get('target_column')}` = {s.get('value')!r} "
                    f"(rule {s.get('rule_id') or 'template'}; use transform.add_column or one transform.apply_column_rules)"
                    for s in suggested_adds
                    if isinstance(s, dict) and s.get("target_column")
                )
            else:
                add_steps = "  - (none — all rule-backed UID columns present or no column_rules)"
            aggregation_scope = target_template.get("aggregation_logic") or template_scope.get("aggregation_logic")
            if isinstance(aggregation_scope, dict):
                metric_rules = aggregation_scope.get("metric_rules") or {}
                pmi_rules = aggregation_scope.get("pmi_safe_rules") or []
                aggregation_scope_text = ", ".join(
                    [f"{metric}={rule}" for metric, rule in metric_rules.items()] + [str(rule) for rule in pmi_rules]
                )
            elif isinstance(aggregation_scope, list):
                aggregation_scope_text = ", ".join(str(item) for item in aggregation_scope)
            else:
                aggregation_scope_text = str(aggregation_scope or "")
            mp = template_scope.get("modeling_period") or {}
            if isinstance(mp, dict):
                mps, mpe = mp.get("start_date"), mp.get("end_date")
            else:
                mps, mpe = None, None
            template_section = f"""

## Target Template (Enrichment Phase)
The output MUST align with this schema. After extraction and reshaping, add enrichment steps:
- **Final output columns (all required in verify.schema)**: {', '.join(mandatory)}
- **Pre-transform / mapping column order**: {', '.join(pre_cols)}
- **UID hierarchy (grain)**: {uid_line}
- **Metrics**: {metrics_line}
- **Supporting columns**: {supporting_line}
- **Variable type**: {template_scope.get('variable_type') or 'not specified'}
- **Modeling period start**: {mps or 'not specified'}
- **Modeling period end**: {mpe or 'not specified'}
- **Target date granularity**: {effective_target_date_granularity(template_scope) or 'not specified'}
- **PMI-safe aggregation**: {aggregation_scope_text or 'sum additive metrics at duplicate UID rows; never average impressions'}
- **Template column rules (emit as explicit tool steps — `transform.apply_column_rules` or `transform.add_column`)**:
{rules_block}
- **Suggested add-column steps from column_gaps**:
{add_steps}
- Use `transform.rename` to match column names to the template.
- Use `transform.map_values` if any column has enum constraints.
- Use `transform.format` for date/number formatting.
- Treat the UID hierarchy as the deduplication and aggregation grain for the final output.
- When multiple rows exist at the same UID, SUM metrics per aggregation_logic and never average impressions.

## Column gaps (prepared dataframe vs template contract)
```json
{gaps_block}
```

{_TEMPLATE_ENRICHMENT_TOOL_ORDER}
"""
            target_granularity = str(effective_target_date_granularity(template_scope) or "").strip().lower()
            source_granularity = str((source_summary or {}).get("date_granularity") or "").strip().lower()
            if target_granularity in ("weekly", "week"):
                metrics_list = template_scope.get("metrics") or []
                available_columns, _alias_lookup = self._context_available_columns(context_packet)
                resolved_metrics = [str(metric) for metric in metrics_list if str(metric) in available_columns]
                metrics_for_weekly_tools = resolved_metrics or [str(metric) for metric in metrics_list]
                metric_rule_map: Dict[str, str] = {}
                if isinstance(aggregation_scope, dict):
                    raw_rules = aggregation_scope.get("metric_rules") or {}
                    if isinstance(raw_rules, dict):
                        for metric_name, rule in raw_rules.items():
                            metric_name = str(metric_name)
                            if metric_name in metrics_for_weekly_tools:
                                metric_rule_map[metric_name] = str(rule or "sum").lower()
                for metric_name in metrics_for_weekly_tools:
                    metric_rule_map.setdefault(str(metric_name), "sum")
                metric_rules_text = ", ".join(f"{m}={r}" for m, r in metric_rule_map.items()) or "sum for every template metric"
                preprocessing_guidance = (
                    "- If the date is split across parts (for example year/month/day or year/quarter), "
                    "first use `transform.build_date_from_parts` to create one real date column.\n"
                )

                sparse_note = ""
                sparse_dims = source_summary.get("sparse_dimension_columns") or []
                structured = structure_analysis.get("tables") if isinstance(structure_analysis, dict) else None
                hierarchies = []
                if isinstance(structured, list):
                    for tbl in structured:
                        if isinstance(tbl, dict) and isinstance(tbl.get("hierarchy"), dict):
                            htype = str((tbl["hierarchy"] or {}).get("type") or "").strip().lower()
                            if htype == "grouped_rows":
                                hierarchies.append(htype)
                grouped_rows_sheet = bool(hierarchies)
                mls = (
                    structure_analysis.get("metric_layout_signals")
                    if isinstance(structure_analysis, dict)
                    else None
                )
                if isinstance(mls, dict) and mls.get("grouped_rows_likely"):
                    grouped_rows_sheet = True

                if isinstance(sparse_dims, list):
                    for entry in sparse_dims:
                        if isinstance(entry, dict):
                            br = entry.get("blank_ratio") or entry.get("blank_ratio_approx")
                            try:
                                pct = float(br) * 100 if br is not None and float(br) <= 1 else float(br or 0)
                            except (TypeError, ValueError):
                                pct = 0.0
                            if pct >= 40.0:
                                sparse_note += (
                                    "- Block-style layout detected: some columns are sparse (e.g. dimensions with "
                                    "high blank rate). If **spend/budget** is only populated on the parent row of each "
                                    "section, after `rename`/`type_cast` you may **`transform.infer_block_boundary_columns`** "
                                    "(if noisy ids were dropped), then **`transform.classify_metric_level`** "
                                    "with inferred or confirmed `block_start_columns`, then **`transform.allocate_block_metric`** "
                                    "**(equal: row_spend = block_total / rows_in_block; weighted: multiply by impressions share "
                                    "within block)** with `metric_col` = spend and realistic boundaries (e.g. `Ref`, `Name`). "
                                    "Do **not** `transform.fill_merged` on spend. Then **`transform.aggregate_weekly`** "
                                    "with explicit `metric_rules` for every template metric (e.g. `spends`, `impressions`).\n"
                                )
                                break
                elif grouped_rows_sheet:
                    sparse_note += (
                        "- Structure analysis suggests **grouped_rows** hierarchies; if mapped spend/budget is "
                        "sparse on continuation rows, optionally **`transform.classify_metric_level`** then "
                        "**`transform.allocate_block_metric`** (equal vs weighted-by-impressions) before weekly rollup "
                        "and **`transform.aggregate_weekly`** with explicit `metric_rules`; avoid `fill_merged` "
                        "on spend/budget.\n"
                    )

                preprocessing_guidance = preprocessing_guidance + sparse_note if sparse_note else preprocessing_guidance

                if source_granularity in ("range", "date_range", "daterange", "flight", "flighting"):
                    tool_guidance = "Use `transform.date_range_to_weekly` with the start/end date columns and `value_cols` set to the template metrics."
                elif source_granularity in ("month", "monthly", "quarter", "quarterly"):
                    tool_guidance = (
                        "Use `transform.expand_period_to_daily` first with the period date column, "
                        "`input_granularity` set to the source grain, and `value_cols` set to the template metrics; "
                        "then use `transform.aggregate_weekly` on the emitted daily date column with "
                        f"`metric_rules` = {{{metric_rules_text}}}."
                    )
                else:
                    tool_guidance = (
                        "Use `transform.aggregate_weekly` with `date_col` set to the (renamed) date column, "
                        "`group_by_cols` set to the UID hierarchy + supporting columns, and `metric_rules` = "
                        f"{{{metric_rules_text}}}."
                    )

                template_section += f"""
### Weekly Rollup (MANDATORY)
Target granularity is **weekly** and the source granularity is `{source_granularity or 'unspecified'}`.
- {preprocessing_guidance.strip()}
- {tool_guidance}
- Prefer resolved metric columns only: {', '.join(metrics_for_weekly_tools) if metrics_for_weekly_tools else 'none resolved yet'}.
- If supporting or hierarchy dimension columns are sparse within a block (for example `Market` or `Publisher` only appears on the first row of each section), add `transform.fill_merged` on **those dimension columns only** BEFORE weekly aggregation so those dimensions are populated on every metric row.
- If **spend/budget** (or any template metric) appears only on the **parent row** of each block and is blank on post rows, add `transform.allocate_block_metric` after `rename`/`type_cast` on that metric column (with `block_start_columns` such as `Ref` or `Name`). **Do not** use `transform.fill_merged` on spend/budget metrics—that duplicates contract totals when you SUM in `aggregate_weekly`.
- Always pass explicit `metric_rules` for **every** template metric in `transform.aggregate_weekly` (e.g. `{{spends: sum, impressions: sum}}`) so sparse numeric columns are not dropped by inference.
- Place the weekly aggregation step AFTER rename/type_cast/format and BEFORE `verify.schema`.
- After aggregation the output MUST contain a single Monday-aligned weekly date column. Use the template date field as `date_col` (e.g. `date` or `calendar_date`); **do not** pass `week_start_col` unless the template uid date is literally `week_start` or you need a second date column alongside the daily one. Do NOT require a separate `week_end` column unless a human explicitly asks for it.
"""

            user_prompt += template_section

        if observer:
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
                }
                
                try:
                    # Configuration-driven generation config
                    # IMPORTANT: Always ensure sufficient max_output_tokens for complex responses
                    gen_config = {"max_output_tokens": 8192, "temperature": 0.0}  # Increased default
                    
                    # MERGE with client config if available (don't replace!)
                    if hasattr(self.llm_client, 'generation_config') and self.llm_client.generation_config:
                        client_config = self.llm_client.generation_config
                        if isinstance(client_config, dict):
                            if client_config.get('max_output_tokens', 0) > gen_config['max_output_tokens']:
                                gen_config['max_output_tokens'] = client_config['max_output_tokens']
                            if 'temperature' in client_config:
                                gen_config['temperature'] = client_config['temperature']
                         
                    response = self.llm_wrapper.generate_content(t.full_prompt, generation_config=gen_config)
                    response_text = response.text
                    t.raw_response = response_text
                    
                    if hasattr(self.llm_client, 'model_name'):
                         t.model_id = self.llm_client.model_name.replace('models/', '')
                         
                    logger.info(f"Captured LLM response for plan_generator: {len(response_text)} chars")
                except Exception as e:
                    logger.error(f"PlanGenerator unexpected error: {e}")
                    t.error_message = str(e)
                    t.success = False
                    if not t.raw_response:
                        t.raw_response = f"Unexpected Error: {e}"
                    response = type('obj', (object,), {'text': ''})  # Mock empty response
                
                try:
                    plan = self._parse_plan(
                        response.text, structure_analysis, context_packet=context_packet
                    )
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
                        review_reason="AI failed to generate a valid plan JSON."
                    )
                    t.success = False
                    t.error_message = str(e)
                
                t.parsed_output = {
                    "tool_calls": plan.tool_calls,
                    "business_rule_actions": plan.business_rule_actions,
                    "approval_items": plan.approval_items,
                    "expected_columns": plan.expected_columns,
                    "confidence": plan.confidence,
                    "reasoning": plan.reasoning
                }
                t.confidence_score = float(plan.confidence)
                
                return plan
        else:
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
            return self.finalize_plan(
                plan, context_packet, target_template, structure_analysis
            )
    
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
