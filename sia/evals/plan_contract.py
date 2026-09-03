"""Deterministic plan quality checks (tool contract, grounding, completeness)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sia.evals.schema import empty_stage_result, stage_passes, violation
from sia.agent.post_collate_transforms import POST_COLLATE_DEFERRED_GRAIN_TOOLS
from sia.integrity.context_isolation import (
    SOURCE_LOCAL_DIMENSION_COLUMNS,
    context_packet_source_id,
    local_context_fields,
    plan_bound_to_wrong_source,
)
from sia.tools.pipeline_catalog import TOOL_PRIMARY_STAGE, stage_for_tool, stage_sort_key
from sia.tools.tool_validator import normalize_tool_name


def _tool_names(tool_calls: Sequence[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for row in tool_calls or []:
        if not isinstance(row, dict):
            continue
        canonical, _ = normalize_tool_name(str(row.get("tool") or ""))
        if canonical:
            names.append(canonical)
    return names


def _norm_col(value: Any) -> str:
    return str(value or "").strip().casefold()


def _column_universe(
    structure_analysis: Optional[Dict[str, Any]],
    context_packet: Optional[Dict[str, Any]],
) -> set:
    """Casefolded set of column labels known for this source.

    Combines structure-analyzer column labels with ``kept_source_columns`` from
    the scoped context packet. Returns empty set when nothing reliable is known
    (callers must skip argument checks in that case to avoid false positives).
    """
    universe: set = set()
    sa = structure_analysis if isinstance(structure_analysis, dict) else {}

    def _add_cols(cols: Any) -> None:
        for col in cols or []:
            if isinstance(col, dict):
                label = col.get("column_label") or col.get("label") or col.get("name")
            else:
                label = col
            n = _norm_col(label)
            if n:
                universe.add(n)

    for key in ("column_analysis", "columns"):
        _add_cols(sa.get(key))
    for tbl in sa.get("tables") or []:
        if isinstance(tbl, dict):
            _add_cols(tbl.get("columns") or tbl.get("column_analysis"))

    cp = context_packet if isinstance(context_packet, dict) else {}
    scoped = cp.get("scoped_source") if isinstance(cp.get("scoped_source"), dict) else {}
    _add_cols(cp.get("kept_source_columns") or scoped.get("kept_source_columns"))
    return universe


# Tool -> param keys that reference source column names (checked for existence).
_COLUMN_REF_PARAMS: Dict[str, Sequence[str]] = {
    "transform.rename": ("mapping", "columns"),
    "transform.map_values": ("column",),
    "transform.aggregate_weekly": ("group_by",),
    "transform.unpivot": ("id_vars", "value_vars", "id_cols"),
    "transform.split_column": ("column",),
    "transform.type_cast": ("column", "columns"),
    "transform.scale_values": ("column", "columns"),
}


def _referenced_columns(canonical: str, params: Dict[str, Any]) -> List[str]:
    """Extract the source-column names a tool's params reference."""
    refs: List[str] = []
    for key in _COLUMN_REF_PARAMS.get(canonical, ()):  # type: ignore[arg-type]
        val = params.get(key)
        if val is None:
            continue
        if canonical == "transform.rename" and isinstance(val, dict):
            # rename mapping keys are existing (source) columns
            refs.extend(str(k) for k in val.keys())
        elif isinstance(val, dict):
            refs.extend(str(k) for k in val.keys())
        elif isinstance(val, (list, tuple)):
            refs.extend(str(v) for v in val)
        else:
            refs.append(str(val))
    return [r for r in refs if str(r).strip()]


