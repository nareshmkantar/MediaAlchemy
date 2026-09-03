from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .job_run_ledger import build_job_run_ledger_summary
from .value_scale import enrich_mapping_value_scale, value_scale_summary_from_mappings
from .target_template_utils import (
    is_no_match_target,
    mapping_targets_requiring_source,
    pre_transform_target_columns,
    template_column_rules_summary,
    template_union_grain_columns,
    validate_template_shape,
)


KEEP_DECISIONS = {"keep", "metadata", "context", "use as context"}
MAIN_BLOCK_DECISIONS = {"keep", "main data", "approved"}
CONTEXT_BLOCK_DECISIONS = {"context", "metadata", "use as context"}
DISCARD_BLOCK_DECISIONS = {"discard", "ignore", "noise"}


def mapping_is_excluded(item: Optional[Dict[str, Any]]) -> bool:
    """True when Guided Setup marked the source column Exclude / Discard."""
    if not isinstance(item, dict):
        return False
    role = str(item.get("role") or "").strip().lower()
    decision = str(item.get("decision") or "").strip().lower()
    return role == "exclude" or decision == "discard"


def excluded_source_column_name(item: Optional[Dict[str, Any]]) -> str:
    if not mapping_is_excluded(item):
        return ""
    return str(
        (item or {}).get("source_column")
        or (item or {}).get("column_name")
        or ""
    ).strip()


def _header_is_renamed_keep_target(
    name_lower: str,
    approved_mappings: Optional[List[Dict[str, Any]]],
    present_lower: set[str],
) -> bool:
    """True when ``name_lower`` is a keep target already realized on the frame.

    Used so Exclude of a raw Amazon ``date`` column does not delete ``date`` after
    ``orderStartDate`` was renamed onto that template name. Before rename, the
    keep source is still present, so the unused raw ``date`` column is dropped.
    """
    for item in approved_mappings or []:
        if not isinstance(item, dict) or mapping_is_excluded(item):
            continue
        target = str(item.get("target_column") or "").strip()
        if not target or is_no_match_target(target) or target.lower() != name_lower:
            continue
        source = str(item.get("source_column") or item.get("column_name") or "").strip().lower()
        if not source or source == name_lower:
            return True
        if source not in present_lower:
            return True
    return False


def drop_excluded_mapping_columns(
    df: Optional[pd.DataFrame],
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """Drop Exclude/Discard source columns, matching headers case-insensitively.

    After rename, a kept source (e.g. ``orderStartDate`` → ``date``) can share the
    name of a different excluded source (Amazon DSP's unused ``date`` column).
    Do not drop a header that is already the realized keep-mapping target.
    """
    if df is None:
        return df, []
    col_by_lower = {str(col).strip().lower(): col for col in df.columns}
    present_lower = set(col_by_lower)
    drop: List[Any] = []
    seen: set[str] = set()
    for item in approved_mappings or []:
        name = excluded_source_column_name(item)
        if not name:
            continue
        if _header_is_renamed_keep_target(name.lower(), approved_mappings, present_lower):
            continue
        actual = col_by_lower.get(name.lower())
        if actual is None or str(actual) in seen:
            continue
        seen.add(str(actual))
        drop.append(actual)
    if not drop:
        return df, []
    return df.drop(columns=drop, errors="ignore"), [str(c) for c in drop]


def _layout_registry_block_role(block: Dict[str, Any]) -> Optional[str]:
    """Return ``main``, ``context``, or ``None`` (discarded). User decision overrides AI category."""
    decision = str(block.get("decision") or "").strip().lower()
    category = str(block.get("block_category") or block.get("category") or "").strip().lower()
    cat_compact = category.replace(" ", "").replace("_", "")

    if decision in CONTEXT_BLOCK_DECISIONS:
        return "context"
    if decision in DISCARD_BLOCK_DECISIONS:
        return None
    if decision in MAIN_BLOCK_DECISIONS:
        return "main"
    if cat_compact == "maindata":
        return "main"
    if category in {"metadata", "context", "supporting meta", "footnotes", "footnote"}:
        return "context"
    return None

CONTEXT_PACKET_SCHEMA_VERSION = 2

_CONTEXT_ONLY_EXACT = {
    "summary",
    "notes",
    "note",
    "readme",
    "legend",
    "glossary",
    "instructions",
    "metadata",
}
_CONTEXT_ONLY_PREFIXES = ("notes_", "note_", "readme_", "legend_", "meta_")
_CONTEXT_ONLY_SUFFIXES = ("_notes", "_note", "_summary", "_readme", "_legend")
_CONTEXT_ONLY_SUBSTRINGS = ("_notes_", "notes_global")


def is_context_only_sheet_name(sheet_name: Optional[str]) -> bool:
    """True when a workbook tab is reference/context (not a main data table to transform)."""
    if not sheet_name:
        return False
    normalized = str(sheet_name).strip().lower().replace(" ", "_")
    if not normalized:
        return False
    if normalized in _CONTEXT_ONLY_EXACT:
        return True
    if any(normalized.startswith(prefix) for prefix in _CONTEXT_ONLY_PREFIXES):
        return True
    if any(normalized.endswith(suffix) for suffix in _CONTEXT_ONLY_SUFFIXES):
        return True
    if any(token in normalized for token in _CONTEXT_ONLY_SUBSTRINGS):
        return True
    return False


def source_registry_entry_is_main_data(source: Optional[Dict[str, Any]]) -> bool:
    """True when a registry row should run through transform/plan/union (not context-only)."""
    if not isinstance(source, dict):
        return False
    if source.get("contains_reference_data") or source.get("contains_summary_only"):
        return False
    if is_context_only_sheet_name(source.get("sheet_name")):
        return False
    raw_main = source.get("contains_main_data")
    if raw_main is None:
        return True
    return bool(raw_main)


def source_is_main_data_for_processing(job: Dict[str, Any], source: Optional[Dict[str, Any]]) -> bool:
    """Layout save wins over registry tab heuristics when demarcation exists for the source."""
    if not isinstance(source, dict):
        return False
    sid = str(source.get("source_id") or "").strip()
    layouts = [
        row
        for row in (job.get("layout_registry") or [])
        if isinstance(row, dict) and str(row.get("source_id") or "") == sid
    ]
    if layouts:
        return any(_layout_registry_block_role(row) == "main" for row in layouts)
    return source_registry_entry_is_main_data(source)


def refresh_job_source_context_flags(job: Dict[str, Any]) -> None:
    """Reconcile source_registry processing flags: saved layout overrides tab-name heuristics."""
    registry = list(job.get("source_registry") or [])
    layout_by_source: Dict[str, List[Dict[str, Any]]] = {}
    for row in job.get("layout_registry") or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("source_id") or "").strip()
        if sid:
            layout_by_source.setdefault(sid, []).append(row)

    changed = False
    for row in registry:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("source_id") or "").strip()
        layouts = layout_by_source.get(sid) or []
        if layouts:
            main_kept = sum(1 for block in layouts if _layout_registry_block_role(block) == "main")
            context_kept = sum(1 for block in layouts if _layout_registry_block_role(block) == "context")
            want_main = main_kept > 0
            want_ref = not want_main and context_kept > 0
            want_summary = want_ref
        else:
            context_only = is_context_only_sheet_name(row.get("sheet_name"))
            want_main = not context_only
            want_ref = context_only
            want_summary = context_only
        if (
            row.get("contains_main_data") != want_main
            or row.get("contains_reference_data") != want_ref
            or row.get("contains_summary_only") != want_summary
        ):
            row["contains_main_data"] = want_main
            row["contains_reference_data"] = want_ref
            row["contains_summary_only"] = want_summary
            changed = True
    if changed:
        job["source_registry"] = registry


