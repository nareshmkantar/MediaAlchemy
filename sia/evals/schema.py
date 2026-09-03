"""Shared types for pipeline evaluation results."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Violation types that block export/collation (hard gate).
CRITICAL_VIOLATION_TYPES = frozenset(
    {
        "zero_rows",
        "mass_row_loss",
        "metric_column_wiped",
        "aggregate_sum_mismatch",
        "dimension_mismatch",
        "metric_mismatch",
        "memory_scope",
        "context_grounding_critical",
        "context_lineage_missing",
        "union_false_duplicate",
        "context_hop_exceeded",
        "grain_tool_executed_pre_collation",
    }
)

CRITICAL_INTEGRITY_SUBTYPES = frozenset(
    {
        "zero_rows",
        "mass_row_loss",
        "metric_column_wiped",
        "aggregate_sum_mismatch",
    }
)


def empty_stage_result(*, pass_default: bool = True) -> Dict[str, Any]:
    return {"pass": pass_default, "metrics": {}, "violations": []}


def violation(
    *,
    vtype: str,
    message: str,
    severity: str = "advisory",
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sev = severity
    if vtype in CRITICAL_VIOLATION_TYPES:
        sev = "critical"
    return {
        "type": vtype,
        "severity": sev,
        "message": message,
        "evidence": dict(evidence or {}),
    }


def stage_passes(violations: List[Dict[str, Any]]) -> bool:
    return not any(
        isinstance(v, dict) and str(v.get("severity") or "").lower() == "critical"
        for v in violations
    )


def empty_pipeline_evals() -> Dict[str, Any]:
    return {
        "critical_gate": {"pass": True, "blocked_reasons": []},
        "per_source": {},
        "collation": {},
        "judge": {},
        "quality": {},
        "overrides": [],
    }


def ensure_per_source_bucket(pipeline: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    per = dict(pipeline.get("per_source") or {})
    sid = str(source_id or "").strip() or "__unknown__"
    bucket = dict(per.get(sid) or {})
    per[sid] = bucket
    pipeline["per_source"] = per
    return bucket