def evaluate_argument_correctness(
    tool_calls: Optional[Sequence[Dict[str, Any]]],
    *,
    structure_analysis: Optional[Dict[str, Any]] = None,
    context_packet: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Advisory check: do tool arguments reference columns that actually exist.

    Complements the runtime schema validator (which only enforces *presence* of
    required params) by catching hallucinated / mismatched column names in
    high-risk reshaping and mapping tools. Skips entirely when no reliable
    column universe is known, so it never fires false positives on sparse jobs.
    """
    out = empty_stage_result(pass_default=True)
    metrics: Dict[str, Any] = {}
    violations: List[Dict[str, Any]] = []

    universe = _column_universe(structure_analysis, context_packet)
    tools = [dict(t) for t in (tool_calls or []) if isinstance(t, dict)]
    checked = 0
    bad_refs: List[Dict[str, Any]] = []

    if universe:
        for t in tools:
            canonical, _ = normalize_tool_name(str(t.get("tool") or ""))
            if canonical not in _COLUMN_REF_PARAMS:
                continue
            params = t.get("params") if isinstance(t.get("params"), dict) else {}
            refs = _referenced_columns(canonical, params)
            for ref in refs:
                checked += 1
                if _norm_col(ref) not in universe:
                    bad_refs.append({"tool": canonical, "column": ref})

    metrics["argument_refs_checked"] = checked
    metrics["argument_bad_ref_count"] = len(bad_refs)
    if checked:
        metrics["argument_correctness_score"] = round(
            max(0.0, (checked - len(bad_refs)) / checked), 3
        )
    for bad in bad_refs[:8]:
        violations.append(
            violation(
                vtype="argument_correctness",
                message=(
                    f"{bad['tool']} references column {bad['column']!r} not found in the "
                    "source's known columns"
                ),
                severity="advisory",
                evidence=bad,
            )
        )

    out["metrics"] = metrics
    out["violations"] = violations
    out["pass"] = True  # advisory only — never blocks export
    return out


def evaluate_plan_contract(
    tool_calls: Optional[Sequence[Dict[str, Any]]],
    *,
    context_packet: Optional[Dict[str, Any]] = None,
    target_template: Optional[Dict[str, Any]] = None,
    structure_analysis: Optional[Dict[str, Any]] = None,
    plan_confidence: float = 0.0,
    approval_items: Optional[Sequence[Dict[str, Any]]] = None,
    plan_source_id: str = "",
    resume_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return stage result dict with metrics and violations."""
    out = empty_stage_result()
    violations: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {}
    tools = [dict(t) for t in (tool_calls or []) if isinstance(t, dict)]
    names = _tool_names(tools)
    metrics["tool_count"] = len(names)
    metrics["unique_tool_count"] = len(set(names))

    cp = context_packet or {}
    tpl = target_template if isinstance(target_template, dict) else cp.get("target_template")
    has_template = isinstance(tpl, dict) and bool(tpl)

    # Unknown tools
    unknown = [n for n in names if stage_for_tool(n) is None and not n.startswith("collation.")]
    if unknown:
        violations.append(
            violation(
                vtype="unknown_tool",
                message=f"Plan references unknown tools: {', '.join(unknown[:6])}",
                severity="advisory",
            )
        )

    # Stage ordering
    stage_keys: List[int] = []
    for n in names:
        st = stage_for_tool(n)
        if st is not None:
            stage_keys.append(stage_sort_key(st))
    order_violations = 0
    for i in range(1, len(stage_keys)):
        if stage_keys[i] < stage_keys[i - 1]:
            order_violations += 1
    metrics["stage_order_violations"] = order_violations
    if order_violations:
        violations.append(
            violation(
                vtype="tool_contract_sequence",
                message=f"Tool sequence has {order_violations} stage-order regression(s) vs pipeline catalog",
                severity="advisory",
            )
        )

    # verify.schema last when template exists
    if has_template and names:
        schema_idxs = [i for i, n in enumerate(names) if n == "verify.schema"]
        if not schema_idxs:
            violations.append(
                violation(
                    vtype="required_tools_missing",
                    message="Target template job missing terminal verify.schema",
                    severity="advisory",
                )
            )
        elif schema_idxs[-1] != len(names) - 1:
            violations.append(
                violation(
                    vtype="tool_contract_sequence",
                    message="verify.schema should be the last tool when a target template exists",
                    severity="advisory",
                )
            )

    # Duplicate verify.schema
    if names.count("verify.schema") > 1:
        violations.append(
            violation(
                vtype="unnecessary_tools",
                message="Plan repeats verify.schema more than once",
                severity="advisory",
            )
        )

    # column_gaps → add_column
    gaps = cp.get("column_gaps") if isinstance(cp.get("column_gaps"), dict) else {}
    suggested = list(gaps.get("suggested_add_columns") or []) if isinstance(gaps, dict) else []
    add_targets = {
        str((t.get("params") or {}).get("target_column") or "").strip().lower()
        for t in tools
        if str(t.get("tool") or "").endswith("add_column")
    }
    for gap in suggested:
        if not isinstance(gap, dict):
            continue
        dest = str(gap.get("target_column") or "").strip().lower()
        if dest and dest not in add_targets:
            violations.append(
                violation(
                    vtype="required_tools_missing",
                    message=f"Template gap missing add_column for {gap.get('target_column')}",
                    severity="advisory",
                    evidence={"target_column": gap.get("target_column")},
                )
            )

    # Context grounding for add_column literals (cross-source bleed)
    local = local_context_fields(cp)
    bleed_count = 0
    for tool in tools:
        if str(tool.get("tool") or "").strip() != "transform.add_column":
            continue
        params = tool.get("params") or {}
        target = str(params.get("target_column") or params.get("column") or "").strip().lower()
        if target not in SOURCE_LOCAL_DIMENSION_COLUMNS or target not in local:
            continue
        literal = str(params.get("value") or "").strip()
        expected = str(local.get(target) or "").strip()
        if literal and expected and literal.lower() != expected.lower():
            bleed_count += 1
            violations.append(
                violation(
                    vtype="context_grounding_critical",
                    message=(
                        f"add_column {target}={literal!r} disagrees with source-local "
                        f"context {expected!r}"
                    ),
                    severity="critical",
                    evidence={"target_column": target, "literal": literal, "expected": expected},
                )
            )
    if bleed_count:
        metrics["context_grounding_bleed_count"] = bleed_count

    active_sid = context_packet_source_id(cp) or str(plan_source_id or "").strip()
    if active_sid:
        metrics["source_id"] = active_sid
    if resume_state and plan_bound_to_wrong_source(dict(resume_state), cp):
        violations.append(
            violation(
                vtype="memory_scope",
                message="Persisted plan or context packet targets a different source_id than active sheet",
                severity="critical",
                evidence={"active_source_id": active_sid},
            )
        )

    # Planning summary completeness
    ps = cp.get("planning_summary") if isinstance(cp.get("planning_summary"), dict) else {}
    for key in ("layout_summary", "mapping_summary"):
        if has_template and not ps.get(key):
            violations.append(
                violation(
                    vtype="context_completeness",
                    message=f"planning_summary missing {key}",
                    severity="advisory",
                )
            )

    # Confidence vs approval_items noise
    items = list(approval_items or [])
    conf = float(plan_confidence or 0.0)
    metrics["plan_confidence"] = conf
    metrics["approval_items_count"] = len(items)
    if conf >= 0.85 and len(items) > 2:
        violations.append(
            violation(
                vtype="plan_confidence_sanity",
                message=f"High plan confidence ({conf:.0%}) but {len(items)} approval items (prompt expects sparse items)",
                severity="advisory",
            )
        )

    # Structure / layout alignment (light)
    sa = structure_analysis if isinstance(structure_analysis, dict) else {}
    tables = list(sa.get("tables") or [])
    layout = cp.get("approved_layout") if isinstance(cp.get("approved_layout"), dict) else {}
    main_blocks = list(layout.get("main_blocks") or layout.get("blocks") or [])
    metrics["structure_table_count"] = len(tables)
    metrics["layout_main_block_count"] = len(main_blocks)
    if tables and main_blocks and abs(len(tables) - len(main_blocks)) > 2:
        violations.append(
            violation(
                vtype="structure_plan_alignment",
                message=(
                    f"structure_analysis tables ({len(tables)}) diverges from "
                    f"approved main blocks ({len(main_blocks)})"
                ),
                severity="advisory",
            )
        )

    metrics["tool_contract_score"] = max(
        0.0,
        1.0
        - 0.15 * order_violations
        - 0.1 * len([v for v in violations if v.get("type") == "required_tools_missing"]),
    )
    defer_union = bool(cp.get("defer_weekly_rollups_to_post_union"))
    if resume_state and isinstance(resume_state, dict):
        defer_union = defer_union or bool(
            resume_state.get("multi_source_active_batch") or resume_state.get("multi_block_active_batch")
        )
    grain_in_plan = [n for n in names if n in POST_COLLATE_DEFERRED_GRAIN_TOOLS]
    if defer_union and grain_in_plan:
        metrics["deferred_grain_tools_in_plan"] = grain_in_plan
        metrics["grain_tools_run_post_collation"] = True
    out["metrics"] = metrics
    out["violations"] = violations
    out["pass"] = stage_passes(violations)
    return out
