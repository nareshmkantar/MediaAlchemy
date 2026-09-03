"""Template min/max constraint review (HITL), parallel to duplicate/integrity pauses."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

CONSTRAINT_ISSUE_TYPES = frozenset({"MIN_VIOLATION", "MAX_VIOLATION"})
CONSTRAINT_ACTIONS = frozenset({"keep_as_is", "clip_to_bound", "drop_rows"})


def constraint_issues_from_dataframe(
    df: Optional[pd.DataFrame],
    template: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Cheap, read-only min/max scan once columns have template names/types.

    This deliberately does not duplicate full ``verify.schema`` behavior: missing
    columns, enums, null rates, dates, and general type checks remain final-schema
    concerns. The runtime gate only fails fast on numeric bounds before expensive
    date expansion or aggregation.
    """
    if df is None or df.empty or not isinstance(template, dict):
        return []
    properties = template.get("properties")
    if not isinstance(properties, dict):
        return []

    lower_columns = {str(col).strip().lower(): col for col in df.columns}
    issues: List[Dict[str, Any]] = []
    for target_column, raw_spec in properties.items():
        if not isinstance(raw_spec, dict):
            continue
        if "minimum" not in raw_spec and "maximum" not in raw_spec:
            continue

        target_name = str(target_column).strip()
        resolved = (
            target_column
            if target_column in df.columns
            else lower_columns.get(target_name.lower())
        )
        if resolved is None:
            continue
        numeric = pd.to_numeric(df[resolved], errors="coerce")

        for bound_key, issue_type, relation in (
            ("minimum", "MIN_VIOLATION", "below"),
            ("maximum", "MAX_VIOLATION", "above"),
        ):
            if bound_key not in raw_spec:
                continue
            bound = raw_spec[bound_key]
            try:
                mask = (
                    numeric.notna() & (numeric < bound)
                    if bound_key == "minimum"
                    else numeric.notna() & (numeric > bound)
                )
            except TypeError:
                # A malformed non-numeric bound belongs in template validation,
                # not in this lightweight source-data gate.
                continue
            count = int(mask.sum())
            if count == 0:
                continue
            samples = [
                float(value)
                for value in numeric[mask].head(5).tolist()
                if pd.notna(value)
            ]
            issues.append(
                {
                    "column": target_name,
                    "resolved_column": resolved,
                    "severity": "MEDIUM",
                    "issue": issue_type,
                    "bound": bound,
                    bound_key: bound,
                    "count": count,
                    "violation_count": count,
                    "sample_values": samples,
                    "check_phase": "post_column_typing",
                    "detail": f"{count} values {relation} {bound_key} ({bound})",
                }
            )
    return issues


