"""
Pipeline stages: single source of truth for nominal tool ordering and LLM/UI catalog text.

Stages follow: discovery → lightweight prep (intent) → table header shape → block extraction →
layout fill/densify → mapping (intent) → typing → date intent → date normalisation → hygiene →
value standardisation (formats + **post-rename block metric allocation**) → combination readiness → reshape/aggregate →
post-union consolidation (collation.*) → validation.

Does not import tool_validator at module load (avoids cycles). Descriptions for markdown/JSON
are resolved lazily from TOOL_SCHEMAS when formatting.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class PipelineStage(str, Enum):
    """Nominal ingestion pipeline ordering."""

    DISCOVERY = "discovery"
    LIGHTWEIGHT_PREP = "lightweight_prep"
    TABLE_SHAPE = "table_shape"
    BLOCK_EXTRACTION = "block_extraction"
    LAYOUT_FILL = "layout_fill"
    SEMANTIC_MAPPING = "semantic_mapping"
    COLUMN_TYPING = "column_typing"
    DATE_INTERPRETATION = "date_interpretation"
    DATE_NORMALISATION = "date_normalisation"
    HYGIENE = "hygiene"
    VALUE_STANDARDISATION = "value_standardisation"
    COMBINATION_READINESS = "combination_readiness"
    RESHAPING_AGGREGATION = "reshaping_aggregation"
    CONSOLIDATION = "consolidation"
    FINAL_LAYOUT = "final_layout"
    VALIDATION = "validation"


@dataclass(frozen=True)
class StageDefinition:
    stage: PipelineStage
    sort_key: int
    entry_contract: str
    exit_contract: str
    intent_setting: bool
    ux_phase: int  # 1–4: Understand & Extract; Meaning; Clean & Standardise; Shape & Guarantee


STAGE_DEFINITIONS: Tuple[StageDefinition, ...] = (
    StageDefinition(
        PipelineStage.DISCOVERY,
        10,
        "Workbook or sheet context is unknown.",
        "Sheets and structural scan targets are identified.",
        False,
        1,
    ),
    StageDefinition(
        PipelineStage.LIGHTWEIGHT_PREP,
        11,
        "Analyst may set lightweight extraction or hygiene intent before heavy layout work.",
        "Intent for early passes is recorded where applicable (no mandatory tools).",
        True,
        1,
    ),
    StageDefinition(
        PipelineStage.TABLE_SHAPE,
        21,
        "Header rows may be split, repeated in the grid, or need merging before a stable header.",
        "A single logical header row is defined (merge/filter header repeats as needed).",
        False,
        1,
    ),
    StageDefinition(
        PipelineStage.BLOCK_EXTRACTION,
        23,
        "Table bounds and matrix vs rectangular layout may be ambiguous.",
        "Rectangular data blocks are extracted, stacked, or matrix-unpivoted into a working grid.",
        False,
        1,
    ),
    StageDefinition(
        PipelineStage.LAYOUT_FILL,
        25,
        "Merged cells or sparse dimension columns may hide grain.",
        "Merged headers filled and/or sparse dimensions densified without changing analytic grain yet; "
        "block-level metric splitting is deferred until after rename/type_cast (see value_standardisation stage).",
        False,
        1,
    ),
    StageDefinition(
        PipelineStage.SEMANTIC_MAPPING,
        30,
        "Raw column labels do not yet map to business roles.",
        "Each kept column has an agreed role (metric, dimension, date role, discard).",
        True,
        2,
    ),
    StageDefinition(
        PipelineStage.COLUMN_TYPING,
        40,
        "Columns may be strings that should be dates or numbers.",
        "Core columns are typed/renamed; obviously blank columns dropped without changing grain.",
        False,
        2,
    ),
    StageDefinition(
        PipelineStage.DATE_INTERPRETATION,
        45,
        "Date shape (parts, range, period, cadence) may still be ambiguous.",
        "Date roles and chosen interpretation path are fixed (often via mapping; no grain-changing tools).",
        True,
        2,
    ),
    StageDefinition(
        PipelineStage.DATE_NORMALISATION,
        50,
        "Canonical calendar representation is not yet materialised.",
        "One coherent daily or range-resolved date axis exists for downstream hygiene and rollup.",
        False,
        2,
    ),
    StageDefinition(
        PipelineStage.HYGIENE,
        60,
        "Table may contain blanks, totals, duplicates, or junk rows.",
        "Noise rows/columns removed after dates are normalised (see docs/UX_STAGES.md stage 2).",
        False,
        3,
    ),
    StageDefinition(
        PipelineStage.VALUE_STANDARDISATION,
        70,
        "Category labels and display formats may not match the target template.",
        "Enums, fuzzy matches, formats, derived expressions, and—after columns are renamed/typed—"
        "block metric allocation into a summable grain align with analytics conventions.",
        False,
        3,
    ),
    StageDefinition(
        PipelineStage.COMBINATION_READINESS,
        77,
        "Multiple sources may not yet be checked for union-safe columns and grain.",
        "Cross-source union readiness signals recorded where applicable (input tables unchanged).",
        False,
        3,
    ),
    StageDefinition(
        PipelineStage.RESHAPING_AGGREGATION,
        80,
        "Grain may still be wider than required or not yet rolled to target period.",
        "Wide-to-long, transpose if needed, and weekly (or other) aggregation applied as required.",
        False,
        4,
    ),
    StageDefinition(
        PipelineStage.CONSOLIDATION,
        84,
        "Stacked union output may need column order, duplicate keys, or row-level dedupe.",
        "Collation tools have aligned columns and resolved duplicate keys/rows on the combined frame.",
        False,
        4,
    ),
    StageDefinition(
        PipelineStage.FINAL_LAYOUT,
        88,
        "Final frame may not match template column order or uid sort for export.",
        "Columns follow template order (date, uid, supporting, metrics) and rows sorted by uid hierarchy.",
        False,
        4,
    ),
    StageDefinition(
        PipelineStage.VALIDATION,
        90,
        "Schema and totals are not yet checked.",
        "Checksums and/or schema checks recorded for the final frame.",
        False,
        4,
    ),
)

_STAGE_SORT: Dict[PipelineStage, int] = {d.stage: d.sort_key for d in STAGE_DEFINITIONS}


def stage_sort_key(stage: PipelineStage) -> int:
    return _STAGE_SORT[stage]


def stage_for_tool(normalized_tool_name: str) -> Optional[PipelineStage]:
    """Primary pipeline stage for a canonical tool name (after normalize_tool_name)."""
    return TOOL_PRIMARY_STAGE.get(normalized_tool_name)


# Canonical tool name -> primary stage (must cover every key in TOOL_SCHEMAS).
# semantic_mapping + date_interpretation intentionally have no tools (graph / mapping / analyst intent).
TOOL_PRIMARY_STAGE: Dict[str, PipelineStage] = {
    "discovery.inventory": PipelineStage.DISCOVERY,
    "discovery.inspect": PipelineStage.DISCOVERY,
    "discovery.propose_file_relationships": PipelineStage.COMBINATION_READINESS,
    "layout.merge_headers": PipelineStage.TABLE_SHAPE,
    "layout.filter_header_repeats": PipelineStage.TABLE_SHAPE,
    "layout.extract": PipelineStage.BLOCK_EXTRACTION,
    "layout.stack": PipelineStage.BLOCK_EXTRACTION,
    "layout.unpivot_matrix": PipelineStage.BLOCK_EXTRACTION,
    "transform.filter_summaries": PipelineStage.LAYOUT_FILL,
    "transform.fill_merged": PipelineStage.LAYOUT_FILL,
    "transform.unmerge_and_fill": PipelineStage.LAYOUT_FILL,
    "transform.densify": PipelineStage.LAYOUT_FILL,
    "transform.type_cast": PipelineStage.COLUMN_TYPING,
    "transform.rename": PipelineStage.COLUMN_TYPING,
    "transform.add_column": PipelineStage.COLUMN_TYPING,
    "transform.apply_column_rules": PipelineStage.COLUMN_TYPING,
    "transform.infer_block_boundary_columns": PipelineStage.COLUMN_TYPING,
    "transform.align_schema": PipelineStage.COLUMN_TYPING,
    "transform.drop_blank_columns": PipelineStage.COLUMN_TYPING,
    "transform.align_columns": PipelineStage.COLUMN_TYPING,
    "transform.split_column": PipelineStage.COLUMN_TYPING,
    "transform.date_range_to_weekly": PipelineStage.DATE_NORMALISATION,
    "transform.expand_date_range_to_daily": PipelineStage.DATE_NORMALISATION,
    "transform.expand_date_range_to_weekly": PipelineStage.DATE_NORMALISATION,
    "transform.build_date_from_parts": PipelineStage.DATE_NORMALISATION,
    "transform.expand_period_to_daily": PipelineStage.DATE_NORMALISATION,
    "transform.infer_granularity_expand_to_daily": PipelineStage.DATE_NORMALISATION,
    "transform.filter_empty": PipelineStage.HYGIENE,
    "transform.skip_rows": PipelineStage.HYGIENE,
    "transform.drop_columns": PipelineStage.HYGIENE,
    "transform.deduplicate": PipelineStage.HYGIENE,
    "transform.map_values": PipelineStage.VALUE_STANDARDISATION,
    "transform.fuzzy_standardize": PipelineStage.VALUE_STANDARDISATION,
    "transform.format": PipelineStage.VALUE_STANDARDISATION,
    "transform.calculate": PipelineStage.VALUE_STANDARDISATION,
    "transform.scale_values": PipelineStage.VALUE_STANDARDISATION,
    "transform.expand_grouped_block": PipelineStage.VALUE_STANDARDISATION,
    "transform.classify_metric_level": PipelineStage.VALUE_STANDARDISATION,
    "transform.allocate_block_metric": PipelineStage.VALUE_STANDARDISATION,
    "validate.cross_source_union": PipelineStage.COMBINATION_READINESS,
    "transform.unpivot": PipelineStage.RESHAPING_AGGREGATION,
    "transform.union_resolve": PipelineStage.RESHAPING_AGGREGATION,
    "transform.transpose": PipelineStage.RESHAPING_AGGREGATION,
    "transform.aggregate_weekly": PipelineStage.RESHAPING_AGGREGATION,
    "transform.reorder_columns": PipelineStage.FINAL_LAYOUT,
    "transform.sort_rows": PipelineStage.FINAL_LAYOUT,
    "collation.reorder_columns": PipelineStage.CONSOLIDATION,
    "collation.drop_duplicate_rows": PipelineStage.CONSOLIDATION,
    "collation.aggregate_duplicate_keys": PipelineStage.CONSOLIDATION,
    "collation.resolve_duplicate_key_groups": PipelineStage.CONSOLIDATION,
    "verify.checksum": PipelineStage.VALIDATION,
    "verify.schema": PipelineStage.VALIDATION,
}


def assert_primary_stage_coverage(tool_schema_keys: List[str]) -> None:
    """Raise AssertionError if any canonical tool lacks a primary stage."""
    missing = sorted(set(tool_schema_keys) - set(TOOL_PRIMARY_STAGE.keys()))
    extra = sorted(set(TOOL_PRIMARY_STAGE.keys()) - set(tool_schema_keys))
    if missing:
        raise AssertionError(f"TOOL_PRIMARY_STAGE missing keys: {missing}")
    if extra:
        raise AssertionError(f"TOOL_PRIMARY_STAGE has unknown keys vs schemas: {extra}")


def _tool_descriptions() -> Dict[str, str]:
    from sia.tools.tool_validator import TOOL_SCHEMAS

    return {name: (schema.description or "").strip() for name, schema in TOOL_SCHEMAS.items()}


def format_catalog_markdown(max_desc_len: int = 160) -> str:
    """Markdown: ordered stages with contracts and tools (descriptions from TOOL_SCHEMAS)."""
    descriptions = _tool_descriptions()
    lines: List[str] = []
    for defn in STAGE_DEFINITIONS:
        lines.append(
            f"### {defn.stage.value} (order={defn.sort_key}, ux_phase={defn.ux_phase})"
        )
        lines.append(f"- **Intent-setting stage**: {'yes' if defn.intent_setting else 'no'}")
        lines.append(f"- **Entry**: {defn.entry_contract}")
        lines.append(f"- **Exit**: {defn.exit_contract}")
        tools_here = sorted(name for name, st in TOOL_PRIMARY_STAGE.items() if st == defn.stage)
        if tools_here:
            lines.append("- **Tools**:")
            for name in tools_here:
                desc = descriptions.get(name, "")
                if len(desc) > max_desc_len:
                    desc = desc[: max_desc_len - 1] + "…"
                lines.append(f"  - `{name}` — {desc}")
        else:
            lines.append("- **Tools**: _(none — use mapping UI / graph / analyst intent)_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def serialize_catalog_json() -> List[Dict[str, Any]]:
    """Structured catalog for UI / API consumers."""
    descriptions = _tool_descriptions()
    out: List[Dict[str, Any]] = []
    for defn in STAGE_DEFINITIONS:
        tool_names = sorted(name for name, st in TOOL_PRIMARY_STAGE.items() if st == defn.stage)
        tools_here = [{"name": name, "description": descriptions.get(name, "")} for name in tool_names]
        out.append(
            {
                "stage": defn.stage.value,
                "sort_key": defn.sort_key,
                "ux_phase": defn.ux_phase,
                "entry_contract": defn.entry_contract,
                "exit_contract": defn.exit_contract,
                "intent_setting": defn.intent_setting,
                "tools": tools_here,
            }
        )
    return out


PIPELINE_CATALOG_MARKDOWN_HEADER = "## Pipeline stages and tool membership (authoritative)\n\n"


def augment_system_prompt_with_catalog(system_prompt: str) -> str:
    """Append the machine-generated staging catalog after the static system prompt."""
    block = PIPELINE_CATALOG_MARKDOWN_HEADER + format_catalog_markdown()
    return f"{system_prompt.rstrip()}\n\n{block}"