@dataclass
class SourceMetadata:
    source_id: str
    file_name: str
    file_path: str
    file_id: str = ""
    sheet_name: Optional[str] = None
    sheet_order: int = 0
    source_type: str = "unknown"
    variable_type: str = ""
    business_domain: str = ""
    brand: str = ""
    market: str = ""
    campaign: str = ""
    reporting_period: str = ""
    modeling_period_start: str = ""
    modeling_period_end: str = ""
    publisher: str = ""
    owner: str = ""
    uid: List[str] = field(default_factory=list)
    date_granularity: str = ""
    aggregation_logic: str = ""
    contains_main_data: bool = False
    contains_reference_data: bool = False
    contains_summary_only: bool = False
    user_notes: List[str] = field(default_factory=list)
    status: str = "discovered"
    fingerprint: str = ""
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LayoutDecision:
    layout_id: str
    source_id: str
    block_id: str
    block_label: str = ""
    block_category: str = "main_data"
    start_row: int = 0
    end_row: int = 0
    start_col: int = 0
    end_col: int = 0
    header_row: int = 0
    decision: str = "approved"
    confidence: float = 1.0
    approved_by: str = "system"
    approved_at: str = ""
    layout_version: int = 1
    coordinates: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["coordinates"]:
            data["coordinates"] = {
                "start_row": self.start_row,
                "end_row": self.end_row,
                "start_col": self.start_col,
                "end_col": self.end_col,
                "header_row": self.header_row,
            }
        return data


@dataclass
class MappingDecision:
    mapping_id: str
    source_id: str
    source_column: str
    source_column_type: str = ""
    classification: str = ""
    target_column: str = "No match"
    target_match_method: str = "none"
    target_match_confidence: float = 0.0
    decision: str = "Discard"
    default_value_rule: str = ""
    format_rule: str = ""
    derivation_rule: str = ""
    approved_by: str = "system"
    approved_at: str = ""
    mapping_version: int = 1
    confidence: float = 0.0
    reasoning: str = ""
    role: str = ""
    date_semantic: str = ""
    block_id: str = ""
    block_label: str = ""
    value_scale: float = 1.0
    value_scale_note: str = ""
    metric_currency: str = ""
    output_alias: str = ""
    split_from: str = ""
    packed_source_column: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BusinessRule:
    rule_id: str
    target_column: str
    rule_type: str
    rule_expression: str
    rule_description: str = ""
    priority: int = 100
    is_mandatory: bool = False
    applies_to_sheet: Optional[str] = None
    applies_to_source_id: Optional[str] = None
    source_column: Optional[str] = None
    approved_by: str = "system"
    approved_at: str = ""
    rule_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def effective_target_date_granularity(x_scope: Optional[Dict[str, Any]]) -> str:
    """Return template date grain from ``x_scope`` (supports ``date_granularity_required``)."""
    if not isinstance(x_scope, dict):
        return ""
    for key in ("date_granularity", "date_granularity_required"):
        raw = str(x_scope.get(key) or "").strip()
        if raw:
            return raw
    return ""


