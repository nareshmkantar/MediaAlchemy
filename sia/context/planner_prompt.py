"""
Curated planner user prompt — ordered, deduped, budget-capped view of ContextPacket.

Full context remains in ``build_context_packet()`` for audit/debug; only this string
is sent to the plan_generator LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sia.agent.context_packet import compute_date_granularity_alignment, effective_target_date_granularity
from sia.agent.json_safe import dumps as json_safe_dumps
from sia.agent.planner_decisions import format_resolved_decisions_for_prompt
from sia.agent.target_template_utils import (
    build_template_contract,
    compute_column_gaps,
    pre_transform_target_columns,
    template_column_rules_summary,
)
from sia.context.planner_context import prepare_planner_context_sections
from sia.context.prompt import format_scoped_fields_for_planner
from sia.context.prompt_briefing import (
    assemble_briefing_payload,
    build_tools_catalog_briefing,
    section_meta,
    serialize_section_row,
)
from sia.integrity.context_isolation import SOURCE_LOCAL_DIMENSION_COLUMNS

# Drop-from-prompt-first when over budget (higher = trimmed sooner).
_PRIORITY_EXAMPLES = 100
_PRIORITY_SOFT_HINTS = 90
_PRIORITY_WEEKLY_EXTRA = 85
_PRIORITY_SNIPPETS = 80
_PRIORITY_EVIDENCE = 75
_PRIORITY_LAYOUT = 70
_PRIORITY_STRUCTURE_REASONING = 65
_PRIORITY_TEMPLATE_GAPS_JSON = 55
_PRIORITY_USER_NOTES = 50
_PRIORITY_RULES = 40
_PRIORITY_RELATIONSHIPS = 35
_PRIORITY_DATE_OBS = 30
_PRIORITY_TEMPLATE = 25
_PRIORITY_STRUCTURE = 20
_PRIORITY_DATE_GRAIN = 15
_PRIORITY_SOURCE_META = 12
_PRIORITY_MAPPINGS = 8
_PRIORITY_LOCAL = 5
_PRIORITY_TASK = 1

DEFAULT_MAX_PROMPT_CHARS = 48_000

_DIMENSION_KEYS = frozenset(SOURCE_LOCAL_DIMENSION_COLUMNS) | frozenset(
    {"modeling_period_start", "modeling_period_end", "aggregation_logic"}
)

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

_KEEP_DECISIONS = {"keep", "approved", "primary", "supporting", "metadata", "context", "use as context"}


@dataclass(frozen=True)
class _Section:
    section_id: str
    bucket: str
    title: str
    drop_priority: int
    body: str


@dataclass
class _PromptBuildResult:
    text: str
    included_ids: List[str]
    dropped_ids: List[str]
    sections: List[_Section]


def _sec(section_id: str, body: str, drop_priority: int) -> _Section:
    bucket, title = section_meta(section_id)
    return _Section(section_id, bucket, title, drop_priority, body)


def context_available_columns(
    context_packet: Optional[Dict[str, Any]],
) -> Tuple[List[str], Dict[str, str]]:
    """Resolved column names available for template gap / weekly tool planning."""
    approved_mappings = list((context_packet or {}).get("approved_mappings") or [])
    business_rules = list((context_packet or {}).get("business_rules") or [])
    source_summary = ((context_packet or {}).get("planning_summary") or {}).get("source_summary") or {}
    prepared_columns = [str(col) for col in (source_summary.get("prepared_columns") or []) if str(col).strip()]

    discard_columns = {
        str(item.get("source_column") or "").strip()
        for item in approved_mappings
        if isinstance(item, dict) and str(item.get("decision", "")).strip().lower() == "discard"
    }

    available_columns: List[str] = [col for col in prepared_columns if col and col not in discard_columns]
    alias_lookup: Dict[str, str] = {}

    for item in approved_mappings:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision", "")).strip().lower()
        source_col = str(item.get("source_column") or "").strip()
        target_col = str(item.get("target_column") or "").strip()
        if decision not in _KEEP_DECISIONS or not source_col:
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

    deduped: List[str] = []
    seen: set = set()
    for col in available_columns:
        if col and col not in seen:
            seen.add(col)
            deduped.append(col)
    return deduped, alias_lookup


def _scoped_field_names(interpreted_context: Dict[str, Any]) -> set:
    scoped = interpreted_context.get("scoped_fields")
    if isinstance(scoped, dict) and scoped:
        return {str(k) for k in scoped.keys()}
    fields = interpreted_context.get("fields")
    if isinstance(fields, dict):
        return {str(k) for k in fields.keys() if str(fields.get(k) or "").strip()}
    return set()


def _summarize_structure(structure_analysis: Dict[str, Any]) -> Tuple[Dict[str, Any], str, str]:
    col_analysis = structure_analysis.get("column_analysis", [])
    if isinstance(col_analysis, dict):
        col_analysis = list(col_analysis.values())
    elif not isinstance(col_analysis, list):
        col_analysis = []

    tables = structure_analysis.get("tables", [])
    tables_summary = ""
    if len(tables) > 1:
        tables_summary = (
            f"\n## IMPORTANT: Multiple Tables Detected ({len(tables)} tables)\n"
            "This spreadsheet contains separate data blocks placed side-by-side.\n"
            "You MUST use the `merge_blocks` tool to combine them into a single dataset.\n\nTables detected:\n"
        )
        for i, t in enumerate(tables):
            if isinstance(t, dict):
                coords = t.get("coordinates", t)
                label = t.get("label", f"Table {i+1}")
                c_start = coords.get("col_start", coords.get("start_col", 0))
                c_end = coords.get("col_end", coords.get("end_col", 0))
                tables_summary += f"- {label}: cols {c_start}-{c_end}\n"
    elif len(tables) == 1:
        t = tables[0]
        if isinstance(t, dict):
            coords = t.get("coordinates", t)
            label = t.get("label", "Main data")
            r_start = coords.get("data_start_row", coords.get("start_row", 0))
            r_end = coords.get("data_end_row", coords.get("end_row", 0))
            c_start = coords.get("col_start", coords.get("start_col", 0))
            c_end = coords.get("col_end", coords.get("end_col", 0))
            tables_summary = (
                f"\n## Single Table Detected\n"
                f"- {label}: rows {r_start}-{r_end}, cols {c_start}-{c_end}\n"
            )

    summarized = {
        "overall_structure": structure_analysis.get("overall_structure", "unknown"),
        "confidence": structure_analysis.get("confidence", 0),
        "tables": [
            {
                "label": t.get("label", f"Table_{i}") if isinstance(t, dict) else f"Table_{i}",
                "coordinates": t.get("coordinates", {}) if isinstance(t, dict) else {},
                "table_shape": t.get("table_shape", "flat") if isinstance(t, dict) else "flat",
                "column_pattern": t.get("column_pattern", {}) if isinstance(t, dict) else {},
            }
            for i, t in enumerate(structure_analysis.get("tables", []))
        ],
        "visual_patterns": structure_analysis.get("visual_patterns", {}),
        "column_summary": [
            {
                "col": c.get("col"),
                "name": c.get("name", f"col_{c.get('col')}"),
                "type": c.get("type", "unknown"),
                "is_blank": c.get("is_blank", False),
            }
            for c in (col_analysis[:15] if len(col_analysis) > 15 else col_analysis)
            if isinstance(c, dict)
        ],
        "reasoning": structure_analysis.get("reasoning", "")[:500],
    }

    temporal_cols = [
        c.get("name", f"col_{c.get('col')}")
        for c in col_analysis
        if isinstance(c, dict) and c.get("type") == "temporal"
    ]
    has_repetitions = any(
        (t.get("column_pattern", {}).get("repetitions", 1) > 1)
        for t in (structure_analysis.get("tables") or [])
        if isinstance(t, dict)
    )
    unpivot_hint = ""
    if temporal_cols and not has_repetitions:
        unpivot_hint = (
            f"\n**Note:** Temporal columns detected ({', '.join(temporal_cols[:5])}). "
            "Consider using `transform.unpivot` to melt these into rows.\n"
        )
    elif has_repetitions:
        unpivot_hint = (
            "\n**Note:** Repeating column pattern detected (side-by-side blocks). "
            "Use `layout.stack` only with explicit per-block `col_start`/`col_end` from demarcation "
            "or structure `tables`, after headers align across blocks. Do NOT stack the full sheet "
            "width as one block — extract each block separately if boundaries are unclear.\n"
        )

    return summarized, tables_summary, unpivot_hint


def _section_local_context(interpreted_context: Dict[str, Any]) -> str:
    scoped_lines = format_scoped_fields_for_planner(interpreted_context)
    if not scoped_lines:
        fields = interpreted_context.get("fields") if isinstance(interpreted_context.get("fields"), dict) else {}
        if not fields:
            return ""
        scoped_lines = [f"{k}={v!r}" for k, v in fields.items() if str(v or "").strip()]

    if not scoped_lines:
        return ""

    lines = [
        "## Local context (authoritative for this sheet)",
        "Use these values for plan literals and dimension stamping. Do not re-infer from sheet name or snippets.",
    ]
    for line in scoped_lines[:12]:
        lines.append(f"- {line}")
    return "\n".join(lines) + "\n"


def _section_snippets(
    snippets: Sequence[Dict[str, Any]],
    covered_fields: set,
    *,
    max_blocks: int = 5,
) -> str:
    if not snippets:
        return ""
    blocks: List[str] = [
        "## Context block evidence (supplementary)",
        "Raw approved metadata blocks. Local context above wins on conflict.",
        "- Do NOT treat these as main metric rows to transform.",
    ]
    added = 0
    for idx, snippet in enumerate(snippets[:max_blocks], start=1):
        if not isinstance(snippet, dict):
            continue
        label = snippet.get("block_label") or snippet.get("block_id") or f"context_block_{idx}"
        summary = str(snippet.get("summary") or "").strip()
        text_preview = list(snippet.get("text_preview") or [])
        non_empty_cells = list(snippet.get("non_empty_cells") or [])
        if covered_fields and not summary and not text_preview:
            continue
        blocks.append(f"\n- Block {idx}: {label}")
        if summary:
            blocks.append(f"  - Summary: {summary}")
        if text_preview:
            blocks.append(f"  - Text preview: {json_safe_dumps(text_preview[:4])}")
        if non_empty_cells and len(covered_fields) < 2:
            blocks.append(f"  - Non-empty cells: {json_safe_dumps(non_empty_cells[:8])}")
        added += 1
    if added == 0:
        return ""
    return "\n".join(blocks) + "\n"


def _section_source_metadata(
    source_summary: Dict[str, Any],
    scoped_names: set,
) -> str:
    if not source_summary:
        return ""
    uid = source_summary.get("uid") or []
    aggregation_logic = source_summary.get("aggregation_logic") or ""
    prepared_columns = source_summary.get("prepared_columns") or []
    header_derivation = source_summary.get("header_derivation") or {}
    sparse_dimension_columns = source_summary.get("sparse_dimension_columns") or []

    lines = [
        "## Source identity",
        f"- File: {source_summary.get('file_name')}",
        f"- Sheet: {source_summary.get('sheet_name')}",
        f"- Variable type: {source_summary.get('variable_type') or source_summary.get('source_type') or 'not specified'}",
        f"- UID: {', '.join(uid) if isinstance(uid, list) and uid else 'not specified'}",
        f"- Date granularity: {source_summary.get('date_granularity') or 'not specified'}",
    ]
    if aggregation_logic and "aggregation_logic" not in scoped_names:
        lines.append(f"- Aggregation logic: {aggregation_logic}")
    for dim in sorted(_DIMENSION_KEYS):
        if dim in scoped_names:
            continue
        val = source_summary.get(dim)
        if val and str(val).strip():
            lines.append(f"- {dim}: {val}")
    mps = source_summary.get("modeling_period_start")
    mpe = source_summary.get("modeling_period_end")
    if mps and "modeling_period_start" not in scoped_names:
        lines.append(f"- Modeling period start: {mps}")
    if mpe and "modeling_period_end" not in scoped_names:
        lines.append(f"- Modeling period end: {mpe}")
    if prepared_columns:
        lines.append(
            "- Prepared columns after scoped/header preprocessing: "
            + ", ".join(str(c) for c in prepared_columns[:20])
        )
    if isinstance(header_derivation, dict) and header_derivation.get("derived"):
        lines.append(
            f"- Header derivation: {header_derivation.get('message') or 'multi-row headers were merged before mapping/planning'}"
        )
    if sparse_dimension_columns:
        sparse_text = ", ".join(
            f"{item.get('source_column')} ({round(float(item.get('blank_ratio') or 0.0) * 100)}% blank)"
            for item in sparse_dimension_columns[:6]
            if isinstance(item, dict) and item.get("source_column")
        )
        if sparse_text:
            lines.append(f"- Sparse dimension columns detected: {sparse_text}")
    return "\n".join(lines) + "\n"


def _build_template_section(
    target_template: Dict[str, Any],
    context_packet: Optional[Dict[str, Any]],
    source_summary: Dict[str, Any],
    structure_analysis: Dict[str, Any],
    *,
    weekly_drop_priority: int = _PRIORITY_WEEKLY_EXTRA,
) -> Tuple[str, int]:
    """Returns (body, effective_drop_priority for weekly extras)."""
    template_cols = list(target_template["properties"].keys())
    mandatory = list(template_cols)
    pre_cols = pre_transform_target_columns(target_template)
    template_scope = target_template.get("x_scope") if isinstance(target_template.get("x_scope"), dict) else {}
    template_scope = template_scope or {}
    contract = (context_packet or {}).get("template_contract") or build_template_contract(target_template)
    column_gaps = (context_packet or {}).get("column_gaps")
    if not column_gaps:
        available_columns, _ = context_available_columns(context_packet)
        column_gaps = compute_column_gaps(
            target_template,
            available_columns,
            approved_mappings=list((context_packet or {}).get("approved_mappings") or []),
        )
    uid_line = ", ".join(contract.get("uid_hierarchy") or template_scope.get("uid_hierarchy") or []) or "not specified"
    metrics_line = ", ".join(contract.get("metrics") or template_scope.get("metrics") or []) or "not specified"
    supporting_line = (
        ", ".join(contract.get("supporting_columns") or template_scope.get("supporting_columns") or [])
        or "not specified"
    )
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

    body = f"""
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

    weekly_priority = weekly_drop_priority
    target_granularity = str(effective_target_date_granularity(template_scope) or "").strip().lower()
    source_granularity = str((source_summary or {}).get("date_granularity") or "").strip().lower()
    if target_granularity in ("weekly", "week"):
        metrics_list = template_scope.get("metrics") or []
        available_columns, _ = context_available_columns(context_packet)
        resolved_metrics = [str(m) for m in metrics_list if str(m) in available_columns]
        metrics_for_weekly = resolved_metrics or [str(m) for m in metrics_list]
        metric_rule_map: Dict[str, str] = {}
        if isinstance(aggregation_scope, dict):
            raw_rules = aggregation_scope.get("metric_rules") or {}
            if isinstance(raw_rules, dict):
                for metric_name, rule in raw_rules.items():
                    metric_name = str(metric_name)
                    if metric_name in metrics_for_weekly:
                        metric_rule_map[metric_name] = str(rule or "sum").lower()
        for metric_name in metrics_for_weekly:
            metric_rule_map.setdefault(str(metric_name), "sum")
        metric_rules_text = ", ".join(f"{m}={r}" for m, r in metric_rule_map.items()) or "sum for every template metric"
        preprocessing_guidance = (
            "- If the date is split across parts (for example year/month/day or year/quarter), "
            "first use `transform.build_date_from_parts` to create one real date column.\n"
        )
        sparse_note = ""
        sparse_dims = source_summary.get("sparse_dimension_columns") or []
        grouped_rows_sheet = False
        structured = structure_analysis.get("tables") if isinstance(structure_analysis, dict) else None
        if isinstance(structured, list):
            for tbl in structured:
                if isinstance(tbl, dict) and isinstance(tbl.get("hierarchy"), dict):
                    if str((tbl["hierarchy"] or {}).get("type") or "").strip().lower() == "grouped_rows":
                        grouped_rows_sheet = True
        mls = structure_analysis.get("metric_layout_signals") if isinstance(structure_analysis, dict) else None
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
                        sparse_note = (
                            "- Block-style layout: sparse dimensions — consider block metric allocation "
                            "before `transform.aggregate_weekly` with explicit `metric_rules`.\n"
                        )
                        break
        elif grouped_rows_sheet:
            sparse_note = (
                "- Grouped_rows layout: consider `classify_metric_level` / `allocate_block_metric` "
                "before weekly rollup.\n"
            )
        if sparse_note:
            preprocessing_guidance += sparse_note

        if source_granularity in ("range", "date_range", "daterange", "flight", "flighting"):
            tool_guidance = (
                "Use `transform.date_range_to_weekly` with the start/end date columns "
                "and `value_cols` set to the template metrics."
            )
        elif source_granularity in ("month", "monthly", "quarter", "quarterly"):
            tool_guidance = (
                "Use `transform.expand_period_to_daily` then `transform.aggregate_weekly` "
                f"with `metric_rules` = {{{metric_rules_text}}}."
            )
        else:
            tool_guidance = (
                "Use `transform.aggregate_weekly` with `date_col`, UID `group_by_cols`, "
                f"and `metric_rules` = {{{metric_rules_text}}}."
            )

        body += f"""
### Weekly Rollup (MANDATORY)
Target granularity is **weekly**; source granularity is `{source_granularity or 'unspecified'}`.
- {preprocessing_guidance.strip()}
- {tool_guidance}
- Prefer resolved metric columns: {', '.join(metrics_for_weekly) if metrics_for_weekly else 'none resolved yet'}.
- Place weekly aggregation AFTER rename/type_cast/format and BEFORE `verify.schema`.
- Pass explicit `metric_rules` for every template metric in `aggregate_weekly`.
"""

    return body, weekly_priority