def constraint_issues_from_report(report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return MIN/MAX issue dicts from a verify.schema validation_report."""
    if not isinstance(report, dict):
        return []
    issues = report.get("issues") or []
    out: List[Dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("issue") or "") in CONSTRAINT_ISSUE_TYPES:
            out.append(dict(issue))
    return out


def parse_validation_report(
    message: Any = None,
    changes_made: Any = None,
) -> Optional[Dict[str, Any]]:
    """Pull validation_report from tool changes_made or JSON message body."""
    if isinstance(changes_made, dict):
        report = changes_made.get("validation_report")
        if isinstance(report, dict):
            return report
    if isinstance(message, dict) and "issues" in message:
        return message
    text = str(message or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def enrich_constraint_issue(
    df: pd.DataFrame,
    issue: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach sample violating values for Review UI."""
    enriched = dict(issue)
    col = str(issue.get("column") or "").strip()
    if not col or df is None or df.empty:
        return enriched

    lower_map = {str(c).strip().lower(): c for c in df.columns}
    resolved = col if col in df.columns else lower_map.get(col.lower())
    if not resolved:
        return enriched

    numeric = pd.to_numeric(df[resolved], errors="coerce")
    mask = pd.Series(False, index=df.index)
    bound = None
    kind = str(issue.get("issue") or "")
    if kind == "MIN_VIOLATION" and issue.get("bound") is not None:
        bound = issue["bound"]
        mask = numeric.notna() & (numeric < bound)
    elif kind == "MAX_VIOLATION" and issue.get("bound") is not None:
        bound = issue["bound"]
        mask = numeric.notna() & (numeric > bound)
    elif kind == "MIN_VIOLATION" and "minimum" in str(issue.get("detail") or "").lower():
        # Fallback if bound missing: try to read from detail is fragile; skip samples.
        pass

    if bound is None and kind == "MIN_VIOLATION" and issue.get("minimum") is not None:
        bound = issue["minimum"]
        mask = numeric.notna() & (numeric < bound)
    if bound is None and kind == "MAX_VIOLATION" and issue.get("maximum") is not None:
        bound = issue["maximum"]
        mask = numeric.notna() & (numeric > bound)

    violators = numeric[mask]
    samples = [float(v) if pd.notna(v) else None for v in violators.head(5).tolist()]
    enriched["resolved_column"] = resolved
    enriched["sample_values"] = samples
    enriched["violation_count"] = int(mask.sum())
    if bound is not None:
        enriched["bound"] = bound
    return enriched


def build_schema_constraint_checkpoint(
    violations: List[Dict[str, Any]],
    state: Dict[str, Any],
    *,
    tool_index: Optional[int] = None,
    tool_name: str = "verify.schema",
) -> Dict[str, Any]:
    """Checkpoint payload for Review queue when template min/max is breached."""
    cols = [str(v.get("column") or "?") for v in violations]
    col_preview = ", ".join(cols[:4]) + ("…" if len(cols) > 4 else "")
    total = sum(int(v.get("violation_count") or v.get("count") or 0) for v in violations)
    description = (
        f"Template constraints failed for {len(violations)} column(s)"
        + (f" ({total} value(s))" if total else "")
        + f": {col_preview}. Choose keep, clip to bound, or drop violating rows."
    )
    trigger = {
        "constraint_violations": violations,
        "tool": tool_name,
        "subtype": "schema_constraint",
    }
    if tool_index is not None:
        trigger["tool_index"] = int(tool_index)

    return {
        "checkpoint_id": f"schema_constraint_{uuid.uuid4().hex[:12]}",
        "checkpoint_type": "schema_mismatch",
        "title": "Template constraint review",
        "description": description,
        "severity": "high",
        "trigger_reason": description,
        "trigger_data": trigger,
        "available_actions": ["approve", "cancel"],
        "recommended_action": "approve",
        "current_step": str(state.get("current_step") or "execute_tools"),
        "iteration": int(state.get("iteration") or 0),
        "confidence": 0.4,
        "created_at": datetime.now().isoformat(),
        "resolved": False,
    }


def schema_constraint_to_hitl_state(
    violations: List[Dict[str, Any]],
    iteration_data: Optional[pd.DataFrame],
    state: Dict[str, Any],
    *,
    tools_history_slice: List[Dict[str, Any]],
    deferred_post_collate: List[Dict[str, Any]],
    warnings: List[str],
    low_confidence_items: List[Dict[str, Any]],
    tool_index: Optional[int] = None,
    tool_name: str = "verify.schema",
) -> Dict[str, Any]:
    """Build execute_tools early-return payload for schema constraint HITL pause."""
    enriched = [
        enrich_constraint_issue(iteration_data, v)
        if iteration_data is not None
        else dict(v)
        for v in violations
    ]
    checkpoint = build_schema_constraint_checkpoint(
        enriched, state, tool_index=tool_index, tool_name=tool_name
    )
    detail = checkpoint["description"]
    prior_checkpoints = [
        cp
        for cp in (state.get("hitl_checkpoints") or [])
        if isinstance(cp, dict) and not cp.get("resolved", False)
    ]
    low = list(low_confidence_items) + [
        {
            "type": "schema_constraint",
            "tool": tool_name,
            "item": v.get("column"),
            "confidence": 0.4,
            "details": v.get("detail") or detail,
        }
        for v in enriched
    ]
    return {
        "current_df": iteration_data,
        "current_frame": {
            "rows": len(iteration_data) if iteration_data is not None else 0,
            "cols": len(iteration_data.columns) if iteration_data is not None else 0,
        },
        "iteration": state["iteration"],
        "hitl_pending_approval": True,
        "hitl_pause_type": "schema_constraint_review",
        "schema_constraint_violations": enriched,
        "integrity_resume_after_tool_index": tool_index,
        "integrity_pause_tool": tool_name,
        "hitl_checkpoints": prior_checkpoints + [checkpoint],
        "low_confidence_items": low,
        "requires_review": True,
        "review_reason": detail,
        "warnings": warnings,
        "message": detail,
        "tools_history": tools_history_slice,
        "deferred_post_collate_tools": deferred_post_collate,
        "trace_steps": [
            {
                "step": "hitl_pause",
                "reason": "schema_constraint_review",
                "tool": tool_name,
                "columns": [v.get("column") for v in enriched],
            }
        ],
    }


def _resolve_column(df: pd.DataFrame, name: str) -> Optional[str]:
    if not name or df is None:
        return None
    if name in df.columns:
        return name
    lower_map = {str(c).strip().lower(): str(c) for c in df.columns}
    return lower_map.get(str(name).strip().lower())


def apply_constraint_resolutions(
    df: pd.DataFrame,
    decisions: Dict[str, Any],
    violations: List[Dict[str, Any]],
) -> Tuple[pd.DataFrame, List[str]]:
    """Apply per-column keep / clip / drop decisions. Returns (df, notes)."""
    if df is None or df.empty or not isinstance(decisions, dict):
        return df, []

    work = df.copy()
    notes: List[str] = []
    drop_mask = pd.Series(False, index=work.index)

    by_col = {str(v.get("column") or "").strip().lower(): v for v in violations if isinstance(v, dict)}

    for raw_col, action in decisions.items():
        col_key = str(raw_col or "").strip()
        act = str(action or "keep_as_is").strip().lower()
        if act not in CONSTRAINT_ACTIONS:
            act = "keep_as_is"
        if act == "keep_as_is":
            notes.append(f"{col_key}: kept values as-is (constraint acknowledged)")
            continue

        meta = by_col.get(col_key.lower()) or {}
        resolved = _resolve_column(work, str(meta.get("resolved_column") or col_key))
        if not resolved:
            notes.append(f"{col_key}: column not found; skipped")
            continue

        numeric = pd.to_numeric(work[resolved], errors="coerce")
        issue = str(meta.get("issue") or "")
        bound = meta.get("bound")
        if bound is None:
            bound = meta.get("minimum") if issue == "MIN_VIOLATION" else meta.get("maximum")
        if bound is None:
            notes.append(f"{col_key}: missing bound; skipped")
            continue
        bound_f = float(bound)

        if issue == "MAX_VIOLATION":
            viol = numeric.notna() & (numeric > bound_f)
        else:
            viol = numeric.notna() & (numeric < bound_f)

        n = int(viol.sum())
        if n == 0:
            notes.append(f"{col_key}: no violating rows")
            continue

        if act == "clip_to_bound":
            work.loc[viol, resolved] = bound_f
            notes.append(f"{col_key}: clipped {n} value(s) to {bound_f}")
        elif act == "drop_rows":
            drop_mask = drop_mask | viol
            notes.append(f"{col_key}: marked {n} row(s) for drop")

    if bool(drop_mask.any()):
        before = len(work)
        work = work.loc[~drop_mask].copy()
        notes.append(f"Dropped {before - len(work)} row(s) for constraint violations")

    return work, notes