def _norm_date_shape(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s in ("period_span", "range_pair", "range", "two_column_span"):
        return "period_span"
    if s in ("single_timestamp", "point", "single", "event"):
        return "single_timestamp"
    if s in ("period_text", "range_text"):
        return "period_text"
    if s in ("date_parts", "parts", "ymd"):
        return "date_parts"
    return "unknown"


def compute_date_granularity_alignment(
    source_date_granularity: str,
    target_date_granularity: str,
    *,
    source_date_shape: str = "",
) -> Dict[str, Any]:
    """Derive planner-facing obligation from Guided Setup source grain vs template target grain."""
    src_raw = str(source_date_granularity or "").strip()
    tgt_raw = str(target_date_granularity or "").strip()
    src = src_raw.lower()
    tgt = tgt_raw.lower()
    shape_n = _norm_date_shape(source_date_shape)

    def _norm_source(s: str) -> str:
        if not s:
            return "unknown"
        if s in ("daily", "day", "d", "date"):
            return "daily"
        if s in ("weekly", "week", "w"):
            return "weekly"
        if s in ("monthly", "month", "m"):
            return "monthly"
        if s in ("quarterly", "quarter", "q"):
            return "quarterly"
        if s in ("range", "date_range", "daterange", "flight", "flighting"):
            return "range"
        return s

    def _norm_target(s: str) -> str:
        if not s:
            return "unknown"
        if s in ("weekly", "week"):
            return "weekly"
        if s in ("daily", "day", "date"):
            return "daily"
        if s in ("monthly", "month"):
            return "monthly"
        if s in ("quarterly", "quarter"):
            return "quarterly"
        return s

    ns = _norm_source(src)
    nt = _norm_target(tgt)
    out: Dict[str, Any] = {
        "source_date_granularity": src_raw or "",
        "target_date_granularity": tgt_raw or "",
        "normalized_source": ns,
        "normalized_target": nt,
        "source_date_shape": str(source_date_shape or "").strip(),
        "normalized_date_shape": shape_n,
        "requires_weekly_rollup_step": False,
        "recommended_primary_tool": "",
        "planner_obligation": "",
    }

    if nt != "weekly":
        if nt == "unknown":
            out["planner_obligation"] = (
                "Target template does not specify a weekly date_granularity in x_scope; "
                "follow the template's stated grain."
            )
        else:
            out["planner_obligation"] = (
                f"Target date granularity is {tgt_raw or nt!r}. Align transforms and verify.schema "
                "to that grain; weekly rollup tools apply only when the template requires weekly output."
            )
        return out

    # Target is weekly — date span (two columns) from Guided Setup overrides coarse grain
    if nt == "weekly" and shape_n == "period_span":
        out["requires_weekly_rollup_step"] = True
        out["normalized_source"] = "range"
        out["recommended_primary_tool"] = (
            "transform.expand_date_range_to_daily → transform.aggregate_weekly"
        )
        out["planner_obligation"] = (
            "Analyst set **Date interpretation** to **Date span (start + end columns)** and TARGET is WEEKLY. "
            "Use `transform.expand_date_range_to_daily` (or `transform.date_range_to_weekly` with `granularity='daily'`) "
            "with the **physical** `start_date_col` and `end_date_col` (two distinct names). Do **not** rename both "
            "into a single `date` column before this step. Then `transform.aggregate_weekly` on `calendar_date`."
        )
        return out

    # Target is weekly
    if ns in ("range",):
        out["requires_weekly_rollup_step"] = True
        out["recommended_primary_tool"] = (
            "transform.date_range_to_weekly (granularity=daily) → transform.aggregate_weekly"
        )
        out["planner_obligation"] = (
            "SOURCE is DATE RANGE / flighting (start and end) and TARGET is WEEKLY. "
            "Expand each row to **daily** points: `transform.date_range_to_weekly` with "
            "`start_date_col`, `end_date_col`, `value_cols`, and `granularity='daily'` "
            "(metrics are split evenly across inclusive day count), producing `calendar_date`. "
            "Then `transform.aggregate_weekly` with `date_col='calendar_date'` (and the same metrics / group_by). "
            "You may instead use a single `date_range_to_weekly` with default weekly output only if the template "
            "does not require an explicit daily intermediate. Place these after rename/type_cast/format and "
            "before verify.schema."
        )
    elif ns in ("monthly", "quarterly"):
        out["requires_weekly_rollup_step"] = True
        out["recommended_primary_tool"] = "transform.expand_period_to_daily → transform.aggregate_weekly"
        out["planner_obligation"] = (
            "SOURCE is MONTHLY or QUARTERLY and TARGET is WEEKLY. "
            "Include transform.expand_period_to_daily then transform.aggregate_weekly "
            "before verify.schema (or a single transform.infer_granularity_expand_to_daily then "
            "transform.aggregate_weekly when you want cadence inferred from the date column)."
        )
    elif ns in ("daily", "unknown"):
        out["requires_weekly_rollup_step"] = True
        out["recommended_primary_tool"] = "transform.aggregate_weekly"
        if ns == "unknown":
            out["planner_obligation"] = (
                "TARGET is WEEKLY but source date_granularity was NOT SET in Guided Setup. "
                "Prefer transform.infer_granularity_expand_to_daily on the main date column (with value_cols) "
                "before transform.aggregate_weekly when rows may be monthly/quarterly/weekly totals; "
                "it infers cadence from sorted-unique date gaps and expands to daily. "
                "If the sheet is clearly one row per calendar day after format, use transform.aggregate_weekly "
                "(metric_rules, date_col, group_by_cols) alone BEFORE verify.schema."
            )
        else:
            out["planner_obligation"] = (
                "TARGET is WEEKLY and SOURCE was marked DAILY (per-day rows). "
                "You MUST add transform.aggregate_weekly after rename/type_cast/format of the date column "
                "and BEFORE verify.schema. Do not end the plan at ISO daily dates only."
            )
    elif ns == "weekly":
        out["requires_weekly_rollup_step"] = False
        out["planner_obligation"] = (
            "SOURCE and TARGET are both WEEKLY. If the dataframe still has one row per calendar day, "
            "add transform.aggregate_weekly before verify.schema; otherwise ensure a single Monday-aligned "
            "week_start column as required by the template."
        )
    else:
        out["requires_weekly_rollup_step"] = True
        out["recommended_primary_tool"] = "transform.aggregate_weekly"
        out["planner_obligation"] = (
            f"TARGET is WEEKLY; source grain is labeled {src_raw or ns!r}. "
            "Pick the correct weekly path (aggregate_weekly, date_range_to_weekly, or expand+daily+aggregate) "
            "and place it BEFORE verify.schema."
        )
    return out


@dataclass
class ContextPacket:
    job_id: str
    source_metadata: Dict[str, Any]
    available_sources: List[Dict[str, Any]] = field(default_factory=list)
    available_source_summaries: List[Dict[str, Any]] = field(default_factory=list)
    approved_layout: Dict[str, Any] = field(default_factory=dict)
    context_block_snippets: List[Dict[str, Any]] = field(default_factory=list)
    interpreted_context: Dict[str, Any] = field(default_factory=dict)
    approved_mappings: List[Dict[str, Any]] = field(default_factory=list)
    business_rules: List[Dict[str, Any]] = field(default_factory=list)
    target_template: Dict[str, Any] = field(default_factory=dict)
    pre_transform_target_columns: List[str] = field(default_factory=list)
    file_relationships: List[Dict[str, Any]] = field(default_factory=list)
    user_notes: List[str] = field(default_factory=list)
    unresolved_items: List[Dict[str, Any]] = field(default_factory=list)
    lineage: Dict[str, Any] = field(default_factory=dict)
    approval_state: Dict[str, Any] = field(default_factory=dict)
    # When True, infer_relationships may still record proposals but must not pause the graph
    # for FILE_RELATIONSHIP_REVIEW (post-execution / web-layer review handles collation).
    defer_graph_file_relationship_review: bool = False
    job_run_ledger_summary: Dict[str, Any] = field(default_factory=dict)
    context_fingerprint: Optional[str] = None
    _schema_version: Optional[int] = field(default=None, compare=False, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        if out.get("_schema_version") is None:
            out.pop("_schema_version", None)
        return out

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ContextPacket":
        """Construct from a serialized packet dict (tolerates unknown / meta keys)."""
        row = dict(data or {})
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in row.items() if k in known}
        if "job_id" not in filtered:
            filtered["job_id"] = str(row.get("job_id") or "")
        return cls(**filtered)

    def planning_summary(self) -> Dict[str, Any]:
        interpreted_fields = dict((self.interpreted_context or {}).get("fields") or {})
        effective_source_summary = {
            "file_name": self.source_metadata.get("file_name"),
            "sheet_name": self.source_metadata.get("sheet_name"),
            "source_type": self.source_metadata.get("source_type"),
            "variable_type": self.source_metadata.get("variable_type") or self.source_metadata.get("source_type"),
            "modeling_period_start": self.source_metadata.get("modeling_period_start"),
            "modeling_period_end": self.source_metadata.get("modeling_period_end"),
            "uid": self.source_metadata.get("uid", []),
            "date_granularity": self.source_metadata.get("date_granularity"),
            "date_shape": self.source_metadata.get("date_shape"),
            "range_start_column": self.source_metadata.get("range_start_column"),
            "range_end_column": self.source_metadata.get("range_end_column"),
            "aggregation_logic": self.source_metadata.get("aggregation_logic"),
            "publisher": self.source_metadata.get("publisher"),
            "market": self.source_metadata.get("market"),
            "brand": self.source_metadata.get("brand"),
            "campaign": self.source_metadata.get("campaign"),
            "owner": self.source_metadata.get("owner"),
            "media_hierarchy": (
                (self.source_metadata.get("extra_metadata") or {}).get("media_hierarchy")
                if isinstance(self.source_metadata.get("extra_metadata"), dict)
                else self.source_metadata.get("media_hierarchy")
            ),
        }
        for key, value in interpreted_fields.items():
            if key in effective_source_summary and not str(effective_source_summary.get(key) or "").strip():
                effective_source_summary[key] = value

        mapped_columns = []
        excluded_columns = []
        unresolved_targets = []
        for mapping in self.approved_mappings:
            source_column = mapping.get("source_column", "")
            target_column = mapping.get("target_column", "")
            decision = str(mapping.get("decision", "")).strip().lower()
            if mapping_is_excluded(mapping) and source_column:
                excluded_columns.append(source_column)
            elif decision in KEEP_DECISIONS and target_column and target_column != "No match":
                mapped_columns.append(f"{source_column} -> {target_column}")

        template_properties = self.target_template.get("properties", {}) if isinstance(self.target_template, dict) else {}
        need_source_map = (
            sorted(mapping_targets_requiring_source(self.target_template))
            if isinstance(self.target_template, dict) and template_properties
            else []
        )
        keep_decisions = {"keep", "metadata", "context", "use as context"}
        mapped_targets = {
            mapping.get("target_column")
            for mapping in self.approved_mappings
            if mapping.get("target_column")
            and mapping.get("target_column") != "No match"
            and str(mapping.get("decision", "")).strip().lower() in keep_decisions
        }
        unresolved_targets.extend([target for target in need_source_map if target not in mapped_targets])

        x_scope: Dict[str, Any] = {}
        if isinstance(self.target_template, dict):
            xs = self.target_template.get("x_scope")
            if isinstance(xs, dict):
                x_scope = xs
        template_uid_hierarchy = list(x_scope.get("uid_hierarchy") or [])
        template_metrics = list(x_scope.get("metrics") or [])
        template_supporting_columns = list(x_scope.get("supporting_columns") or [])
        template_column_rules = template_column_rules_summary(self.target_template if isinstance(self.target_template, dict) else {})

        rules_summary = []
        for rule in sorted(self.business_rules, key=lambda item: item.get("priority", 100)):
            target = rule.get("target_column", "")
            rule_type = rule.get("rule_type", "")
            expr = rule.get("rule_expression", "")
            if target and rule_type:
                rules_summary.append(f"{target}: {rule_type} -> {expr}")

        tgt_grain_eff = effective_target_date_granularity(x_scope)
        date_granularity_alignment = compute_date_granularity_alignment(
            str(effective_source_summary.get("date_granularity") or ""),
            tgt_grain_eff,
            source_date_shape=str(effective_source_summary.get("date_shape") or ""),
        )

        return {
            "source_summary": effective_source_summary,
            "layout_summary": {
                "header_row": self.approved_layout.get("header_row"),
                "analysis_bounds": self.approved_layout.get("analysis_bounds") or self.approved_layout.get("bounds"),
                "scope_type": self.approved_layout.get("scope_type"),
                "main_blocks_count": len(self.approved_layout.get("main_blocks") or []),
                "context_blocks_count": len(self.approved_layout.get("context_blocks") or []),
                "context_block_labels": [
                    str(block.get("block_label") or block.get("block_id") or "context_block")
                    for block in (self.approved_layout.get("context_blocks") or [])[:8]
                    if isinstance(block, dict)
                ],
                "context_block_snippets_count": len(self.context_block_snippets or []),
                "context_block_preview": [
                    {
                        "label": snippet.get("block_label") or snippet.get("block_id") or "context_block",
                        "summary": snippet.get("summary") or "",
                        "text_preview": list(snippet.get("text_preview") or []),
                    }
                    for snippet in (self.context_block_snippets or [])[:5]
                    if isinstance(snippet, dict)
                ],
            },
            "interpreted_context": {
                "fields": interpreted_fields,
                "scoped_fields": dict((self.interpreted_context or {}).get("scoped_fields") or {}),
                "assumptions": list((self.interpreted_context or {}).get("assumptions") or [])[:10],
                "evidence": list((self.interpreted_context or {}).get("evidence") or [])[:12],
            },
            "mapping_summary": {
                "mapped_columns": mapped_columns[:20],
                "mapped_count": len(mapped_columns),
                "excluded_columns": excluded_columns[:20],
                "unresolved_target_columns": unresolved_targets[:20],
                "value_scale_notes": value_scale_summary_from_mappings(self.approved_mappings)[:12],
            },
            "rules_summary": rules_summary[:20],
            "user_notes": self.user_notes[:10],
            "available_sources_count": len(self.available_sources),
            "relationship_summary": {
                "count": len(self.file_relationships),
                "approved": len([item for item in self.file_relationships if str(item.get("status", "")).lower() in {"approved", "active"}]),
            },
            "template_uid_hierarchy": template_uid_hierarchy,
            "template_metrics": template_metrics,
            "template_supporting_columns": template_supporting_columns,
            "template_column_rules": template_column_rules,
            "pre_transform_target_columns": list(self.pre_transform_target_columns or []),
            "date_granularity_alignment": date_granularity_alignment,
        }

    def canonical_planning_view(
        self,
        mapping_supplement: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Canonical, authoritative derived view for planner/executor prompts.

        This is the ONLY approved derived view of a ContextPacket. It merges:
        - the core planning_summary sections (source, layout, interpreted context,
          mappings, rules, notes, relationships, template info),
        - optional ``mapping_supplement`` fields captured during the mapping stage
          (``prepared_columns``, ``header_derivation``, ``sparse_dimension_columns``).

        Nodes that need mapping-stage-specific data must contribute it as a
        supplement instead of replacing ``planning_summary``.
        """
        view = self.planning_summary()
        supplement = mapping_supplement or {}
        source_summary = dict(view.get("source_summary") or {})
        if "prepared_columns" in supplement:
            source_summary["prepared_columns"] = list(supplement.get("prepared_columns") or [])
        if "header_derivation" in supplement:
            source_summary["header_derivation"] = supplement.get("header_derivation") or {}
        if "sparse_dimension_columns" in supplement:
            source_summary["sparse_dimension_columns"] = list(
                supplement.get("sparse_dimension_columns") or []
            )
        if "merged_metric_ranges" in supplement:
            source_summary["merged_metric_ranges"] = list(
                supplement.get("merged_metric_ranges") or []
            )
        if supplement.get("date_column_observations"):
            source_summary["date_column_observations"] = list(supplement.get("date_column_observations") or [])
        if supplement.get("date_cadence_summary"):
            source_summary["date_cadence_summary"] = str(supplement.get("date_cadence_summary") or "")
        if supplement.get("date_granularity_mismatch_note"):
            source_summary["date_granularity_mismatch_note"] = str(supplement.get("date_granularity_mismatch_note") or "")
        view["source_summary"] = source_summary

        view["relationship_context"] = {
            "approved": list(self.file_relationships or []),
            "count": len(self.file_relationships or []),
        }
        view["artifact_context"] = dict((self.lineage or {}))
        return view


def build_canonical_planning_view(
    context_packet: Optional[Dict[str, Any]],
    mapping_supplement: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the canonical planning view from a context_packet dict.

    Accepts the serialized packet dict returned by ``build_context_packet`` and
    returns the canonical view. Unknown keys are tolerated so future extensions
    do not break callers.
    """
    if not isinstance(context_packet, dict) or not context_packet:
        return {}
    try:
        packet = ContextPacket.from_dict(context_packet)
    except TypeError:
        return dict(context_packet.get("planning_summary") or {})
    view = packet.canonical_planning_view(mapping_supplement=mapping_supplement)
    from sia.context.planner_context import apply_boundary_sanitization_to_view

    sid = str(
        (context_packet.get("lineage") or {}).get("source_id")
        or (context_packet.get("source_metadata") or {}).get("source_id")
        or ""
    ).strip()
    return apply_boundary_sanitization_to_view(view, context_packet, active_source_id=sid)


def make_source_id(job_id: Optional[str], file_id: Optional[str], sheet_name: Optional[str], file_path: Optional[str] = None) -> str:
    scope_id = str(sheet_name or "default")
    if job_id and file_id:
        return f"{job_id}:{file_id}:{scope_id}"
    return f"{Path(file_path or file_id or 'source').stem}:{scope_id}"


def create_source_registry(
    file_path: str,
    filename: str,
    sheets: Optional[List[str]] = None,
    job_id: Optional[str] = None,
    file_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    normalized_sheets = sheets or [None]
    registry = []
    normalized_file_id = str(file_id or Path(file_path).stem)
    for idx, sheet_name in enumerate(normalized_sheets):
        source_id = make_source_id(job_id, normalized_file_id, sheet_name, file_path=file_path)
        context_only = is_context_only_sheet_name(sheet_name)
        registry.append(SourceMetadata(
            source_id=source_id,
            file_id=normalized_file_id,
            file_name=filename,
            file_path=file_path,
            sheet_name=sheet_name,
            sheet_order=idx,
            contains_main_data=not context_only,
            contains_reference_data=context_only,
            contains_summary_only=context_only,
            fingerprint=f"{normalized_file_id}:{filename}:{sheet_name or 'default'}",
        ).to_dict())
    return registry


def normalize_mapping_records(records: Optional[List[Dict[str, Any]]], source_id: str) -> List[Dict[str, Any]]:
    normalized = []
    for idx, record in enumerate(records or []):
        if not isinstance(record, dict):
            continue
        role = str(record.get("role") or "").strip()
        decision = str(record.get("decision") or "").strip()
        target_column = str(record.get("target_column") or "No match")
        if mapping_is_excluded({"role": role, "decision": decision}):
            role = "exclude"
            decision = "Discard"
            target_column = "No match"
        elif not decision:
            decision = "Discard"
        normalized.append(enrich_mapping_value_scale(MappingDecision(
            mapping_id=str(record.get("mapping_id") or f"{source_id}:mapping:{idx}"),
            source_id=str(record.get("source_id") or source_id),
            source_column=str(record.get("source_column") or record.get("column_name") or ""),
            source_column_type=str(record.get("source_column_type") or record.get("column_type") or ""),
            classification=str(record.get("classification") or ""),
            target_column=target_column,
            target_match_method=str(record.get("target_match_method") or "none"),
            target_match_confidence=float(record.get("target_match_confidence") or 0.0),
            decision=decision,
            default_value_rule=str(record.get("default_value_rule") or ""),
            format_rule=str(record.get("format_rule") or ""),
            derivation_rule=str(record.get("derivation_rule") or ""),
            confidence=float(record.get("confidence") or 0.0),
            reasoning=str(record.get("reasoning") or ""),
            role=role,
            date_semantic=str(record.get("date_semantic") or ""),
            block_id=str(record.get("block_id") or ""),
            block_label=str(record.get("block_label") or ""),
            value_scale=float(record.get("value_scale") or 1.0),
            value_scale_note=str(record.get("value_scale_note") or ""),
            metric_currency=str(record.get("metric_currency") or ""),
            output_alias=str(record.get("output_alias") or ""),
            split_from=str(record.get("split_from") or ""),
            packed_source_column=str(
                record.get("packed_source_column") or record.get("split_from") or ""
            ),
            approved_by=str(record.get("approved_by") or "system"),
            approved_at=str(record.get("approved_at") or ""),
            mapping_version=int(record.get("mapping_version") or 1),
        ).to_dict()))
    return normalized


def derive_date_context_from_mappings(mappings: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Derive source date metadata from per-column mapping semantics."""
    rows = [m for m in (mappings or []) if isinstance(m, dict)]
    kept = [
        m for m in rows
        if str(m.get("decision") or "").strip().lower() != "discard"
        and str(m.get("target_column") or "").strip().lower() != "no match"
    ]

    def _src_for(tag: str) -> str:
        for item in kept:
            if str(item.get("date_semantic") or "").strip().lower() == tag:
                return str(item.get("source_column") or item.get("column_name") or "").strip()
        return ""

    start_col = _src_for("range_start")
    end_col = _src_for("range_end")
    has_period_text = bool(_src_for("period_text"))
    has_point = bool(_src_for("point_in_time"))
    has_parts = any(
        bool(_src_for(tag))
        for tag in ("part_year", "part_month", "part_day", "part_quarter")
    )

    out: Dict[str, Any] = {
        "date_shape": "unknown",
        "range_start_column": "",
        "range_end_column": "",
        "date_granularity": "",
    }
    if start_col and end_col and start_col != end_col:
        out["date_shape"] = "period_span"
        out["range_start_column"] = start_col
        out["range_end_column"] = end_col
        out["date_granularity"] = "range"
    elif has_parts:
        out["date_shape"] = "date_parts"
    elif has_period_text:
        out["date_shape"] = "period_text"
    elif has_point:
        out["date_shape"] = "single_timestamp"
    return out


def normalize_business_rules(records: Optional[List[Dict[str, Any]]], source_id: Optional[str] = None) -> List[Dict[str, Any]]:
    normalized = []
    for idx, record in enumerate(records or []):
        if not isinstance(record, dict):
            continue
        target_column = str(record.get("target_column") or "").strip()
        rule_type = str(record.get("rule_type") or "").strip()
        if not target_column or not rule_type:
            continue
        normalized.append(BusinessRule(
            rule_id=str(record.get("rule_id") or f"{source_id or 'global'}:rule:{idx}"),
            target_column=target_column,
            rule_type=rule_type,
            rule_expression=str(record.get("rule_expression") or record.get("value") or ""),
            rule_description=str(record.get("rule_description") or ""),
            priority=int(record.get("priority") or 100),
            is_mandatory=bool(record.get("is_mandatory", False)),
            applies_to_sheet=record.get("applies_to_sheet"),
            applies_to_source_id=record.get("applies_to_source_id") or source_id,
            source_column=record.get("source_column"),
            approved_by=str(record.get("approved_by") or "system"),
            approved_at=str(record.get("approved_at") or ""),
            rule_version=int(record.get("rule_version") or 1),
        ).to_dict())
    return normalized


def _normalize_yyyy_mm_dd(val: Any) -> str:
    """Normalize template / API dates to YYYY-MM-DD for HTML date inputs and metadata."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]
    return s


def _merge_scope_hints_from_template(
    source_metadata: Dict[str, Any],
    target_template: Dict[str, Any],
) -> Dict[str, Any]:
    """
    When the user has not saved scope fields on the source, copy hints from
    ``x_scope`` on the active template: modeling period dates and ``variable_type``.

    This runs even when ``validate_template_shape`` fails, so partial or
    in-progress templates still contribute ``x_scope`` defaults.
    """
    out = dict(source_metadata or {})
    if not isinstance(target_template, dict) or not target_template:
        return out
    xs = target_template.get("x_scope") if isinstance(target_template.get("x_scope"), dict) else {}
    tpl_vt = str(xs.get("variable_type") or "").strip()
    if tpl_vt and not str(out.get("variable_type") or "").strip():
        out["variable_type"] = tpl_vt
    mp = xs.get("modeling_period") if isinstance(xs.get("modeling_period"), dict) else {}
    tpl_start = _normalize_yyyy_mm_dd(
        mp.get("start_date")
        or mp.get("startDate")
        or mp.get("start")
        or xs.get("modeling_period_start")
        or xs.get("modeling_start_date")
        or xs.get("modelling_start_date")
    )
    tpl_end = _normalize_yyyy_mm_dd(
        mp.get("end_date")
        or mp.get("endDate")
        or mp.get("end")
        or xs.get("modeling_period_end")
        or xs.get("modeling_end_date")
        or xs.get("modelling_end_date")
    )
    if tpl_start and not str(out.get("modeling_period_start") or "").strip():
        out["modeling_period_start"] = tpl_start
    if tpl_end and not str(out.get("modeling_period_end") or "").strip():
        out["modeling_period_end"] = tpl_end
    return out


def build_context_packet(
    job: Dict[str, Any],
    target_template: Optional[Dict[str, Any]] = None,
    selected_sheet: Optional[str] = None,
    selected_source_id: Optional[str] = None,
    *,
    defer_file_relationship_graph_hitl: bool = False,
) -> Dict[str, Any]:
    available_sources = list(job.get("source_registry") or [])
    source_metadata = _select_source_metadata(available_sources, selected_sheet, selected_source_id)
    active_template = target_template or {}
    ok_template, _ = validate_template_shape(active_template)
    source_metadata = _merge_scope_hints_from_template(source_metadata, active_template)
    # Attach hierarchy registration (publisher grain + accepted combined fields)
    try:
        from sia.agent.hierarchy_register import get_hierarchy_entry_for_source

        sid_hint = str(
            selected_source_id
            or source_metadata.get("source_id")
            or ""
        )
        hier = get_hierarchy_entry_for_source(job, sid_hint) if sid_hint else None
        if hier:
            source_metadata = dict(source_metadata)
            if hier.get("publisher_name") or hier.get("publisher_id"):
                source_metadata["publisher"] = (
                    source_metadata.get("publisher")
                    or hier.get("publisher_name")
                    or hier.get("publisher_id")
                )
            extra = dict(source_metadata.get("extra_metadata") or {})
            extra["media_hierarchy"] = {
                "publisher_id": hier.get("publisher_id"),
                "publisher_name": hier.get("publisher_name"),
                "grain_level": hier.get("grain_level"),
                "grain_level_id": hier.get("grain_level_id"),
                "grain_level_name": hier.get("grain_level_name"),
                "grain_level_index": hier.get("grain_level_index"),
                "combined_fields": [
                    c for c in (hier.get("combined_fields") or [])
                    if isinstance(c, dict) and c.get("accepted") and not c.get("single_dimension")
                ],
            }
            source_metadata["extra_metadata"] = extra
    except Exception:
        pass
    registry_source_id = str(
        source_metadata.get("source_id")
        or selected_source_id
        or make_source_id(
            job.get("id", "job"),
            source_metadata.get("file_id"),
            selected_sheet,
            file_path=source_metadata.get("file_path"),
        )
    )
    block_id_filter: Optional[str] = None
    try:
        from sia.agent.multi_block_sheet import parse_block_virtual_source_id

        parsed = parse_block_virtual_source_id(str(selected_source_id or registry_source_id))
        if parsed:
            registry_source_id, block_id_filter = parsed[0], parsed[1]
            source_metadata = dict(source_metadata)
            source_metadata["source_id"] = str(selected_source_id or registry_source_id)
            source_metadata["parent_source_id"] = registry_source_id
            source_metadata["block_id"] = block_id_filter
    except Exception:
        pass
    source_id = str(selected_source_id or registry_source_id).strip() or registry_source_id
    mapping_records = [
        item
        for item in (job.get("mapping_registry") or job.get("pending_schema_mappings") or [])
        if isinstance(item, dict)
        and str(item.get("source_id") or "") == registry_source_id
        and (not block_id_filter or str(item.get("block_id") or "").strip() == block_id_filter)
    ]
    approved_mappings = normalize_mapping_records(
        mapping_records,
        source_id=registry_source_id,
    )
    derived_date = derive_date_context_from_mappings(approved_mappings)
    if str(derived_date.get("date_shape") or "").strip().lower() != "unknown":
        source_metadata["date_shape"] = derived_date.get("date_shape")
        source_metadata["range_start_column"] = derived_date.get("range_start_column") or ""
        source_metadata["range_end_column"] = derived_date.get("range_end_column") or ""
    if derived_date.get("date_granularity"):
        source_metadata["date_granularity"] = derived_date.get("date_granularity")

    approved_layout = _build_layout_context(job, source_id=source_id, selected_sheet=selected_sheet)
    sheet_for_scope = str(selected_sheet or source_metadata.get("sheet_name") or "").strip()

    from sia.context.artifacts import resolve_source_context_material
    from sia.integrity.context_isolation import (
        append_tab_inference_evidence,
        build_scoped_fields,
    )
    from sia.context.scoped_field import scoped_fields_to_dict

    def _build_fresh_context() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        snippets = _extract_context_block_snippets(source_metadata, approved_layout)
        interpreted = _interpret_context_block_snippets(source_metadata, snippets)
        return snippets, interpreted

    context_block_snippets, interpreted_context, context_fp, _from_cache = resolve_source_context_material(
        job,
        source_id=str(source_id),
        sheet_name=sheet_for_scope or None,
        source_metadata=source_metadata,
        approved_layout=approved_layout,
        build_fresh=_build_fresh_context,
    )
    scoped = build_scoped_fields(
        interpreted_context,
        context_block_snippets,
        sheet_for_scope,
        source_id=str(source_id),
    )
    if scoped:
        interpreted_context = dict(interpreted_context or {})
        interpreted_context = append_tab_inference_evidence(interpreted_context, scoped)
        from sia.context.scoped_field import scoped_fields_to_flat

        interpreted_context["fields"] = scoped_fields_to_flat(scoped)
        interpreted_context["scoped_fields"] = scoped_fields_to_dict(scoped)
    business_rule_records = [
        item for item in (job.get("business_rules_registry") or [])
        if not isinstance(item, dict)
        or not item.get("applies_to_source_id")
        or item.get("applies_to_source_id") == source_id
    ]
    business_rules = normalize_business_rules(business_rule_records, source_id=source_id)
    notes = _collect_user_notes(job, source_metadata)
    pt_cols = pre_transform_target_columns(active_template) if ok_template else []
    packet = ContextPacket(
        job_id=str(job.get("id", "")),
        source_metadata=source_metadata,
        available_sources=available_sources,
        available_source_summaries=_build_available_source_summaries(job, active_template, available_sources),
        approved_layout=approved_layout,
        context_block_snippets=context_block_snippets,
        interpreted_context=interpreted_context,
        approved_mappings=approved_mappings,
        business_rules=business_rules,
        target_template=active_template,
        pre_transform_target_columns=pt_cols,
        file_relationships=list(job.get("approved_file_relationships") or []),
        user_notes=notes,
        unresolved_items=_compute_unresolved_items(active_template, approved_mappings),
        lineage={
            "job_id": job.get("id"),
            "file_name": source_metadata.get("file_name") or job.get("filename"),
            "source_id": source_id,
            "sheet_name": selected_sheet or source_metadata.get("sheet_name"),
        },
        approval_state={
            "destructive_approved": bool(job.get("destructive_approved")),
            "approved_tool_indices": job.get("approved_tool_indices"),
        },
        defer_graph_file_relationship_review=bool(defer_file_relationship_graph_hitl),
        job_run_ledger_summary=build_job_run_ledger_summary(job),
        _schema_version=CONTEXT_PACKET_SCHEMA_VERSION,
    )
    out = packet.to_dict()
    out["_schema_version"] = CONTEXT_PACKET_SCHEMA_VERSION
    out["context_fingerprint"] = context_fp
    try:
        from sia.context.diff_log import log_scoped_fields_built

        log_scoped_fields_built(
            job,
            source_id=str(source_id),
            fields=(interpreted_context or {}).get("fields") or {},
            stage="packet_build",
        )
    except Exception:
        pass
    return out


_REVIEW_CONTEXT_OVERLAY_KEYS = (
    "resolved_planner_decisions",
    "planner_decision_notes",
    "duplicate_target_mappings",
)

# Never overlay these from a saved packet — always keep fresh per-source build.
_RESUME_SCOPE_FRESH_ONLY_KEYS = (
    "interpreted_context",
    "context_block_snippets",
    "source_metadata",
    "lineage",
    "context_fingerprint",
    "available_sources",
    "available_source_summaries",
    "approved_layout",
    "business_rules",
    "user_notes",
)

_REVIEW_MAPPING_OVERLAY_KEYS = (
    "decision",
    "target_column",
    "role",
    "target_match_confidence",
    "target_match_method",
    "date_semantic",
    "metric_role",
    "value_scale",
    "value_scale_note",
    "metric_currency",
)


def _mapping_row_key(row: Dict[str, Any]) -> str:
    src = str(row.get("source_column") or row.get("column_name") or "").strip().lower()
    block_id = str(row.get("block_id") or "").strip()
    return f"{block_id}|{src}" if block_id else src


def _merge_mapping_row_for_resume(fresh: Dict[str, Any], saved: Dict[str, Any]) -> Dict[str, Any]:
    """Keep fresh registry fields; overlay review-time mapping patches when they differ."""
    merged = dict(fresh)
    for key in _REVIEW_MAPPING_OVERLAY_KEYS:
        if key not in saved:
            continue
        if saved.get(key) != merged.get(key):
            merged[key] = saved[key]
    return merged


def merge_approved_mappings_for_resume(
    fresh_mappings: Optional[List[Dict[str, Any]]],
    saved_mappings: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Merge mapping rows for plan-review resume: job registry is base; review patches win per column.
    """
    fresh_rows = [dict(x) for x in (fresh_mappings or []) if isinstance(x, dict)]
    saved_rows = [dict(x) for x in (saved_mappings or []) if isinstance(x, dict)]
    if not saved_rows:
        return fresh_rows
    if not fresh_rows:
        return saved_rows

    saved_by_key = {_mapping_row_key(row): row for row in saved_rows}
    merged: List[Dict[str, Any]] = []
    for fresh_row in fresh_rows:
        key = _mapping_row_key(fresh_row)
        saved_row = saved_by_key.get(key)
        if saved_row:
            merged.append(_merge_mapping_row_for_resume(fresh_row, saved_row))
        else:
            merged.append(fresh_row)
    return merged


def merge_plan_review_context_packet(
    fresh_packet: Optional[Dict[str, Any]],
    saved_packet: Optional[Dict[str, Any]],
    *,
    job: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Hybrid resume context (option C): rebuild from job, then overlay planner-review decisions.

    Fresh packet supplies current layout, mappings, rules, and notes from the job.
    Saved packet supplies analyst decisions from the Review Plan screen.
    """
    out = dict(fresh_packet or {})
    saved = dict(saved_packet or {}) if saved_packet else {}
    if not saved and not job:
        return out

    for key in _REVIEW_CONTEXT_OVERLAY_KEYS:
        val = saved.get(key)
        if val:
            out[key] = val

    fresh_only = dict(fresh_packet or {})
    for key in _RESUME_SCOPE_FRESH_ONLY_KEYS:
        if key in fresh_only:
            out[key] = fresh_only[key]

    saved_sid = str(
        ((saved.get("lineage") or {}).get("source_id"))
        or ((saved.get("source_metadata") or {}).get("source_id"))
        or ""
    ).strip()
    fresh_sid = str(
        ((fresh_only.get("lineage") or {}).get("source_id"))
        or ((fresh_only.get("source_metadata") or {}).get("source_id"))
        or ""
    ).strip()
    if saved_sid and fresh_sid and saved_sid != fresh_sid:
        for key in _RESUME_SCOPE_FRESH_ONLY_KEYS:
            if key in fresh_only:
                out[key] = fresh_only[key]

    if not out.get("resolved_planner_decisions") and job:
        job_decisions = job.get("resolved_planner_decisions")
        if job_decisions:
            out["resolved_planner_decisions"] = list(job_decisions)

    if saved.get("approved_mappings"):
        out["approved_mappings"] = merge_approved_mappings_for_resume(
            out.get("approved_mappings"),
            saved.get("approved_mappings"),
        )
        if not out.get("duplicate_target_mappings"):
            try:
                from .planner_decisions import duplicate_target_sources

                dupes = duplicate_target_sources(out.get("approved_mappings") or [])
                if dupes:
                    out["duplicate_target_mappings"] = dupes
            except Exception:
                pass

    return out


def _extract_context_block_snippets(
    source_metadata: Dict[str, Any],
    approved_layout: Dict[str, Any],
    max_blocks: int = 5,
    max_rows: int = 4,
    max_cols: int = 6,
    max_cells: int = 18,
) -> List[Dict[str, Any]]:
    file_path = str(source_metadata.get("file_path") or "").strip()
    sheet_name = source_metadata.get("sheet_name")
    context_blocks = list((approved_layout or {}).get("context_blocks") or [])
    if not file_path or not context_blocks:
        return []

    try:
        from .scoped_source import load_raw_sheet_dataframe

        raw_df = load_raw_sheet_dataframe(file_path, sheet_name)
    except Exception:
        return []

    snippets: List[Dict[str, Any]] = []
    for block in context_blocks[:max_blocks]:
        if not isinstance(block, dict):
            continue
        coords = block.get("coordinates") if isinstance(block.get("coordinates"), dict) else block
        start_row = _safe_int_like(coords.get("start_row", coords.get("header_row", 0)), 0)
        end_row = _safe_int_like(coords.get("end_row", coords.get("data_end_row", start_row)), start_row)
        start_col = _safe_int_like(coords.get("start_col", coords.get("col_start", 0)), 0)
        end_col = _safe_int_like(coords.get("end_col", coords.get("col_end", start_col)), start_col)

        if raw_df.empty:
            continue
        start_row = max(0, min(start_row, len(raw_df) - 1))
        end_row = max(start_row, min(end_row, len(raw_df) - 1))
        start_col = max(0, min(start_col, len(raw_df.columns) - 1))
        end_col = max(start_col, min(end_col, len(raw_df.columns) - 1))

        snippet_df = raw_df.iloc[start_row:end_row + 1, start_col:end_col + 1].copy()
        if snippet_df.empty:
            continue

        sample_df = snippet_df.iloc[:max_rows, :max_cols].copy()
        sample_rows: List[List[Any]] = []
        non_empty_cells: List[Dict[str, Any]] = []
        text_preview: List[str] = []

        for rel_row_idx, row in enumerate(sample_df.itertuples(index=False, name=None)):
            row_values: List[Any] = []
            preview_parts: List[str] = []
            for rel_col_idx, value in enumerate(row):
                safe_value = _normalize_preview_value(value)
                row_values.append(safe_value)
                if _is_non_empty_preview_value(safe_value):
                    preview_parts.append(str(safe_value))
                    if len(non_empty_cells) < max_cells:
                        non_empty_cells.append({
                            "row": start_row + rel_row_idx,
                            "col": start_col + rel_col_idx,
                            "value": safe_value,
                        })
            sample_rows.append(row_values)
            if preview_parts and len(text_preview) < max_rows:
                text_preview.append(" | ".join(preview_parts))

        summary = text_preview[0] if text_preview else ""
        snippets.append({
            "block_id": block.get("block_id"),
            "block_label": block.get("block_label") or block.get("label") or block.get("block_id"),
            "block_category": block.get("block_category") or block.get("category") or "context",
            "coordinates": {
                "start_row": start_row,
                "end_row": end_row,
                "start_col": start_col,
                "end_col": end_col,
            },
            "shape": {
                "rows": int(end_row - start_row + 1),
                "cols": int(end_col - start_col + 1),
            },
            "sample_rows": sample_rows,
            "text_preview": text_preview,
            "non_empty_cells": non_empty_cells,
            "summary": summary,
        })
    return snippets


def _interpret_context_block_snippets(
    source_metadata: Dict[str, Any],
    snippets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    assumptions: List[str] = []
    evidence: List[Dict[str, Any]] = []
    sheet_name = str(source_metadata.get("sheet_name") or "").strip()
    source_id = str(source_metadata.get("source_id") or "").strip()

    for snippet in snippets or []:
        if not isinstance(snippet, dict):
            continue
        label = str(snippet.get("block_label") or snippet.get("block_id") or "context_block")
        block_id = str(snippet.get("block_id") or "").strip() or None
        lines = [str(item).strip() for item in (snippet.get("text_preview") or []) if str(item).strip()]
        for line in lines:
            norm_line = " ".join(line.split())
            line_lower = norm_line.lower()

            _maybe_capture_field(
                fields, evidence, label, norm_line, "publisher",
                _extract_key_value(norm_line, {"publisher", "publisher name", "platform", "site"}),
                source_id=source_id, sheet_name=sheet_name, block_id=block_id,
            )
            _maybe_capture_field(
                fields,
                evidence,
                label,
                norm_line,
                "channel",
                _extract_key_value(
                    norm_line,
                    {
                        "channel",
                        "channel context",
                        "channel_context",
                        "media channel",
                        "paid channel",
                        "media_channel",
                        "channel type",
                        "channel_type",
                    },
                ),
                source_id=source_id,
                sheet_name=sheet_name,
                block_id=block_id,
            )
            _maybe_capture_field(
                fields, evidence, label, norm_line, "market",
                _extract_key_value(norm_line, {"market", "region", "country", "geo"}),
                source_id=source_id, sheet_name=sheet_name, block_id=block_id,
            )
            _maybe_capture_field(
                fields, evidence, label, norm_line, "brand",
                _extract_key_value(norm_line, {"brand"}),
                source_id=source_id, sheet_name=sheet_name, block_id=block_id,
            )
            _maybe_capture_field(
                fields, evidence, label, norm_line, "campaign",
                _extract_key_value(norm_line, {"campaign", "campaign name"}),
                source_id=source_id, sheet_name=sheet_name, block_id=block_id,
            )
            _maybe_capture_field(
                fields, evidence, label, norm_line, "owner",
                _extract_key_value(norm_line, {"owner"}),
                source_id=source_id, sheet_name=sheet_name, block_id=block_id,
            )
            _maybe_capture_field(
                fields, evidence, label, norm_line, "variable_type",
                _extract_key_value(norm_line, {"variable type", "source type", "metric type"}),
                source_id=source_id, sheet_name=sheet_name, block_id=block_id,
            )
            _maybe_capture_field(
                fields, evidence, label, norm_line, "date_granularity",
                _extract_key_value(norm_line, {"date granularity", "granularity", "reporting grain", "time grain"}),
                source_id=source_id, sheet_name=sheet_name, block_id=block_id,
            )
            _maybe_capture_field(
                fields, evidence, label, norm_line, "aggregation_logic",
                _extract_key_value(norm_line, {"aggregation logic", "aggregation rule", "rollup logic"}),
                source_id=source_id, sheet_name=sheet_name, block_id=block_id,
            )

            period = _extract_modeling_period(norm_line)
            if period:
                start_date, end_date = period
                _maybe_capture_field(
                    fields, evidence, label, norm_line, "modeling_period_start", start_date,
                    source_id=source_id, sheet_name=sheet_name, block_id=block_id,
                )
                _maybe_capture_field(
                    fields, evidence, label, norm_line, "modeling_period_end", end_date,
                    source_id=source_id, sheet_name=sheet_name, block_id=block_id,
                )

            if any(token in line_lower for token in {"note", "notes", "assumption", "assumptions", "use ", "logic", "rule"}):
                if norm_line not in assumptions:
                    assumptions.append(norm_line)

    return {
        "fields": fields,
        "assumptions": assumptions[:10],
        "evidence": evidence[:12],
    }


def _maybe_capture_field(
    fields: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    block_label: str,
    line: str,
    field_name: str,
    value: Optional[str],
    *,
    source_id: str = "",
    sheet_name: str = "",
    block_id: Optional[str] = None,
) -> None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return
    if field_name in {"date_granularity"}:
        cleaned = _normalize_granularity(cleaned)
    if field_name not in fields:
        fields[field_name] = cleaned
        evidence.append({
            "field": field_name,
            "value": cleaned,
            "block_label": block_label,
            "block_id": block_id,
            "line": line,
            "scope": "block",
            "source_id": str(source_id or "").strip(),
            "sheet_name": str(sheet_name or "").strip(),
            "confidence": 1.0,
            "hop": 0,
        })


def _extract_key_value(line: str, keys: set[str]) -> Optional[str]:
    separators = ["|", ":", "-", "="]
    parts = [segment.strip() for segment in line.split("|") if segment.strip()]
    if len(parts) >= 2:
        key = parts[0].strip().lower()
        if key in keys:
            return parts[1].strip()

    for key in keys:
        key_lower = key.lower()
        if line.lower().startswith(key_lower):
            remainder = line[len(key):].strip()
            for sep in separators[1:]:
                if remainder.startswith(sep):
                    return remainder[len(sep):].strip()
            if remainder:
                return remainder
    return None


def _extract_modeling_period(line: str) -> Optional[Tuple[str, str]]:
    lower = line.lower()
    if "modeling period" not in lower and "modelling period" not in lower and "period" not in lower:
        return None

    dates = re.findall(r"\d{4}-\d{2}-\d{2}", line)
    if len(dates) >= 2:
        return dates[0], dates[1]
    return None


def _normalize_granularity(value: str) -> str:
    lowered = value.strip().lower()
    mapping = {
        "day": "daily",
        "daily": "daily",
        "week": "weekly",
        "weekly": "weekly",
        "month": "monthly",
        "monthly": "monthly",
        "quarter": "quarterly",
        "quarterly": "quarterly",
    }
    return mapping.get(lowered, value.strip())


def _safe_int_like(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_preview_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    text = str(value).strip()
    return text if text else None


def _is_non_empty_preview_value(value: Any) -> bool:
    return value not in (None, "")


def apply_context_to_dataframe(
    df: Optional[pd.DataFrame],
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
    business_rules: Optional[List[Dict[str, Any]]] = None,
    *,
    drop_excluded: bool = True,
) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """Apply mapping decisions: optional Exclude drop, Keep rename, then rules.

    Set ``drop_excluded=False`` after planner tools have already renamed columns.
    Schema Mapping Exclude runs on the clean sheet *before* rename; repeating it
    here would match template names (e.g. ``date``) and delete kept values.
    """
    if df is None:
        return df, []

    result = df.copy()
    applied_actions: List[str] = []

    if drop_excluded:
        result, discard_columns = drop_excluded_mapping_columns(result, approved_mappings)
        if discard_columns:
            applied_actions.append(f"dropped {len(discard_columns)} discarded columns")

    rename_map: Dict[str, str] = {}
    for mapping in approved_mappings or []:
        source_column = mapping.get("source_column")
        target_column = mapping.get("target_column")
        decision = str(mapping.get("decision", "")).strip().lower()
        if (
            source_column in result.columns
            and target_column
            and not is_no_match_target(target_column)
            and decision in KEEP_DECISIONS
            and target_column not in rename_map.values()
        ):
            rename_map[source_column] = target_column

    if rename_map:
        result = result.rename(columns=rename_map)
        applied_actions.append(f"applied {len(rename_map)} approved column mappings")

    for rule in sorted(business_rules or [], key=lambda item: item.get("priority", 100)):
        target_column = rule.get("target_column")
        if not target_column:
            continue
        if target_column not in result.columns:
            default_value = str(rule.get("rule_expression", ""))
            if rule.get("rule_type") == "default_value" and default_value:
                result[target_column] = default_value
                applied_actions.append(f"defaulted missing column {target_column}")
            continue

        if rule.get("rule_type") in {"fill_blank", "default_value"}:
            value = _coerce_rule_value(rule.get("rule_expression"))
            blank_mask = result[target_column].isna()
            if result[target_column].dtype == object:
                blank_mask = blank_mask | result[target_column].astype(str).str.strip().eq("")
            if blank_mask.any():
                result.loc[blank_mask, target_column] = value
                applied_actions.append(f"filled blanks in {target_column}")
        elif rule.get("rule_type") == "format":
            formatted = _format_series(result[target_column], str(rule.get("rule_expression", "")))
            if formatted is not None:
                result[target_column] = formatted
                applied_actions.append(f"formatted {target_column}")

    return result, applied_actions


def _select_source_metadata(
    available_sources: List[Dict[str, Any]],
    selected_sheet: Optional[str],
    selected_source_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not available_sources:
        return {}
    if selected_source_id:
        for source in available_sources:
            if source.get("source_id") == selected_source_id:
                return dict(source)
    if selected_sheet:
        for source in available_sources:
            if source.get("sheet_name") == selected_sheet:
                return dict(source)
    return dict(available_sources[0])


def _build_layout_context(job: Dict[str, Any], source_id: str, selected_sheet: Optional[str]) -> Dict[str, Any]:
    scoped_source = dict(((job.get("source_scope_registry") or {}).get(source_id)) or job.get("scoped_source") or {})
    layout_registry = list(job.get("layout_registry") or [])
    approved_blocks = [
        block for block in layout_registry
        if block.get("source_id") == source_id and (not selected_sheet or block.get("sheet_name") in {None, selected_sheet})
    ]
    main_blocks = []
    context_blocks = []
    for block in approved_blocks:
        if not isinstance(block, dict):
            continue
        role = _layout_registry_block_role(block)
        if role == "main":
            main_blocks.append(block)
        elif role == "context":
            context_blocks.append(block)
    if approved_blocks:
        return {
            "source_id": source_id,
            "header_row": scoped_source.get("header_row"),
            "scope_type": scoped_source.get("scope_type"),
            "analysis_bounds": scoped_source.get("analysis_bounds"),
            "blocks": main_blocks + context_blocks,
            "main_blocks": main_blocks,
            "context_blocks": context_blocks,
        }
    return {
        "source_id": source_id,
        "header_row": scoped_source.get("header_row"),
        "scope_type": scoped_source.get("scope_type"),
        "analysis_bounds": scoped_source.get("analysis_bounds"),
        "blocks": scoped_source.get("main_blocks", []),
        "main_blocks": scoped_source.get("main_blocks", []),
        "context_blocks": scoped_source.get("context_blocks", []),
    }


def _build_available_source_summaries(
    job: Dict[str, Any],
    target_template: Dict[str, Any],
    available_sources: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    mandatory_targets = set(mapping_targets_requiring_source(target_template)) if isinstance(target_template, dict) else set()
    mapping_registry = list(job.get("mapping_registry") or [])

    for source in available_sources:
        source_id = source.get("source_id")
        source_mappings = [
            item for item in mapping_registry
            if not isinstance(item, dict)
            or not item.get("source_id")
            or item.get("source_id") == source_id
        ]
        normalized_mappings = normalize_mapping_records(source_mappings, source_id=source_id or "source")
        mapped_targets = [
            item.get("target_column")
            for item in normalized_mappings
            if item.get("target_column") and item.get("target_column") != "No match"
        ]
        kept_columns = [
            item.get("source_column")
            for item in normalized_mappings
            if str(item.get("decision", "")).strip().lower() in KEEP_DECISIONS and item.get("source_column")
        ]
        # Registry rows must omit "falsey" defaults: bool(source.get("contains_main_data")) treats
        # a missing key as False, which wrongly drops sources from union / main-source logic in
        # propose_file_relationships (summaries always carry explicit keys).
        raw_main = source.get("contains_main_data")
        if raw_main is None:
            contains_main_data = True
        else:
            contains_main_data = bool(raw_main)
        raw_ref = source.get("contains_reference_data")
        if raw_ref is None:
            contains_reference_data = False
        else:
            contains_reference_data = bool(raw_ref)
        summaries.append({
            "source_id": source_id,
            "file_id": source.get("file_id"),
            "file_name": source.get("file_name"),
            "sheet_name": source.get("sheet_name"),
            "source_type": source.get("source_type"),
            "variable_type": source.get("variable_type") or source.get("source_type"),
            "uid": list(source.get("uid") or []),
            "date_granularity": source.get("date_granularity"),
            "modeling_period_start": source.get("modeling_period_start"),
            "modeling_period_end": source.get("modeling_period_end"),
            "contains_main_data": contains_main_data,
            "contains_reference_data": contains_reference_data,
            "publisher": source.get("publisher"),
            "media_hierarchy": (
                (source.get("extra_metadata") or {}).get("media_hierarchy")
                if isinstance(source.get("extra_metadata"), dict)
                else None
            ),
            "mapped_targets": mapped_targets,
            "template_grain_columns": template_union_grain_columns(target_template),
            "kept_source_columns": kept_columns,
            "unresolved_mandatory_targets": sorted(mandatory_targets.difference(mapped_targets)),
        })
    return summaries


def _collect_user_notes(job: Dict[str, Any], source_metadata: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    for item in source_metadata.get("user_notes", []) or []:
        if item and item not in notes:
            notes.append(str(item))
    for item in job.get("user_notes", []) or []:
        if item and item not in notes:
            notes.append(str(item))
    return notes


def _compute_unresolved_items(target_template: Dict[str, Any], approved_mappings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    mapped_targets = {
        mapping.get("target_column")
        for mapping in approved_mappings
        if mapping.get("target_column") and mapping.get("target_column") != "No match"
    }
    unresolved = []
    for target_column in sorted(mapping_targets_requiring_source(target_template)):
        if target_column not in mapped_targets:
            unresolved.append({
                "target_column": target_column,
                "status": "missing_mapping",
                "requirement": "requires_source",
            })
    return unresolved


def _format_series(series: pd.Series, expression: str) -> Optional[pd.Series]:
    if not expression:
        return None
    format_lookup = {
        "yyyy-mm-dd": "%Y-%m-%d",
        "yyyy/mm/dd": "%Y/%m/%d",
        "dd/mm/yyyy": "%d/%m/%Y",
        "mm/dd/yyyy": "%m/%d/%Y",
    }
    format_string = format_lookup.get(expression.lower(), expression)
    parsed = pd.to_datetime(series, errors="coerce")
    if parsed.notna().sum() == 0:
        return None
    formatted = parsed.dt.strftime(format_string)
    formatted = formatted.where(parsed.notna(), series)
    return formatted


def _coerce_rule_value(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value).strip()
    if text == "":
        return ""
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return value
