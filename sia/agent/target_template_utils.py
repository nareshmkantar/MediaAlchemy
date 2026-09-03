"""
Target template helpers — single contract: x_scope uid_hierarchy, metrics,
supporting_columns partition properties; business_logic.column_rules.
"""
from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


def _as_string_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        seq = [values]
    elif isinstance(values, Iterable):
        seq = values
    else:
        seq = [values]
    out: List[str] = []
    seen: Set[str] = set()
    for item in seq:
        if item is None:
            continue
        token = str(item).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def normalize_target_template(template: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize user-provided templates into the canonical runtime shape.

    Supported compat rules:
    - ``x_scope.uid`` -> ``x_scope.uid_hierarchy``
    - British spelling ``modelling_*`` -> canonical ``modeling_period``
    - nested ``x_scope.aggregation_logic`` -> top-level ``aggregation_logic``
    - infer ``metrics`` from ``aggregation_logic.metric_rules`` when omitted
    - infer ``supporting_columns`` from remaining property keys that are not
      part of ``uid_hierarchy`` and are not inferred metrics

    The uploaded user template remains authoritative; this function only fills
    structural gaps required by the runtime contract.
    """
    if not isinstance(template, dict) or not template:
        return {}

    normalized = deepcopy(template)
    props = normalized.get("properties")
    if not isinstance(props, dict):
        normalized["properties"] = {}
        props = normalized["properties"]

    raw_scope = normalized.get("x_scope")
    scope: Dict[str, Any] = dict(raw_scope) if isinstance(raw_scope, dict) else {}

    uid_hierarchy = _as_string_list(scope.get("uid_hierarchy") or scope.get("uid"))

    aggregation_logic = normalized.get("aggregation_logic")
    if not isinstance(aggregation_logic, dict):
        aggregation_logic = {}
    nested_aggregation = scope.get("aggregation_logic")
    if isinstance(nested_aggregation, dict):
        aggregation_logic = {**nested_aggregation, **aggregation_logic}
    normalized["aggregation_logic"] = aggregation_logic

    metrics = _as_string_list(scope.get("metrics"))
    if not metrics:
        metrics = _as_string_list((aggregation_logic.get("metric_rules") or {}).keys())

    supporting_columns = _as_string_list(scope.get("supporting_columns"))
    covered = set(uid_hierarchy) | set(metrics) | set(supporting_columns)
    inferred_supporting: List[str] = []
    for key in props.keys():
        name = str(key)
        if not name or name in covered:
            continue
        entry = props.get(key)
        req = ""
        if isinstance(entry, dict):
            req = str(entry.get("x_requirement") or "").strip().lower()
        if req in {"mandatory", "mandatory_for_synergy_models"}:
            continue
        inferred_supporting.append(name)
    supporting_columns = _as_string_list(supporting_columns + inferred_supporting)

    modeling_period = scope.get("modeling_period")
    if not isinstance(modeling_period, dict):
        modeling_period = {}
    british_modeling_period = scope.get("modelling_period")
    if isinstance(british_modeling_period, dict):
        modeling_period = {**british_modeling_period, **modeling_period}

    start_date = (
        modeling_period.get("start_date")
        or modeling_period.get("startDate")
        or scope.get("modeling_period_start")
        or scope.get("modeling_start_date")
        or scope.get("modelling_start_date")
    )
    end_date = (
        modeling_period.get("end_date")
        or modeling_period.get("endDate")
        or scope.get("modeling_period_end")
        or scope.get("modeling_end_date")
        or scope.get("modelling_end_date")
    )
    if start_date or end_date:
        scope["modeling_period"] = {
            "start_date": start_date or "",
            "end_date": end_date or "",
        }

    scope["uid_hierarchy"] = uid_hierarchy
    scope["metrics"] = metrics
    scope["supporting_columns"] = supporting_columns
    normalized["x_scope"] = scope
    return normalized


def validate_template_shape(template: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    Require properties and x_scope lists; union(uid_hierarchy, metrics, supporting_columns)
    must equal the set of property keys (each list entry must exist in properties).
    """
    template = normalize_target_template(template)
    if not isinstance(template, dict) or not template:
        return False, "template is empty"
    props = template.get("properties")
    if not isinstance(props, dict) or not props:
        return False, "properties must be a non-empty object"
    prop_keys = set(props.keys())
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    uh = list(scope.get("uid_hierarchy") or [])
    met = list(scope.get("metrics") or [])
    sup = list(scope.get("supporting_columns") or [])
    if not uh and not met and not sup:
        return False, "x_scope must define uid_hierarchy, metrics, and/or supporting_columns"
    ordered = pre_transform_target_columns(template)
    if set(ordered) != prop_keys:
        missing = prop_keys - set(ordered)
        extra = set(ordered) - prop_keys
        return False, f"x_scope lists must list exactly properties keys; missing={missing!s} extra={extra!s}"
    return True, ""


def pre_transform_target_columns(template: Dict[str, Any]) -> List[str]:
    """Ordered uid_hierarchy + metrics + supporting_columns (dedupe, first occurrence wins)."""
    template = normalize_target_template(template)
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    seq: List[str] = []
    seen: Set[str] = set()
    for block in (
        scope.get("uid_hierarchy") or [],
        scope.get("metrics") or [],
        scope.get("supporting_columns") or [],
    ):
        for x in block:
            if x is None:
                continue
            name = str(x)
            if name in seen:
                continue
            seen.add(name)
            seq.append(name)
    return seq


def _is_date_scope_key(key: str, prop_entry: Any) -> bool:
    """Treat as date dimension: name ``date`` or JSON Schema ``format: date`` on properties."""
    if str(key).strip().lower() == "date":
        return True
    if isinstance(prop_entry, dict) and str(prop_entry.get("format", "")).lower() == "date":
        return True
    return False


def pre_transform_column_order_for_ui(template: Dict[str, Any]) -> List[str]:
    """
    Pre-transform template field order for mapping UI and matrix columns:
    **date** (keys in uid_hierarchy that are date dimensions), then **remaining uid_hierarchy**,
    then **supporting_columns**, then **metrics**. Same keys as ``pre_transform_target_columns``,
    only ordering differs (business logic / post-transform still uses full template).
    """
    template = normalize_target_template(template)
    if not isinstance(template, dict):
        return []
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
    uh = [str(x) for x in (scope.get("uid_hierarchy") or []) if x is not None]
    sup = [str(x) for x in (scope.get("supporting_columns") or []) if x is not None]
    met = [str(x) for x in (scope.get("metrics") or []) if x is not None]

    date_keys: List[str] = []
    uid_rest: List[str] = []
    for k in uh:
        p = props.get(k)
        if _is_date_scope_key(k, p):
            date_keys.append(k)
        else:
            uid_rest.append(k)

    out: List[str] = []
    seen: Set[str] = set()
    for block in (date_keys, uid_rest, sup, met):
        for x in block:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
    return out


def collation_merge_column_key_order(template: Dict[str, Any]) -> List[str]:
    """Column order for multi-source collation ``reorder_columns`` (post-union).

    Order: **date** dimensions from ``uid_hierarchy`` first (in list order among dates),
    remaining **uid_hierarchy** entries in list order, **supporting_columns** in list order,
    then **metrics** in template list order (metrics last).
    """
    template = normalize_target_template(template)
    if not isinstance(template, dict) or not template:
        return []
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
    uh = [str(x) for x in (scope.get("uid_hierarchy") or []) if x is not None]
    met = [str(x) for x in (scope.get("metrics") or []) if x is not None]
    sup = [str(x) for x in (scope.get("supporting_columns") or []) if x is not None]

    date_keys: List[str] = []
    uid_rest: List[str] = []
    for k in uh:
        p = props.get(k)
        if _is_date_scope_key(k, p):
            date_keys.append(k)
        else:
            uid_rest.append(k)

    out: List[str] = []
    seen: Set[str] = set()
    for block in (date_keys, uid_rest, sup, met):
        for x in block:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
    return out


def final_output_column_order(template: Dict[str, Any]) -> List[str]:
    """Canonical final column order: date → uid → supporting → metrics."""
    return collation_merge_column_key_order(template)


def template_union_grain_columns(template: Optional[Dict[str, Any]]) -> List[str]:
    """UID + supporting dimension names for union join-key inference (excludes metrics)."""
    template = normalize_target_template(template or {})
    if not isinstance(template, dict) or not template:
        return []
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
    uh = [str(x) for x in (scope.get("uid_hierarchy") or []) if x is not None]
    sup = [str(x) for x in (scope.get("supporting_columns") or []) if x is not None]

    date_keys: List[str] = []
    uid_rest: List[str] = []
    for k in uh:
        p = props.get(k)
        if _is_date_scope_key(k, p):
            date_keys.append(k)
        else:
            uid_rest.append(k)

    out: List[str] = []
    seen: Set[str] = set()
    for block in (date_keys, uid_rest, sup):
        for x in block:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
    return out


def weekly_aggregate_group_by_columns(
    template: Optional[Dict[str, Any]],
    date_col: Optional[Any] = None,
    present_columns: Optional[List[Any]] = None,
) -> List[str]:
    """Dimension columns for ``transform.aggregate_weekly`` group_by (uid + supporting, not date/metrics)."""
    template = normalize_target_template(template)
    if not template:
        return []
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
    metrics = {str(m) for m in (scope.get("metrics") or []) if m is not None}
    uh = [str(x) for x in (scope.get("uid_hierarchy") or []) if x is not None]
    sup = [str(x) for x in (scope.get("supporting_columns") or []) if x is not None]

    date_names: Set[str] = set()
    if date_col:
        date_names.add(str(date_col).strip())
    for k in uh:
        if _is_date_scope_key(k, props.get(k)):
            date_names.add(k)

    candidates: List[str] = []
    seen: Set[str] = set()
    for k in uh + sup:
        if not k or k in metrics or k in date_names or k in seen:
            continue
        seen.add(k)
        candidates.append(k)

    if present_columns:
        have = [str(c) for c in present_columns if c is not None and str(c).strip()]
        lower = {str(c).lower(): c for c in have}
        resolved: List[str] = []
        seen_phys: Set[str] = set()
        for name in candidates:
            pick = name if name in have else lower.get(name.lower())
            if pick and pick not in seen_phys:
                seen_phys.add(pick)
                resolved.append(pick)
        return resolved
    return candidates


DATE_UID_COLUMN_ALIASES = frozenset(
    {
        "date",
        "calendar_date",
        "week_start",
        "week_start_date",
        "week",
        "period_date",
    }
)


def _rename_target_to_source_lookup(
    approved_mappings: Optional[List[Dict[str, Any]]],
) -> Dict[str, str]:
    """Lowercase template target name → physical source column (pre-rename)."""
    rename = build_rename_mapping_from_approved_mappings(approved_mappings or [])
    out: Dict[str, str] = {}
    for src, tgt in rename.items():
        key = str(tgt or "").strip().lower()
        if key and key not in out:
            out[key] = str(src).strip()
    return out


def resolve_column_names_against_present(
    present_columns: Optional[List[Any]],
    column_names: Optional[List[Any]],
    *,
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Map template / plan column names to names present on the frame (case + mapping aware)."""
    have = [str(c) for c in (present_columns or []) if c is not None and str(c).strip()]
    if not have:
        return []
    lower = {str(c).lower(): c for c in have}
    target_to_source = _rename_target_to_source_lookup(approved_mappings)
    resolved: List[str] = []
    seen: Set[str] = set()
    for raw in column_names or []:
        name = str(raw or "").strip()
        if not name:
            continue
        pick = name if name in have else lower.get(name.lower())
        if not pick:
            src = target_to_source.get(name.lower())
            if src:
                pick = src if src in have else lower.get(src.lower())
        if not pick and name.lower() in DATE_UID_COLUMN_ALIASES:
            pick = lower.get("date") or lower.get("calendar_date")
        if pick and pick not in seen:
            seen.add(pick)
            resolved.append(pick)
    return resolved


def final_output_sort_columns(
    template: Dict[str, Any],
    present_columns: Optional[List[Any]] = None,
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Row sort keys: date uid fields first, then remaining ``uid_hierarchy`` in template order."""
    template = normalize_target_template(template)
    if not template:
        return []
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
    uh = [str(x) for x in (scope.get("uid_hierarchy") or []) if x is not None]

    date_keys: List[str] = []
    uid_rest: List[str] = []
    for k in uh:
        p = props.get(k)
        if _is_date_scope_key(k, p):
            date_keys.append(k)
        else:
            uid_rest.append(k)
    preferred = date_keys + uid_rest

    if not present_columns:
        return preferred

    return resolve_column_names_against_present(
        present_columns,
        preferred,
        approved_mappings=approved_mappings,
    )


def _resolve_column_order_against_frame(
    df: pd.DataFrame,
    column_order: Optional[List[Any]],
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Map template column names to physical dataframe columns (case-insensitive)."""
    if df is None or df.empty:
        return []
    return resolve_template_column_names_against_frame(
        df,
        column_order,
        approved_mappings=approved_mappings,
    )


def resolve_template_column_names_against_frame(
    df: pd.DataFrame,
    column_names: Optional[List[Any]],
    *,
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Resolve template column keys to physical dataframe columns."""
    if df is None or df.empty:
        return []
    return resolve_column_names_against_present(
        list(df.columns),
        column_names,
        approved_mappings=approved_mappings,
    )


def reorder_dataframe_columns(
    df: Optional[pd.DataFrame],
    template: Optional[Dict[str, Any]],
    column_order: Optional[List[Any]] = None,
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
    extra_allowed: Optional[List[str]] = None,
) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """Reorder columns to template order; append unlisted columns at the end (stable)."""
    if df is None or df.empty:
        return df, []

    from sia.tools.collation_tools import reorder_columns

    preferred = list(column_order or [])
    if not preferred and isinstance(template, dict) and template:
        preferred = final_output_column_order(template)

    resolved = _resolve_column_order_against_frame(df, preferred)
    rest = [c for c in df.columns if c not in resolved]
    out = reorder_columns(df, resolved + rest)

    if list(out.columns) == list(df.columns):
        return out, []
    return out, [f"reordered columns to template order ({len(resolved)} template keys first)"]


def sort_dataframe_by_template(
    df: Optional[pd.DataFrame],
    template: Optional[Dict[str, Any]],
    sort_columns: Optional[List[Any]] = None,
    ascending: bool = True,
) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """Sort rows by uid hierarchy (date dimensions first)."""
    if df is None or df.empty:
        return df, []

    keys = list(sort_columns or [])
    if not keys and isinstance(template, dict) and template:
        keys = final_output_sort_columns(template, present_columns=list(df.columns))
    resolved = _resolve_column_order_against_frame(df, keys)
    if not resolved:
        return df, []

    out = df.copy()
    props = (
        (template or {}).get("properties")
        if isinstance(template, dict)
        else {}
    ) or {}
    if not isinstance(props, dict):
        props = {}

    for col in resolved:
        if col not in out.columns:
            continue
        prop = props.get(col)
        if _is_date_scope_key(col, prop):
            out[col] = pd.to_datetime(out[col], errors="coerce")

    out = out.sort_values(
        by=resolved,
        ascending=ascending,
        kind="mergesort",
        na_position="last",
    ).reset_index(drop=True)
    return out, [f"sorted rows by {', '.join(resolved)}"]


def mapping_target_columns_for_ui(template: Dict[str, Any]) -> List[str]:
    """
    Target columns exposed in the semantic mapping dropdown.

    This is intentionally narrower than the full template property set:
    only date fields from ``uid_hierarchy``, the remaining ``uid_hierarchy``
    entries, and ``metrics`` are valid mapping targets in the UI.
    ``supporting_columns`` stay in the template for downstream processing,
    but are not offered as direct mapping targets.
    """
    template = normalize_target_template(template)
    if not isinstance(template, dict):
        return []
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
    uh = [str(x) for x in (scope.get("uid_hierarchy") or []) if x is not None]
    met = [str(x) for x in (scope.get("metrics") or []) if x is not None]

    date_keys: List[str] = []
    uid_rest: List[str] = []
    for k in uh:
        p = props.get(k)
        if _is_date_scope_key(k, p):
            date_keys.append(k)
        else:
            uid_rest.append(k)

    out: List[str] = []
    seen: Set[str] = set()
    for block in (date_keys, uid_rest, met):
        for x in block:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
    return out


def primary_target_columns(template: Dict[str, Any]) -> Set[str]:
    """
    Primary target fields for semantic column mapping: the date key(s),
    ``uid_hierarchy`` entries, and template ``metrics``. A source column whose
    mapped ``target_column`` falls in this set is treated as a **primary**
    column in the mapping UI.
    """
    template = normalize_target_template(template)
    if not isinstance(template, dict):
        return set()
    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
    uh = [str(x) for x in (scope.get("uid_hierarchy") or []) if x is not None]
    metrics = [str(x) for x in (scope.get("metrics") or []) if x is not None]
    out: Set[str] = set()
    for k in uh:
        out.add(k)
        p = props.get(k)
        if _is_date_scope_key(k, p):
            out.add(k)
    for k in metrics:
        out.add(k)
    return out


def column_rule_mapping_exceptions(template: Dict[str, Any]) -> Set[str]:
    """
    Target columns that may be satisfied by template rules without a source mapping
    (e.g. set_value when column missing or null).
    """
    template = normalize_target_template(template)
    out: Set[str] = set()
    bl = template.get("business_logic") if isinstance(template.get("business_logic"), dict) else {}
    for rule in bl.get("column_rules") or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("operator") != "set_value":
            continue
        cond = rule.get("condition") if isinstance(rule.get("condition"), dict) else {}
        if cond.get("type") != "column_missing_or_null":
            continue
        col = cond.get("column")
        dest = rule.get("destination_column") or col
        if col and dest == col:
            out.add(str(dest))
    return out


def mapping_targets_requiring_source(template: Dict[str, Any]) -> Set[str]:
    """Pre-transform columns that still need an approved source mapping."""
    ok, err = validate_template_shape(template)
    if not ok:
        logger.warning("mapping_targets_requiring_source: invalid template: %s", err)
        return set()
    return set(pre_transform_target_columns(template)) - column_rule_mapping_exceptions(template)


def template_column_rules_summary(template: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    bl = template.get("business_logic") if isinstance(template.get("business_logic"), dict) else {}
    for rule in bl.get("column_rules") or []:
        if not isinstance(rule, dict):
            continue
        rid = rule.get("rule_id", "")
        desc = rule.get("description", "")
        op = rule.get("operator", "")
        if rid or desc or op:
            lines.append(f"{rid}: {op} — {desc}".strip(": —"))
    return lines[:30]


def _normalize_column_name(name: Any) -> str:
    return str(name or "").strip().lower()


def is_no_match_target(value: Any) -> bool:
    """True when mapping UI / planner marks a column as intentionally unmapped."""
    token = str(value or "").strip().lower().replace("_", " ")
    return token in {"no match", "nomatch", "no-match"}


def sanitize_aggregate_weekly_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Drop redundant ``week_start_col`` when weekly rollup reuses ``date_col``.

    The tool already buckets into the resolved date column when ``drop_original_date``
    is true and the plan only passes the legacy default ``week_start_col="week_start"``.
    Omitting the param keeps traces and plans aligned with the template date name (e.g. ``date``).
    """
    out = dict(params or {})
    date_col = str(out.get("date_col") or "").strip()
    drop = out.get("drop_original_date", True)
    if drop is None:
        drop = True
    ws = out.get("week_start_col")
    if ws is None:
        return out
    ws_str = str(ws).strip()
    if not ws_str:
        out.pop("week_start_col", None)
        return out
    if (
        bool(drop)
        and ws_str.lower() == "week_start"
        and date_col
        and date_col.lower() != "week_start"
    ):
        out.pop("week_start_col", None)
    return out


def sanitize_rename_mapping(mapping: Optional[Dict[Any, Any]]) -> Dict[str, str]:
    """
    Drop rename pairs whose target is ``No match`` — keep the physical source column name.
    """
    out: Dict[str, str] = {}
    for raw_source, raw_target in (mapping or {}).items():
        if is_no_match_target(raw_target) or is_no_match_target(raw_source):
            continue
        source = str(raw_source or "").strip()
        target = str(raw_target or "").strip()
        if not source or not target or source == target:
            continue
        out[source] = target
    return out


MAPPING_KEEP_DECISIONS = frozenset(
    {"keep", "approved", "primary", "supporting", "metadata", "context", "use as context"}
)


def build_rename_mapping_from_approved_mappings(
    approved_mappings: Optional[List[Dict[str, Any]]],
) -> Dict[str, str]:
    """Physical source → template / collation name (prefers Primary/Supporting ``output_alias``)."""
    mapping: Dict[str, str] = {}
    for item in approved_mappings or []:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision", "")).strip().lower()
        source_col = str(item.get("source_column") or item.get("column_name") or "").strip()
        if decision not in MAPPING_KEEP_DECISIONS or not source_col:
            continue
        alias = str(item.get("output_alias") or "").strip()
        target_col = str(item.get("target_column") or "").strip()
        dest = alias or target_col
        if not dest or is_no_match_target(dest) or source_col == dest:
            continue
        mapping.setdefault(source_col, dest)
    return sanitize_rename_mapping(mapping)


def _column_present(name: str, present_normalized: Set[str]) -> bool:
    return _normalize_column_name(name) in present_normalized


def build_template_contract(template: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Canonical template contract for planner state (no dataframe-specific gaps)."""
    template = normalize_target_template(template)
    if not template:
        return {}

    scope = template.get("x_scope") if isinstance(template.get("x_scope"), dict) else {}
    props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
    bl = template.get("business_logic") if isinstance(template.get("business_logic"), dict) else {}
    aggregation = template.get("aggregation_logic")
    if not isinstance(aggregation, dict):
        aggregation = scope.get("aggregation_logic") if isinstance(scope.get("aggregation_logic"), dict) else {}

    column_rules: List[Dict[str, Any]] = []
    for rule in bl.get("column_rules") or []:
        if not isinstance(rule, dict):
            continue
        cond = rule.get("condition") if isinstance(rule.get("condition"), dict) else {}
        col = cond.get("column")
        dest = rule.get("destination_column") or col
        column_rules.append(
            {
                "rule_id": rule.get("rule_id", ""),
                "operator": rule.get("operator", ""),
                "description": rule.get("description", ""),
                "destination_column": dest,
                "value": rule.get("value"),
                "condition_type": cond.get("type"),
                "condition_column": col,
            }
        )

    return {
        "title": template.get("title"),
        "variable_type": scope.get("variable_type"),
        "uid_hierarchy": _as_string_list(scope.get("uid_hierarchy")),
        "metrics": _as_string_list(scope.get("metrics")),
        "supporting_columns": _as_string_list(scope.get("supporting_columns")),
        "mandatory_properties": list(props.keys()),
        "pre_transform_column_order": pre_transform_target_columns(template),
        "column_rules": column_rules,
        "column_rule_summary": template_column_rules_summary(template),
        "rule_satisfied_without_mapping": sorted(column_rule_mapping_exceptions(template)),
        "aggregation_logic": aggregation if isinstance(aggregation, dict) else {},
        "date_granularity": str(scope.get("date_granularity") or "").strip(),
        "modeling_period": scope.get("modeling_period") if isinstance(scope.get("modeling_period"), dict) else {},
    }


def compute_column_gaps(
    template: Optional[Dict[str, Any]],
    present_columns: Optional[List[Any]],
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compare template contract columns to dataframe/mapping reality for planning."""
    template = normalize_target_template(template)
    present_list = [str(c) for c in (present_columns or []) if c is not None and str(c).strip()]
    present_norm: Set[str] = {_normalize_column_name(c) for c in present_list}

    if not template:
        return {
            "present_columns": present_list,
            "missing_uid": [],
            "missing_metrics": [],
            "missing_supporting": [],
            "suggested_add_columns": [],
            "rename_needed": [],
        }

    contract = build_template_contract(template)

    def _missing(names: List[str]) -> List[str]:
        return [n for n in names if not _column_present(n, present_norm)]

    missing_uid = _missing(contract.get("uid_hierarchy") or [])
    missing_metrics = _missing(contract.get("metrics") or [])
    missing_supporting = _missing(contract.get("supporting_columns") or [])

    suggested_add_columns: List[Dict[str, Any]] = []
    for rule in contract.get("column_rules") or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("operator") != "set_value":
            continue
        if rule.get("condition_type") != "column_missing_or_null":
            continue
        dest = rule.get("destination_column")
        if not dest:
            continue
        if _column_present(str(dest), present_norm):
            continue
        suggested_add_columns.append(
            {
                "rule_id": rule.get("rule_id", ""),
                "target_column": str(dest),
                "value": rule.get("value"),
                "when": "missing",
                "tool": "transform.add_column",
            }
        )

    rename_needed: List[Dict[str, str]] = []
    for mapping in approved_mappings or []:
        if not isinstance(mapping, dict):
            continue
        src = mapping.get("source_column")
        tgt = mapping.get("target_column")
        if not src or not tgt or str(src).strip() == str(tgt).strip():
            continue
        if is_no_match_target(tgt):
            continue
        if _column_present(str(tgt), present_norm):
            continue
        if _column_present(str(src), present_norm):
            rename_needed.append({"from": str(src), "to": str(tgt)})

    return {
        "present_columns": present_list,
        "missing_uid": missing_uid,
        "missing_metrics": missing_metrics,
        "missing_supporting": missing_supporting,
        "suggested_add_columns": suggested_add_columns,
        "rename_needed": rename_needed,
    }


def allowed_post_transform_columns(
    template: Optional[Dict[str, Any]],
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
    business_rules: Optional[List[Dict[str, Any]]] = None,
    extra_allowed: Optional[List[str]] = None,
) -> Set[str]:
    """Columns allowed to survive into the final dataframe.

    The allowlist is composed of:

    - Every ``properties`` key of the target template (final output columns).
    - Every ``target_column`` from approved mappings whose decision keeps the
      column (``keep``/``metadata``/``context``).
    - Every destination column created by approved business rules.
    - A small set of helper columns we produce during processing (week
      boundaries, calendar date) so weekly rollups can still expose them if
      the template allows.
    - Optional ``extra_allowed`` names passed by callers.
    """
    template = normalize_target_template(template)
    allowed: Set[str] = set()
    if isinstance(template, dict):
        props = template.get("properties") if isinstance(template.get("properties"), dict) else {}
        allowed.update(str(k) for k in props.keys())
        allowed.update(pre_transform_target_columns(template))

    for mapping in approved_mappings or []:
        if not isinstance(mapping, dict):
            continue
        decision = str(mapping.get("decision", "")).strip().lower()
        target = mapping.get("target_column")
        source_column = mapping.get("source_column")
        if decision not in MAPPING_KEEP_DECISIONS or not source_column:
            continue
        if target and not is_no_match_target(target):
            allowed.add(str(target))
        else:
            allowed.add(str(source_column))

    for rule in business_rules or []:
        if not isinstance(rule, dict):
            continue
        dest = rule.get("target_column") or rule.get("destination_column")
        if dest:
            allowed.add(str(dest))

    for name in extra_allowed or []:
        if name:
            allowed.add(str(name))

    return allowed


def prune_dataframe_to_template(
    df: Optional[pd.DataFrame],
    template: Optional[Dict[str, Any]],
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
    business_rules: Optional[List[Dict[str, Any]]] = None,
    extra_allowed: Optional[List[str]] = None,
) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """Drop any columns that are not in the template/mapping/rules allowlist.

    This is a deterministic safety net used after the planner tools and the
    context/template rules have run, so excluded / unmapped columns do not
    survive even when the planner forgets to emit an explicit
    ``transform.drop_columns`` call. Order follows ``pre_transform_target_columns``
    when possible, then any extra helper/business columns that were kept.
    """
    if df is None:
        return None, []
    if not isinstance(template, dict) or not template:
        return df, []

    allowed = allowed_post_transform_columns(
        template,
        approved_mappings=approved_mappings,
        business_rules=business_rules,
        extra_allowed=extra_allowed,
    )
    if not allowed:
        return df, []

    kept = [c for c in df.columns if c in allowed]
    dropped = [c for c in df.columns if c not in allowed]
    out = df.loc[:, kept].copy() if dropped else df.copy()

    out, _reorder_notes = reorder_dataframe_columns(
        out,
        template,
        approved_mappings=approved_mappings,
        extra_allowed=extra_allowed,
    )

    notes: List[str] = []
    if dropped:
        notes.append(
            f"pruned {len(dropped)} unmapped column(s): {', '.join(str(c) for c in dropped[:10])}"
            + ("..." if len(dropped) > 10 else "")
        )
    notes.extend(_reorder_notes)
    return out, notes


def apply_column_rules_list(
    df: Optional[pd.DataFrame],
    column_rules: Optional[List[Dict[str, Any]]],
) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """Apply template-style column_rules (set_value + column_missing_or_null only)."""
    if df is None:
        return None, []
    out = df.copy()
    actions: List[str] = []

    for rule in column_rules or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("operator") != "set_value":
            logger.warning("Unsupported template rule operator: %s", rule.get("operator"))
            continue
        cond = rule.get("condition") if isinstance(rule.get("condition"), dict) else {}
        if cond.get("type") != "column_missing_or_null":
            logger.warning("Unsupported template rule condition: %s", cond.get("type"))
            continue
        col = cond.get("column")
        dest = rule.get("destination_column") or col
        val = rule.get("value")
        rid = rule.get("rule_id", "")
        if not col or dest is None:
            continue

        target = str(dest)
        if target not in out.columns:
            out[target] = val
            actions.append(f"{rid}: created column {target!r} = {val!r}")
            continue

        ser = out[target]
        blank = ser.isna()
        if ser.dtype == object or str(ser.dtype) == "string":
            blank = blank | ser.astype(str).str.strip().eq("")
        if bool(blank.all()) or len(out) == 0:
            out[target] = val
            actions.append(f"{rid}: filled empty column {target!r} = {val!r}")
        elif bool(blank.any()):
            out.loc[blank, target] = val
            actions.append(f"{rid}: filled {int(blank.sum())} blank rows in {target!r}")

    return out, actions


def apply_template_column_rules(
    df: Optional[pd.DataFrame],
    template: Optional[Dict[str, Any]],
) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """Apply business_logic.column_rules (set_value + column_missing_or_null)."""
    if df is None:
        return None, []
    if not isinstance(template, dict):
        return df, []

    bl = template.get("business_logic") if isinstance(template.get("business_logic"), dict) else {}
    return apply_column_rules_list(df, bl.get("column_rules") or [])
