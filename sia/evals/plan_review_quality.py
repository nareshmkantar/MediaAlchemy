"""HITL plan-review question precision / recall heuristics."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sia.evals.schema import empty_stage_result, violation


def _has_block_sparse_signal(
    context_packet: Optional[Dict[str, Any]],
    structure_analysis: Optional[Dict[str, Any]],
) -> bool:
    cp = context_packet or {}
    sa = structure_analysis or {}
    scoped = cp.get("scoped_source") if isinstance(cp.get("scoped_source"), dict) else {}
    merged = list(scoped.get("merged_metric_ranges") or [])
    if merged:
        return True
    vp = sa.get("visual_patterns") if isinstance(sa.get("visual_patterns"), dict) else {}
    mls = vp.get("metric_layout_signals") if isinstance(vp.get("metric_layout_signals"), dict) else {}
    if mls.get("grouped_rows_likely"):
        return True
    if mls.get("block_sparse_metrics"):
        return True
    return False


def evaluate_plan_review_quality(
    approval_items: Optional[Sequence[Dict[str, Any]]],
    *,
    plan_confidence: float = 0.0,
    context_packet: Optional[Dict[str, Any]] = None,
    structure_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out = empty_stage_result(pass_default=True)
    items = [dict(x) for x in (approval_items or []) if isinstance(x, dict)]
    metrics: Dict[str, Any] = {
        "items": len(items),
        "plan_confidence": float(plan_confidence or 0.0),
    }
    violations: List[Dict[str, Any]] = []
    misses: List[str] = []
    noise: List[str] = []

    block_signal = _has_block_sparse_signal(context_packet, structure_analysis)
    has_block_alloc_item = any(
        str(it.get("decision_type") or "").strip() == "block_metric_allocation"
        or "block-level" in str(it.get("question") or "").lower()
        for it in items
    )
    if block_signal and not has_block_alloc_item:
        misses.append("block_metric_allocation")
        violations.append(
            violation(
                vtype="review_miss",
                message="Grouped/block-sparse layout detected but no block metric allocation question",
                severity="advisory",
            )
        )

    low_conf = float(plan_confidence or 0.0) < 0.7
    if low_conf and not items:
        misses.append("low_confidence_plan")
        violations.append(
            violation(
                vtype="review_miss",
                message=f"Plan confidence {plan_confidence:.0%} < 70% but no approval items",
                severity="advisory",
            )
        )

    necessary = 0
    for it in items:
        dtype = str(it.get("decision_type") or "").strip()
        q = str(it.get("question") or "").lower()
        needed = (
            dtype == "block_metric_allocation"
            or "block-level" in q
            or low_conf
            or block_signal
        )
        if needed:
            necessary += 1
        elif float(plan_confidence or 0.0) >= 0.85:
            noise.append(dtype or q[:48] or "item")
            violations.append(
                violation(
                    vtype="review_noise",
                    message=f"Approval item may be unnecessary at confidence {plan_confidence:.0%}",
                    severity="advisory",
                    evidence={"question": it.get("question")},
                )
            )

    metrics["necessity_score"] = (necessary / len(items)) if items else 1.0
    metrics["misses"] = misses
    metrics["noise"] = noise
    out["metrics"] = metrics
    out["violations"] = violations
    out["pass"] = True  # advisory only
    return out
