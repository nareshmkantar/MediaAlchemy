"""Deterministic context verification — scope, provenance, and isolation gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import pandas as pd

from sia.evals.plan_contract import evaluate_plan_contract
from sia.integrity.context_isolation import (
    check_context_packet_lineage,
    check_output_matches_local_context,
    check_output_metrics_match_clean_template,
    context_packet_source_id,
    local_context_fields,
    local_scoped_fields,
    plan_source_id,
)

PlanLike = Union[Dict[str, Any], Any, None]


@dataclass
class ContextVerificationResult:
    """Result of a deterministic context gate (not LLM OutputVerifier)."""

    pass_: bool = True
    violations: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    stage: str = ""

    def critical_violations(self) -> List[Dict[str, Any]]:
        return [
            v
            for v in self.violations
            if str(v.get("severity") or "").lower() == "critical"
            or v.get("type")
            in (
                "context_grounding_critical",
                "dimension_mismatch",
                "metric_mismatch",
                "memory_scope",
                "context_lineage_missing",
            )
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pass": self.pass_,
            "violations": list(self.violations),
            "metrics": dict(self.metrics),
            "stage": self.stage,
        }

    def to_assess_dict(
        self,
        *,
        context_packet: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Shape expected by pipeline evals / ``assess_source_context_isolation``."""
        cp = context_packet or {}
        local = local_context_fields(cp)
        scoped = local_scoped_fields(cp)
        sid = str(
            source_id
            or self.metrics.get("source_id")
            or context_packet_source_id(cp)
            or ""
        )
        return {
            "pass": self.pass_,
            "source_id": sid,
            "local_context": local,
            "scoped_local_context": {k: v.to_dict() for k, v in scoped.items()},
            "violations": list(self.violations),
            "metrics": dict(self.metrics),
        }


