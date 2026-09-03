"""Deterministic checks on what actually ran in execute_tools vs plan deferral rules."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sia.agent.post_collate_transforms import POST_COLLATE_DEFERRED_GRAIN_TOOLS
from sia.evals.schema import empty_stage_result, stage_passes, violation
from sia.tools.tool_validator import normalize_tool_name


def _canonical_tool_names(tool_calls: Optional[Sequence[Dict[str, Any]]]) -> List[str]:
    names: List[str] = []
    for row in tool_calls or []:
        if not isinstance(row, dict):
            continue
        canonical, _ = normalize_tool_name(str(row.get("tool") or "").strip())
        if canonical:
            names.append(canonical)
    return names


def _stable_params_fingerprint(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    cleaned = {str(k): v for k, v in params.items() if not str(k).startswith("_")}
    try:
        return json.dumps(cleaned, sort_keys=True, default=str)
    except TypeError:
        return json.dumps({str(k): str(v) for k, v in cleaned.items()}, sort_keys=True)


def normalize_invocation(
    tool_call: Any,
    *,
    index: int = 0,
    source_id: str = "",
) -> Optional[Dict[str, Any]]:
    """One executed or planned tool call with canonical name and stable params fingerprint."""
    if isinstance(tool_call, dict):
        raw_tool = tool_call.get("tool")
        params = tool_call.get("params") if isinstance(tool_call.get("params"), dict) else {}
    else:
        raw_tool = getattr(tool_call, "tool", None)
        params = getattr(tool_call, "params", None)
        params = params if isinstance(params, dict) else {}
    canonical, _ = normalize_tool_name(str(raw_tool or "").strip())
    if not canonical:
        return None
    return {
        "index": int(index),
        "source_id": str(source_id or "").strip(),
        "tool": canonical,
        "params_fingerprint": _stable_params_fingerprint(params),
        "params": dict(params),
    }


def invocations_from_tool_calls(
    tool_calls: Optional[Sequence[Any]],
    *,
    source_id: str = "",
    start_index: int = 0,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, row in enumerate(tool_calls or []):
        inv = normalize_invocation(row, index=start_index + i, source_id=source_id)
        if inv:
            out.append(inv)
    return out


def evaluate_invocation_adherence(
    *,
    planned_tools: Optional[Sequence[Any]] = None,
    executed_tools: Optional[Sequence[Any]] = None,
    deferred_tools: Optional[Sequence[Any]] = None,
    source_id: str = "",
) -> Dict[str, Any]:
    """Ordered invocation diff: (tool, params) sequence planned vs actually executed."""
    planned = invocations_from_tool_calls(planned_tools, source_id=source_id)
    executed = invocations_from_tool_calls(executed_tools, source_id=source_id)
    deferred = invocations_from_tool_calls(deferred_tools, source_id=source_id)

    deferred_keys: Set[Tuple[str, str]] = {
        (d["tool"], d["params_fingerprint"]) for d in deferred
    }
    deferred_grain_tools = {d["tool"] for d in deferred if d["tool"] in POST_COLLATE_DEFERRED_GRAIN_TOOLS}

    mismatches: List[Dict[str, Any]] = []
    shared = min(len(planned), len(executed))
    for i in range(shared):
        p, e = planned[i], executed[i]
        if p["tool"] != e["tool"] or p["params_fingerprint"] != e["params_fingerprint"]:
            mismatches.append(
                {
                    "index": i,
                    "planned_tool": p["tool"],
                    "executed_tool": e["tool"],
                    "planned_params": p["params_fingerprint"],
                    "executed_params": e["params_fingerprint"],
                }
            )

    extra = executed[shared:]
    missing_raw = planned[shared:]
    missing: List[Dict[str, Any]] = []
    for inv in missing_raw:
        key = (inv["tool"], inv["params_fingerprint"])
        if key in deferred_keys:
            continue
        if inv["tool"] in deferred_grain_tools and not executed:
            continue
        missing.append(inv)

    denom = max(1, len(planned), len(executed))
    drift = len(mismatches) + len(extra) + len(missing)
    score = round(max(0.0, 1.0 - drift / denom), 3)

    return {
        "planned_invocation_count": len(planned),
        "executed_invocation_count": len(executed),
        "invocation_mismatch_count": len(mismatches),
        "invocation_extra_count": len(extra),
        "invocation_missing_count": len(missing),
        "invocation_adherence_score": score,
        "planned_invocations": planned[:24],
        "executed_invocations": executed[:24],
        "invocation_mismatches": mismatches[:12],
        "invocation_extra": extra[:12],
        "invocation_missing": missing[:12],
    }


def evaluate_execution_deferral(
    *,
    defer_grain: bool,
    planned_tools: Optional[Sequence[Dict[str, Any]]] = None,
    executed_tools: Optional[Sequence[Dict[str, Any]]] = None,
    deferred_tools: Optional[Sequence[Dict[str, Any]]] = None,
    source_id: str = "",
) -> Dict[str, Any]:
    """
    Verify grain-changing tools were deferred (not executed per-source) in multi-source batches.

    When ``executed_tools`` carries per-invocation params (from ``tools_history``), also scores
    ordered plan adherence at invocation granularity.
    """
    out = empty_stage_result()
    violations: List[Dict[str, Any]] = []
    planned = _canonical_tool_names(planned_tools)
    executed = _canonical_tool_names(executed_tools)
    deferred = _canonical_tool_names(deferred_tools)

    grain_planned = [n for n in planned if n in POST_COLLATE_DEFERRED_GRAIN_TOOLS]
    grain_executed = [n for n in executed if n in POST_COLLATE_DEFERRED_GRAIN_TOOLS]
    grain_deferred = [n for n in deferred if n in POST_COLLATE_DEFERRED_GRAIN_TOOLS]

    invocation_metrics = evaluate_invocation_adherence(
        planned_tools=planned_tools,
        executed_tools=executed_tools,
        deferred_tools=deferred_tools,
        source_id=source_id,
    )

    metrics: Dict[str, Any] = {
        "defer_grain": bool(defer_grain),
        "grain_tools_planned": grain_planned,
        "grain_tools_executed": grain_executed,
        "grain_tools_deferred": grain_deferred,
        "executed_tool_count": len(executed),
        "deferred_tool_count": len(deferred),
        **invocation_metrics,
    }

    planned_set = set(planned)
    accounted = set(executed) | set(deferred)
    unplanned = [n for n in executed if n not in planned_set] if planned else []
    missing = [n for n in planned if n not in accounted] if planned else []
    metrics["planned_tool_count"] = len(planned)
    metrics["unplanned_executed_tools"] = sorted(set(unplanned))
    metrics["missing_planned_tools"] = sorted(set(missing))

    if planned:
        denom = max(1, len(planned_set))
        drift = len(set(unplanned)) + len(set(missing))
        metrics["plan_adherence_score_set"] = round(max(0.0, 1.0 - drift / denom), 3)
        metrics["plan_adherence_score"] = invocation_metrics["invocation_adherence_score"]
        if unplanned:
            violations.append(
                violation(
                    vtype="plan_adherence_drift",
                    message=(
                        f"Execution ran tool(s) not in the plan: {', '.join(sorted(set(unplanned))[:6])}"
                    ),
                    severity="advisory",
                    evidence={"unplanned": sorted(set(unplanned))},
                )
            )
        if missing:
            violations.append(
                violation(
                    vtype="plan_adherence_drift",
                    message=(
                        f"Planned tool(s) never executed or deferred: {', '.join(sorted(set(missing))[:6])}"
                    ),
                    severity="advisory",
                    evidence={"missing": sorted(set(missing))},
                )
            )
        if (
            invocation_metrics["invocation_mismatch_count"]
            or invocation_metrics["invocation_extra_count"]
            or invocation_metrics["invocation_missing_count"]
        ):
            violations.append(
                violation(
                    vtype="invocation_adherence_drift",
                    message=(
                        f"Execution order/params drift: {invocation_metrics['invocation_mismatch_count']} "
                        f"mismatch(es), {invocation_metrics['invocation_extra_count']} extra invocation(s), "
                        f"{invocation_metrics['invocation_missing_count']} missing invocation(s)"
                    ),
                    severity="advisory",
                    evidence={
                        "mismatches": invocation_metrics.get("invocation_mismatches") or [],
                        "extra": [e.get("tool") for e in invocation_metrics.get("invocation_extra") or []],
                    },
                )
            )

    if defer_grain and grain_executed:
        violations.append(
            violation(
                vtype="grain_tool_executed_pre_collation",
                message=(
                    f"Grain tool(s) ran per-source before collation: {', '.join(grain_executed)}. "
                    "These should be deferred until after duplicate_check."
                ),
                severity="critical",
                evidence={"executed": grain_executed, "deferred": grain_deferred},
            )
        )

    if defer_grain and grain_planned and not grain_deferred and not grain_executed:
        violations.append(
            violation(
                vtype="grain_tool_missing_deferral",
                message=(
                    f"Plan includes grain tool(s) {', '.join(grain_planned)} but none were deferred "
                    "or executed — deferral partition may have failed."
                ),
                severity="advisory",
            )
        )

    if defer_grain and grain_planned and grain_deferred:
        metrics["deferral_ok"] = True
    elif not defer_grain:
        metrics["deferral_ok"] = True
    else:
        metrics["deferral_ok"] = not grain_executed

    out["metrics"] = metrics
    out["violations"] = violations
    out["pass"] = stage_passes(violations)
    return out