def _apply_prompt_budget(sections: Sequence[_Section], max_chars: int) -> _PromptBuildResult:
    remaining: Dict[str, _Section] = {
        s.section_id: s for s in sections if s.body and s.body.strip()
    }
    all_ids = [s.section_id for s in sections if s.section_id in remaining]

    def assembled() -> str:
        return "".join(remaining[s.section_id].body for s in sections if s.section_id in remaining)

    if len(assembled()) <= max_chars:
        return _PromptBuildResult(
            text=assembled(),
            included_ids=list(all_ids),
            dropped_ids=[],
            sections=list(sections),
        )

    droppable = sorted(
        [s for s in sections if s.section_id in remaining],
        key=lambda s: s.drop_priority,
        reverse=True,
    )
    for sec in droppable:
        if len(assembled()) <= max_chars:
            break
        if sec.drop_priority <= _PRIORITY_MAPPINGS:
            continue
        remaining.pop(sec.section_id, None)

    if len(assembled()) > max_chars:
        for sec in reversed([s for s in sections if s.section_id in remaining]):
            if sec.drop_priority <= _PRIORITY_DATE_GRAIN:
                continue
            body = remaining[sec.section_id].body
            if len(body) > 800:
                remaining[sec.section_id] = _Section(
                    sec.section_id,
                    sec.bucket,
                    sec.title,
                    sec.drop_priority,
                    body[:800] + "\n\n… [truncated for prompt budget]\n",
                )
            if len(assembled()) <= max_chars:
                break

    included = [sid for sid in all_ids if sid in remaining]
    dropped = [sid for sid in all_ids if sid not in remaining]
    final_sections: List[_Section] = []
    for sec in sections:
        if sec.section_id in remaining:
            final_sections.append(remaining[sec.section_id])
        elif sec.section_id in dropped:
            final_sections.append(sec)
    return _PromptBuildResult(
        text=assembled(),
        included_ids=included,
        dropped_ids=dropped,
        sections=final_sections,
    )