def _plan_tool_calls(
    plan: PlanLike,
    tool_calls: Optional[Sequence[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if tool_calls is not None:
        return [dict(t) for t in tool_calls if isinstance(t, dict)]
    if isinstance(plan, dict):
        return [dict(t) for t in (plan.get("tool_calls") or []) if isinstance(t, dict)]
    if plan is not None:
        raw = getattr(plan, "tool_calls", None) or []
        return [dict(t) for t in raw if isinstance(t, dict)]
    return []


class ContextVerifier:
    """
    Pure deterministic checks for source-scoped context.

    **Not** ``OutputVerifier`` (``sia/agent/verifier.py``): that module validates
    flat-table shape, schema, and template rules via LLM-assisted checks.
    ``ContextVerifier`` only enforces scope, provenance, and cross-sheet isolation.
    """

    @staticmethod
    def verify_plan_context(
        plan: PlanLike,
        context_packet: Optional[Dict[str, Any]],
        *,
        job: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[Sequence[Dict[str, Any]]] = None,
        resume_state: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
    ) -> ContextVerificationResult:
        """Pre-execute gate: plan literals and source binding vs active packet."""
        cp = context_packet or {}
        tools = _plan_tool_calls(plan, tool_calls)
        contract = evaluate_plan_contract(
            tools,
            context_packet=cp,
            target_template=target_template or cp.get("target_template"),
            plan_source_id=plan_source_id(plan),
            resume_state=resume_state,
        )
        violations = list(contract.get("violations") or [])
        metrics = dict(contract.get("metrics") or {})

        active_sid = context_packet_source_id(cp) or plan_source_id(plan)
        bound_sid = plan_source_id(plan)
        if active_sid and bound_sid and active_sid != bound_sid:
            violations.append(
                {
                    "type": "memory_scope",
                    "severity": "critical",
                    "message": (
                        f"plan.source_id {bound_sid!r} does not match active source {active_sid!r}"
                    ),
                    "evidence": {"plan_source_id": bound_sid, "active_source_id": active_sid},
                }
            )

        for row in check_context_packet_lineage(cp):
            violations.append({**row, "severity": "critical"})

        critical_types = {
            "context_grounding_critical",
            "memory_scope",
            "context_lineage_missing",
        }
        pass_ = not any(
            str(v.get("severity") or "").lower() == "critical"
            or str(v.get("type") or "") in critical_types
            for v in violations
        )

        if job is not None and not pass_:
            from sia.context.diff_log import log_value_change

            sid = str(active_sid or "").strip()
            for v in violations:
                if str(v.get("severity") or "").lower() != "critical" and v.get("type") not in critical_types:
                    continue
                ev = v.get("evidence") if isinstance(v.get("evidence"), dict) else {}
                log_value_change(
                    job,
                    stage="pre_execute_gate",
                    source_id=sid,
                    field=str(ev.get("target_column") or v.get("type") or "context"),
                    before=ev.get("literal") or ev.get("expected"),
                    after=ev.get("expected") or v.get("message"),
                    reason=str(v.get("message") or ""),
                )

        return ContextVerificationResult(
            pass_=pass_,
            violations=violations,
            metrics=metrics,
            stage="plan",
        )

    @staticmethod
    def verify_output_frame(
        df: Optional[pd.DataFrame],
        context_packet: Optional[Dict[str, Any]],
        *,
        job: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
    ) -> ContextVerificationResult:
        """Post-finalize gate: output dimensions and metrics vs local context."""
        cp = context_packet or {}
        violations: List[Dict[str, Any]] = []
        for row in check_context_packet_lineage(context_packet):
            violations.append({**row, "severity": "critical"})
        for row in check_output_matches_local_context(df, context_packet):
            violations.append({**row, "severity": "critical"})
        sid = str(source_id or context_packet_source_id(cp) or "").strip()
        if job is not None and sid:
            for row in check_output_metrics_match_clean_template(df, job, sid):
                violations.append({**row, "severity": "critical"})

        from sia.context.stamp_policy import evaluate_low_confidence_context

        violations.extend(evaluate_low_confidence_context(cp, job))

        if job is not None and violations:
            from sia.context.diff_log import log_value_change

            for v in violations:
                vtype = str(v.get("type") or "")
                if vtype not in (
                    "dimension_mismatch",
                    "metric_mismatch",
                    "context_lineage_missing",
                ):
                    continue
                log_value_change(
                    job,
                    stage="finalize_assess",
                    source_id=sid,
                    field=str(v.get("column") or v.get("type") or "violation"),
                    before=v.get("expected"),
                    after=v.get("actual") or v.get("message"),
                    reason=str(v.get("message") or vtype),
                )

        local = local_context_fields(cp)
        scoped = local_scoped_fields(cp)
        critical_types = {
            "dimension_mismatch",
            "metric_mismatch",
            "context_lineage_missing",
        }
        pass_ = not any(
            str(v.get("severity") or "").lower() == "critical"
            or str(v.get("type") or "") in critical_types
            for v in violations
        )
        return ContextVerificationResult(
            pass_=pass_,
            violations=violations,
            metrics={
                "source_id": sid,
                "local_context": local,
                "scoped_local_context": {k: v.to_dict() for k, v in scoped.items()},
            },
            stage="output",
        )

    @staticmethod
    def assess_output_frame(
        df: Optional[pd.DataFrame],
        context_packet: Optional[Dict[str, Any]],
        *,
        job: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Backward-compatible dict for pipeline evals (wraps ``verify_output_frame``)."""
        result = ContextVerifier.verify_output_frame(
            df,
            context_packet,
            job=job,
            source_id=source_id,
        )
        return result.to_assess_dict(context_packet=context_packet, source_id=source_id)


def verify_plan_context(
    plan: PlanLike,
    context_packet: Optional[Dict[str, Any]],
    **kwargs: Any,
) -> ContextVerificationResult:
    """Module-level alias for ``ContextVerifier.verify_plan_context``."""
    return ContextVerifier.verify_plan_context(plan, context_packet, **kwargs)
