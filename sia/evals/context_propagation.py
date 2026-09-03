"""Evals for context propagation hop limits (Phase 8.2)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sia.context.confidence import MAX_CONTEXT_HOPS
from sia.evals.schema import empty_stage_result, stage_passes, violation


def evaluate_context_hop_events(
    job: Optional[Dict[str, Any]],
    *,
    source_id: str = "",
    max_hops: int = MAX_CONTEXT_HOPS,
) -> Dict[str, Any]:
    """Scan diff log for hop-limit rejections and excessive hop counts."""
    out = empty_stage_result()
    violations: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {"max_hop_seen": 0, "hop_rejections": 0}

    if not isinstance(job, dict):
        out["metrics"] = metrics
        return out

    sid = str(source_id or "").strip()
    for row in job.get("context_diff_log") or []:
        if not isinstance(row, dict):
            continue
        if sid and str(row.get("source_id") or "") not in ("", sid):
            continue
        hop = int(row.get("hop") or 0)
        metrics["max_hop_seen"] = max(metrics["max_hop_seen"], hop)
        reason = str(row.get("reason") or "")
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        rejected = bool(meta.get("rejected")) or "exceeds" in reason.lower()
        if rejected and ("hop" in reason.lower() or hop > max_hops):
            metrics["hop_rejections"] = int(metrics["hop_rejections"]) + 1
            violations.append(
                violation(
                    vtype="context_hop_exceeded",
                    message=(
                        f"Context hop {hop} for field {row.get('field')!r} rejected "
                        f"(limit {max_hops}): {reason or 'propagation blocked'}"
                    ),
                    severity="critical",
                    evidence=dict(row),
                )
            )
        elif hop > max_hops:
            violations.append(
                violation(
                    vtype="context_hop_exceeded",
                    message=f"Context field {row.get('field')!r} recorded hop {hop} > limit {max_hops}",
                    severity="critical",
                    evidence=dict(row),
                )
            )

    out["metrics"] = metrics
    out["violations"] = violations
    out["pass"] = stage_passes(violations)
    return out
