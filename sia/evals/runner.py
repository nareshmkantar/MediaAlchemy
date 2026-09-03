"""Orchestrate pipeline eval capture, rollup, and critical gate."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sia.evals.collation_sanity import (
    evaluate_cross_source_isolation,
    evaluate_union_false_duplicates,
)
from sia.evals.plan_contract import evaluate_plan_contract
from sia.evals.plan_review_quality import evaluate_plan_review_quality
from sia.evals.schema import (
    CRITICAL_INTEGRITY_SUBTYPES,
    CRITICAL_VIOLATION_TYPES,
    empty_pipeline_evals,
    ensure_per_source_bucket,
    violation,
)
from sia.evals.structure_consistency import evaluate_structure_consistency
from sia.integrity.context_isolation import assess_source_context_isolation


class PipelineEvalRunner:
    """Mutates job.pipeline_evals in place."""

    @staticmethod
    def ensure(job: Dict[str, Any]) -> Dict[str, Any]:
        pe = job.get("pipeline_evals")
        if not isinstance(pe, dict):
            pe = empty_pipeline_evals()
            job["pipeline_evals"] = pe
        for key in ("critical_gate", "per_source", "collation", "judge", "quality", "overrides"):
            if key not in pe:
                pe[key] = empty_pipeline_evals()[key]
        return pe

    @staticmethod
    def record_structure(
        job: Dict[str, Any],
        source_id: str,
        *,
        structure_analysis: Optional[Dict[str, Any]] = None,
        context_packet: Optional[Dict[str, Any]] = None,
        metric_layout_signals: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pe = PipelineEvalRunner.ensure(job)
        result = evaluate_structure_consistency(
            structure_analysis,
            context_packet=context_packet,
            metric_layout_signals=metric_layout_signals,
        )
        bucket = ensure_per_source_bucket(pe, source_id)
        bucket["structure"] = result
        PipelineEvalRunner.recompute_critical_gate(job)
        return result

    @staticmethod
    def record_plan(
        job: Dict[str, Any],
        source_id: str,
        *,
        raw_tool_calls: Optional[List[Dict[str, Any]]] = None,
        finalized_tool_calls: Optional[List[Dict[str, Any]]] = None,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
        structure_analysis: Optional[Dict[str, Any]] = None,
        plan_confidence: float = 0.0,
        approval_items: Optional[List[Dict[str, Any]]] = None,
        plan_source_id: str = "",
        resume_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pe = PipelineEvalRunner.ensure(job)
        raw = evaluate_plan_contract(
            raw_tool_calls,
            context_packet=context_packet,
            target_template=target_template,
            structure_analysis=structure_analysis,
            plan_confidence=plan_confidence,
            approval_items=approval_items,
            plan_source_id=plan_source_id,
            resume_state=resume_state,
        )
        final = evaluate_plan_contract(
            finalized_tool_calls,
            context_packet=context_packet,
            target_template=target_template,
            structure_analysis=structure_analysis,
            plan_confidence=plan_confidence,
            approval_items=approval_items,
            plan_source_id=plan_source_id,
            resume_state=resume_state,
        )
        from sia.evals.plan_contract import evaluate_argument_correctness

        args = evaluate_argument_correctness(
            finalized_tool_calls,
            structure_analysis=structure_analysis,
            context_packet=context_packet,
        )
        bucket = ensure_per_source_bucket(pe, source_id)
        bucket["plan"] = {
            "pass": final.get("pass", True),
            "metrics": {
                **dict(final.get("metrics") or {}),
                **dict(args.get("metrics") or {}),
                "raw_tool_contract_score": (raw.get("metrics") or {}).get("tool_contract_score"),
                "final_tool_contract_score": (final.get("metrics") or {}).get("tool_contract_score"),
            },
            "violations": list(final.get("violations") or []) + list(args.get("violations") or []),
            "raw": {
                "pass": raw.get("pass", True),
                "metrics": dict(raw.get("metrics") or {}),
                "violations": list(raw.get("violations") or []),
            },
        }
        PipelineEvalRunner.recompute_critical_gate(job)
        return bucket["plan"]

    @staticmethod
    def record_plan_review(
        job: Dict[str, Any],
        source_id: str,
        *,
        approval_items: Optional[List[Dict[str, Any]]] = None,
        plan_confidence: float = 0.0,
        context_packet: Optional[Dict[str, Any]] = None,
        structure_analysis: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pe = PipelineEvalRunner.ensure(job)
        result = evaluate_plan_review_quality(
            approval_items,
            plan_confidence=plan_confidence,
            context_packet=context_packet,
            structure_analysis=structure_analysis,
        )
        bucket = ensure_per_source_bucket(pe, source_id)
        bucket["plan_review"] = result
        return result

    @staticmethod
    def record_execution_event(
        job: Dict[str, Any],
        source_id: str,
        event: Dict[str, Any],
    ) -> None:
        pe = PipelineEvalRunner.ensure(job)
        bucket = ensure_per_source_bucket(pe, source_id)
        exec_row = dict(bucket.get("execution") or {"pass": True, "integrity_events": [], "violations": []})
        events = list(exec_row.get("integrity_events") or [])
        events.append(dict(event))
        exec_row["integrity_events"] = events[-50:]
        violations = list(exec_row.get("violations") or [])
        subtype = str(event.get("subtype") or event.get("type") or "")
        if subtype in CRITICAL_INTEGRITY_SUBTYPES:
            violations.append(
                violation(
                    vtype=subtype,
                    message=str(event.get("message") or event.get("title") or subtype),
                    severity="critical",
                    evidence=dict(event),
                )
            )
        exec_row["violations"] = violations
        exec_row["pass"] = not any(
            str(v.get("severity") or "").lower() == "critical" for v in violations if isinstance(v, dict)
        )
        bucket["execution"] = exec_row
        PipelineEvalRunner.recompute_critical_gate(job)

    @staticmethod
    def record_execution_deferral(
        job: Dict[str, Any],
        source_id: str,
        *,
        defer_grain: bool,
        planned_tools: Optional[List[Dict[str, Any]]] = None,
        executed_tools: Optional[List[Dict[str, Any]]] = None,
        deferred_tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        from sia.evals.execution_accuracy import evaluate_execution_deferral

        pe = PipelineEvalRunner.ensure(job)
        sid = str(source_id or "").strip()
        result = evaluate_execution_deferral(
            defer_grain=defer_grain,
            planned_tools=planned_tools,
            executed_tools=executed_tools,
            deferred_tools=deferred_tools,
            source_id=sid,
        )
        bucket = ensure_per_source_bucket(pe, source_id)
        exec_row = dict(bucket.get("execution") or {"pass": True, "integrity_events": [], "violations": []})
        exec_row["metrics"] = {
            **dict(exec_row.get("metrics") or {}),
            **dict(result.get("metrics") or {}),
        }
        existing = list(exec_row.get("violations") or [])
        seen_types = {str(v.get("type") or "") for v in existing if isinstance(v, dict)}
        for v in result.get("violations") or []:
            if not isinstance(v, dict):
                continue
            vtype = str(v.get("type") or "")
            if vtype in seen_types:
                continue
            existing.append(v)
            seen_types.add(vtype)
        exec_row["violations"] = existing
        exec_row["pass"] = not any(
            str(v.get("severity") or "").lower() == "critical" for v in existing if isinstance(v, dict)
        )
        if sid and result.get("metrics"):
            inv_by_source = job.setdefault("tool_invocations_by_source", {})
            if isinstance(inv_by_source, dict):
                inv_by_source[sid] = {
                    "planned": (result.get("metrics") or {}).get("planned_invocations") or [],
                    "executed": (result.get("metrics") or {}).get("executed_invocations") or [],
                }
        bucket["execution"] = exec_row
        PipelineEvalRunner.recompute_critical_gate(job)
        return exec_row

    @staticmethod
    def record_context_isolation(
        job: Dict[str, Any],
        source_id: str,
        *,
        frame,
        context_packet: Optional[Dict[str, Any]] = None,
        target_template: Optional[Dict[str, Any]] = None,
        precomputed: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pe = PipelineEvalRunner.ensure(job)
        if precomputed is not None:
            result = dict(precomputed)
        else:
            result = assess_source_context_isolation(
                frame,
                context_packet=context_packet,
                target_template=target_template,
            )
        bucket = ensure_per_source_bucket(pe, source_id)
        local_ctx = dict(result.get("local_context") or {})
        scoped_ctx = dict(result.get("scoped_local_context") or {})
        metrics = dict(result.get("metrics") or {})
        if local_ctx:
            metrics["local_context"] = local_ctx
        if scoped_ctx:
            metrics["scoped_local_context"] = scoped_ctx
        bucket["context_isolation"] = {
            "pass": bool(result.get("pass", True)),
            "violations": list(result.get("violations") or []),
            "metrics": metrics,
        }
        bucket["pass"] = PipelineEvalRunner._source_pass(bucket)
        PipelineEvalRunner.recompute_critical_gate(job)
        return bucket["context_isolation"]

    @staticmethod
    def record_context_hop_eval(job: Dict[str, Any], source_id: str) -> Dict[str, Any]:
        """Record hop-limit violations on the execution stage bucket."""
        from sia.evals.context_propagation import evaluate_context_hop_events

        pe = PipelineEvalRunner.ensure(job)
        result = evaluate_context_hop_events(job, source_id=str(source_id))
        bucket = ensure_per_source_bucket(pe, source_id)
        exec_row = dict(bucket.get("execution") or {"pass": True, "integrity_events": [], "violations": []})
        exec_row["metrics"] = {
            **dict(exec_row.get("metrics") or {}),
            **dict(result.get("metrics") or {}),
        }
        existing = list(exec_row.get("violations") or [])
        seen = {str(v.get("type") or "") for v in existing if isinstance(v, dict)}
        for v in result.get("violations") or []:
            if not isinstance(v, dict):
                continue
            vtype = str(v.get("type") or "")
            if vtype in seen:
                continue
            existing.append(v)
            seen.add(vtype)
        exec_row["violations"] = existing
        exec_row["pass"] = not any(
            str(v.get("severity") or "").lower() == "critical" for v in existing if isinstance(v, dict)
        )
        bucket["execution"] = exec_row
        bucket["pass"] = PipelineEvalRunner._source_pass(bucket)
        PipelineEvalRunner.recompute_critical_gate(job)
        return exec_row

    @staticmethod
    def record_collation(
        job: Dict[str, Any],
        *,
        frames_by_source: Optional[Dict[str, Any]] = None,
        join_keys: Optional[List[str]] = None,
        measure_cols: Optional[List[str]] = None,
        target_template: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pe = PipelineEvalRunner.ensure(job)
        per = dict(pe.get("per_source") or {})
        cross = evaluate_cross_source_isolation(per)
        union = evaluate_union_false_duplicates(
            frames_by_source or {},
            join_keys=join_keys,
            measure_cols=measure_cols,
            target_template=target_template,
        )
        collation = {
            "pass": bool(cross.get("pass", True) and union.get("pass", True)),
            "metrics": {
                **dict(cross.get("metrics") or {}),
                **dict(union.get("metrics") or {}),
            },
            "violations": list(cross.get("violations") or []) + list(union.get("violations") or []),
        }
        pe["collation"] = collation
        PipelineEvalRunner.recompute_critical_gate(job)
        return collation

    @staticmethod
    def record_judge_mirror(job: Dict[str, Any], judge_result: Optional[Dict[str, Any]]) -> None:
        pe = PipelineEvalRunner.ensure(job)
        pe["judge"] = dict(judge_result or {})

    @staticmethod
    def record_quality(
        job: Dict[str, Any],
        quality: Optional[Dict[str, Any]],
        *,
        source_id: str = "",
    ) -> Dict[str, Any]:
        """Persist composite quality score (0-100) for Evals UI, plus per-source breakdown."""
        pe = PipelineEvalRunner.ensure(job)
        row = dict(quality or {})
        if source_id:
            row["source_id"] = str(source_id)
        try:
            from sia.evals.quality_score import compute_all_source_quality

            row["per_source"] = compute_all_source_quality(job)
        except Exception:  # pragma: no cover - defensive
            row["per_source"] = {}
        pe["quality"] = row
        return row

    @staticmethod
    def record_override(job: Dict[str, Any], *, reason: str, actor: str = "analyst") -> None:
        pe = PipelineEvalRunner.ensure(job)
        overrides = list(pe.get("overrides") or [])
        overrides.append({"reason": reason, "actor": actor})
        pe["overrides"] = overrides
        PipelineEvalRunner.recompute_critical_gate(job)

    @staticmethod
    def _source_pass(bucket: Dict[str, Any]) -> bool:
        for stage in ("structure", "plan", "execution", "context_isolation"):
            row = bucket.get(stage)
            if isinstance(row, dict) and row.get("pass") is False:
                return False
        return True

    @staticmethod
    def recompute_critical_gate(job: Dict[str, Any]) -> Dict[str, Any]:
        pe = PipelineEvalRunner.ensure(job)
        blocked: List[str] = []
        per = dict(pe.get("per_source") or {})

        for sid, bucket in per.items():
            if not isinstance(bucket, dict):
                continue
            for stage_key in ("plan", "execution", "context_isolation"):
                stage = bucket.get(stage_key)
                if not isinstance(stage, dict):
                    continue
                for v in stage.get("violations") or []:
                    if not isinstance(v, dict):
                        continue
                    vtype = str(v.get("type") or "")
                    sev = str(v.get("severity") or "").lower()
                    if sev == "critical" or vtype in CRITICAL_VIOLATION_TYPES:
                        blocked.append(f"{vtype}:{sid}")

        collation = pe.get("collation")
        if isinstance(collation, dict) and not collation.get("pass", True):
            for v in collation.get("violations") or []:
                if isinstance(v, dict):
                    blocked.append(f"collation:{v.get('type', 'fail')}")

        overrides = list(pe.get("overrides") or [])
        gate_pass = len(blocked) == 0 or bool(overrides)
        pe["critical_gate"] = {
            "pass": gate_pass,
            "blocked_reasons": blocked,
            "overridden": bool(overrides) and len(blocked) > 0,
        }
        return pe["critical_gate"]

    @staticmethod
    def critical_gate_blocks(job: Dict[str, Any]) -> bool:
        pe = PipelineEvalRunner.ensure(job)
        gate = pe.get("critical_gate") or {}
        return not bool(gate.get("pass", True))

    @staticmethod
    def blocked_reasons(job: Dict[str, Any]) -> List[str]:
        pe = PipelineEvalRunner.ensure(job)
        gate = pe.get("critical_gate") or {}
        return list(gate.get("blocked_reasons") or [])

    @staticmethod
    def summarize_for_judge(job: Dict[str, Any]) -> str:
        """Compact text for LLM judge prompt augmentation (Tier 3)."""
        pe = PipelineEvalRunner.ensure(job)
        lines: List[str] = []
        gate = pe.get("critical_gate") or {}
        lines.append(f"Critical gate: {'PASS' if gate.get('pass') else 'FAIL'}")
        for reason in gate.get("blocked_reasons") or []:
            lines.append(f"  - blocked: {reason}")
        per = pe.get("per_source") or {}
        for sid, bucket in per.items():
            if not isinstance(bucket, dict):
                continue
            lines.append(f"Source {sid}:")
            for stage in ("structure", "plan", "execution", "context_isolation"):
                row = bucket.get(stage)
                if not isinstance(row, dict):
                    continue
                status = "PASS" if row.get("pass", True) else "FAIL"
                lines.append(f"  {stage}: {status}")
                for v in (row.get("violations") or [])[:3]:
                    if isinstance(v, dict):
                        lines.append(f"    - {v.get('type')}: {v.get('message')}")
        coll = pe.get("collation")
        if isinstance(coll, dict) and coll.get("violations"):
            lines.append("Collation:")
            for v in coll.get("violations") or []:
                if isinstance(v, dict):
                    lines.append(f"  - {v.get('message')}")
        diff_tail = list(job.get("context_diff_log") or [])[-8:]
        if diff_tail:
            lines.append("Context diff trail (recent):")
            for row in diff_tail:
                if not isinstance(row, dict):
                    continue
                lines.append(
                    f"  - [{row.get('stage')}] {row.get('field')}: "
                    f"{row.get('before')!r} → {row.get('after')!r}"
                )
        for sid, bucket in per.items():
            if not isinstance(bucket, dict):
                continue
            ctx_row = bucket.get("context_isolation")
            if not isinstance(ctx_row, dict):
                continue
            metrics = ctx_row.get("metrics") if isinstance(ctx_row.get("metrics"), dict) else {}
            scoped = metrics.get("scoped_local_context") or metrics.get("local_context") or {}
            if scoped:
                lines.append(f"Scoped context ({sid}): {scoped}")
        return "\n".join(lines)