def _section_rows_from_budget(budget: _PromptBuildResult) -> List[Dict[str, Any]]:
    included = set(budget.included_ids)
    rows: List[Dict[str, Any]] = []
    for sec in budget.sections:
        if not (sec.body and sec.body.strip()):
            continue
        truncated = "[truncated for prompt budget]" in sec.body
        rows.append(
            serialize_section_row(
                section_id=sec.section_id,
                body=sec.body,
                drop_priority=sec.drop_priority,
                in_curated_prompt=sec.section_id in included,
                truncated=truncated,
            )
        )
    return rows


def _finalize_planner_prompt(
    sections: Sequence[_Section],
    *,
    max_chars: int,
    context_packet: Optional[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    budget = _apply_prompt_budget(sections, max_chars)
    tools_row = build_tools_catalog_briefing()
    section_rows = _section_rows_from_budget(budget)
    briefing = assemble_briefing_payload(
        user_prompt=budget.text,
        section_rows=section_rows,
        included_ids=budget.included_ids,
        dropped_ids=budget.dropped_ids,
        max_chars=max_chars,
        context_packet=context_packet,
        tools_row=tools_row,
    )
    return budget.text, briefing


def _collect_planner_prompt_sections(
    structure_analysis: Dict[str, Any],
    context_packet: Optional[Dict[str, Any]] = None,
    target_template: Optional[Dict[str, Any]] = None,
    examples: Optional[List[Dict[str, Any]]] = None,
    *,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> List[_Section]:
    """
    Collect ordered prompt sections before budget curation.

    Ordering: local context → mappings → source/structure → template → supplementary evidence.
    """
    cp = dict(context_packet or {})
    context_summary = cp.get("planning_summary") if isinstance(cp.get("planning_summary"), dict) else {}
    planner_ctx = prepare_planner_context_sections(cp, context_summary)

    approved_mapping_summary = context_summary.get("mapping_summary") or {}
    layout_summary = context_summary.get("layout_summary") or {}
    interpreted_context = dict(planner_ctx.get("interpreted_context") or {})
    rules_summary = list(context_summary.get("rules_summary") or [])
    source_summary = dict(context_summary.get("source_summary") or {})
    notes_summary = list(context_summary.get("user_notes") or [])
    relationship_context = dict(planner_ctx.get("relationship_context") or context_summary.get("relationship_context") or {})
    sanitized_peer_summaries = list(planner_ctx.get("available_source_summaries") or [])
    source_graph_view = planner_ctx.get("source_graph_view") or cp.get("source_graph_view")
    excluded_columns = list(approved_mapping_summary.get("excluded_columns") or [])
    value_scale_notes = list(approved_mapping_summary.get("value_scale_notes") or [])
    context_block_snippets = list(cp.get("context_block_snippets") or [])

    summarized, tables_summary, unpivot_hint = _summarize_structure(structure_analysis or {})
    col_analysis = structure_analysis.get("column_analysis", []) if structure_analysis else []
    if isinstance(col_analysis, dict):
        col_analysis = list(col_analysis.values())
    elif not isinstance(col_analysis, list):
        col_analysis = []
    cols_context_list = [
        c.get("name", f"col_{c.get('col', i)}")
        for i, c in enumerate(col_analysis[:8])
        if isinstance(c, dict)
    ]

    examples_text = ""
    if examples:
        examples_text = "Similar examples from database:\n" + "\n".join(
            f"- {e.get('description', 'Example')}" for e in examples[:3]
        )

    task_header = f"""Given this spreadsheet structure analysis, create a sequence of tool calls to transform the data into a flat, normalized table.

## Your Task
Create a JSON response with this priority order:
1. `confidence` - Your confidence in this plan (0.0 to 1.0)
2. `tool_calls` - Ordered list of transformations to apply
3. `business_rule_actions` - Explicit actions for defaults, formatting, fill-blank rules, or cross-field checks
4. `approval_items` - **Sparse** human decisions only when the plan is ambiguous or confidence is weaker: default to `[]`. Prefer objects with `question` + `options` (2–4 `{{id,label}}` choices that pick a branch). Avoid long lists of Yes/No trivia—see system prompt **4A** (max ~3 items, no routine column-definition checks).
5. `expected_schema` - The column names you expect in the final output (list of strings)

Respond with JSON only, no markdown formatting.
"""

    scoped_names = _scoped_field_names(interpreted_context)
    sections: List[_Section] = []

    sections.append(_sec("task", task_header, _PRIORITY_TASK))

    local_ctx = _section_local_context(interpreted_context)
    if local_ctx:
        sections.append(_sec("local_context", "\n" + local_ctx, _PRIORITY_LOCAL))

    mapped = approved_mapping_summary.get("mapped_columns") or []
    if mapped:
        mapping_body = (
            "\n## Approved Column Mappings\n"
            "These mappings were approved upstream. Prefer them over semantic guesses.\n"
            "- Use the exact approved source column names when emitting `transform.rename`.\n"
            "- Do NOT invent new rename pairs for template targets that are still `No match` upstream.\n"
            + "\n".join(f"- {item}" for item in mapped)
            + "\n"
        )
        sections.append(_sec("mappings", mapping_body, _PRIORITY_MAPPINGS))

    if excluded_columns:
        excl = (
            "\n## Approved Exclusions\n"
            "These source columns were explicitly marked `Discard` upstream and are already removed from the working dataframe.\n"
            "- Do NOT include them in rename mappings\n"
            "- Do NOT include them in `expected_schema`\n"
            "- Do NOT create business rules for them\n"
            + "\n".join(f"- {item}" for item in excluded_columns)
            + "\n"
        )
        sections.append(_sec("exclusions", excl, _PRIORITY_MAPPINGS))

    resolved_block = format_resolved_decisions_for_prompt(list(cp.get("resolved_planner_decisions") or []))
    if resolved_block:
        sections.append(_sec("resolved", resolved_block, _PRIORITY_MAPPINGS))

    dup_targets = cp.get("duplicate_target_mappings") or {}
    if isinstance(dup_targets, dict) and dup_targets:
        dup_body = (
            "\n## Multiple source columns → same template target\n"
            "Follow `resolved_planner_decisions` when present; do not invent stub column names.\n"
        )
        for tgt, srcs in list(dup_targets.items())[:8]:
            dup_body += f"- `{tgt}` ← {', '.join(repr(s) for s in srcs)}\n"
        sections.append(_sec("dup_targets", dup_body, _PRIORITY_MAPPINGS))

    meta = _section_source_metadata(source_summary, scoped_names)
    if meta:
        sections.append(_sec("source_meta", "\n" + meta, _PRIORITY_SOURCE_META))

    gran_align = context_summary.get("date_granularity_alignment") or {}
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
        defer_union = bool(cp.get("defer_weekly_rollups_to_post_union"))
        if defer_union:
            grain_body = f"""
## Date granularity (multi-source batch — per-source plan only)
- Source vs template: **{gran_align.get('source_date_granularity') or gran_align.get('normalized_source') or 'not specified'}** → **{gran_align.get('target_date_granularity') or gran_align.get('normalized_target') or 'not specified'}**.
- Include grain tools in `tool_calls` when needed; executor defers until after duplicate_check.{rec_line}
"""
        else:
            grain_body = f"""
## Date granularity alignment (REQUIRED — honor in tool_calls)
- **Source**: {gran_align.get('source_date_granularity') or gran_align.get('normalized_source') or 'not specified'}
- **Target**: {gran_align.get('target_date_granularity') or gran_align.get('normalized_target') or 'not specified'}
- **Planner obligation**: {gran_align.get('planner_obligation')}{rec_line}
"""
        sections.append(_sec("date_grain", grain_body, _PRIORITY_DATE_GRAIN))

    obs_cols = (source_summary or {}).get("date_column_observations") or []
    obs_summary = (source_summary or {}).get("date_cadence_summary") or ""
    obs_mismatch = (source_summary or {}).get("date_granularity_mismatch_note") or ""
    obs_agg_conf = (source_summary or {}).get("date_cadence_aggregate_confidence")
    if obs_cols or obs_summary or obs_mismatch:
        obs_body = "\n## Observed date cadence from prepared data (sample-based)\n"
        if obs_agg_conf is not None:
            obs_body += f"- **Aggregate confidence**: {obs_agg_conf}\n"
        if obs_summary:
            obs_body += f"- **Summary**: {obs_summary}\n"
        if obs_mismatch:
            obs_body += f"- **Conflict note**: {obs_mismatch}\n"
        for item in obs_cols[:8]:
            if isinstance(item, dict):
                obs_body += f"- {json_safe_dumps(item)}\n"
        pair_hint = (source_summary or {}).get("inferred_date_range_pair")
        if isinstance(pair_hint, dict) and pair_hint.get("start_date_col") and pair_hint.get("end_date_col"):
            obs_body += (
                f"\n### Inferred date range columns: `{pair_hint.get('start_date_col')}` / "
                f"`{pair_hint.get('end_date_col')}` (confidence {pair_hint.get('confidence')})\n"
            )
        sections.append(_sec("date_obs", obs_body, _PRIORITY_DATE_OBS))

    structure_body = f"""
## Structure Analysis (Summary)
{json_safe_dumps(summarized, indent=2)}
{unpivot_hint}
{examples_text}
{tables_summary}

## Important Context
- Columns include: {', '.join(cols_context_list)}
- Overall structure: {structure_analysis.get('overall_structure', 'Unknown')}
"""
    sections.append(_sec("structure", structure_body, _PRIORITY_STRUCTURE))

    if layout_summary:
        context_labels = layout_summary.get("context_block_labels") or []
        layout_body = f"""
## Approved Layout Scope
- Scope type: {layout_summary.get('scope_type') or 'not specified'}
- Header row: {layout_summary.get('header_row')}
- Analysis bounds: {json_safe_dumps(layout_summary.get('analysis_bounds') or {})}
- Main blocks approved: {layout_summary.get('main_blocks_count', 0)}
- Context blocks approved: {layout_summary.get('context_blocks_count', 0)}
"""
        if context_labels:
            layout_body += "- Context block labels: " + ", ".join(context_labels) + "\n"
        sections.append(_sec("layout", layout_body, _PRIORITY_LAYOUT))

    if target_template and target_template.get("properties"):
        tmpl_body, _ = _build_template_section(
            target_template,
            cp,
            source_summary,
            structure_analysis or {},
        )
        sections.append(_sec("template", tmpl_body, _PRIORITY_TEMPLATE))

    snippet_body = _section_snippets(context_block_snippets, scoped_names)
    if snippet_body:
        sections.append(_sec("snippets", "\n" + snippet_body, _PRIORITY_SNIPPETS))

    assumptions = interpreted_context.get("assumptions") or []
    evidence = interpreted_context.get("evidence") or []
    if assumptions or (evidence and len(scoped_names) < 3):
        extra_ic = ""
        if assumptions:
            extra_ic += "- Assumptions: " + json_safe_dumps(assumptions[:6]) + "\n"
        if evidence and not scoped_names:
            extra_ic += "- Evidence: " + json_safe_dumps(evidence[:6]) + "\n"
        if extra_ic:
            sections.append(
                _sec("evidence", "\n## Supplementary interpreted notes\n" + extra_ic, _PRIORITY_EVIDENCE)
            )

    if source_graph_view or relationship_context.get("approved"):
        rel_body = """
## Source Graph
Approved relationships — the ONLY trusted topology for cross-source references.
- Never invent joins that are not listed here.
"""
        if isinstance(source_graph_view, dict):
            nodes_preview = source_graph_view.get("nodes") or []
            edges_preview = source_graph_view.get("edges") or []
            if nodes_preview:
                rel_body += "\n- Nodes: " + json_safe_dumps(nodes_preview[:8]) + "\n"
            if edges_preview:
                rel_body += "- Edges: " + json_safe_dumps(edges_preview[:8]) + "\n"
        elif relationship_context.get("approved"):
            rel_body += "\n- Approved relationships: " + json_safe_dumps(
                list(relationship_context.get("approved") or [])[:8]
            ) + "\n"
        sections.append(_sec("relationships", rel_body, _PRIORITY_RELATIONSHIPS))

    if len(sanitized_peer_summaries or cp.get("available_source_summaries") or []) >= 2:
        fc = cp.get("file_relationships")
        if not (isinstance(fc, list) and len(fc) > 0):
            sections.append(
                _sec(
                    "propose_rels",
                    """
## Optional tool: cross-source relationships
When multiple sources are in this job and relationships are not approved, you may call
`{"tool": "discovery.propose_file_relationships", "params": {}}` before heavy layout transforms.
""",
                    _PRIORITY_RELATIONSHIPS,
                )
            )

    guardrail = """
## Empty-Row Filtering Guardrail
- `xls.data.filter_empty` is ONLY for genuinely blank / incomplete rows.
- Numeric zero values are valid populated metrics, NOT blanks.
"""
    sections.append(_sec("guardrail", guardrail, _PRIORITY_MAPPINGS))

    if rules_summary:
        rules_body = "\n## Approved Business Rules\n" + "\n".join(f"- {item}" for item in rules_summary) + "\n"
        sections.append(_sec("rules", rules_body, _PRIORITY_RULES))

    if value_scale_notes:
        vs_body = (
            "\n## Header denomination (value scale)\n"
            + "\n".join(f"- {item}" for item in value_scale_notes)
            + "\n"
        )
        sections.append(_sec("value_scale", vs_body, _PRIORITY_RULES))

    if notes_summary:
        notes_body = "\n## User Notes\n" + "\n".join(f"- {item}" for item in notes_summary) + "\n"
        sections.append(_sec("notes", notes_body, _PRIORITY_USER_NOTES))

    return sections


def build_planner_prompt_result(
    structure_analysis: Dict[str, Any],
    context_packet: Optional[Dict[str, Any]] = None,
    target_template: Optional[Dict[str, Any]] = None,
    examples: Optional[List[Dict[str, Any]]] = None,
    *,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> Dict[str, Any]:
    """Return curated user prompt plus structured briefing for debug UI."""
    sections = _collect_planner_prompt_sections(
        structure_analysis,
        context_packet,
        target_template,
        examples,
        max_chars=max_chars,
    )
    user_prompt, briefing = _finalize_planner_prompt(
        sections,
        max_chars=max_chars,
        context_packet=context_packet,
    )
    return {"user_prompt": user_prompt, "briefing": briefing}


def build_planner_prompt_context(
    structure_analysis: Dict[str, Any],
    context_packet: Optional[Dict[str, Any]] = None,
    target_template: Optional[Dict[str, Any]] = None,
    examples: Optional[List[Dict[str, Any]]] = None,
    *,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> str:
    """Build the curated plan_generator user prompt string."""
    return build_planner_prompt_result(
        structure_analysis,
        context_packet,
        target_template,
        examples,
        max_chars=max_chars,
    )["user_prompt"]
