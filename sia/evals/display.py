"""Human-readable rollup of pipeline_evals for the Evals UI."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sia.evals.schema import CRITICAL_VIOLATION_TYPES

STAGE_ORDER: Tuple[str, ...] = (
    "structure",
    "plan",
    "plan_review",
    "execution",
    "context_isolation",
    "collation",
)

STAGE_LABELS: Dict[str, str] = {
    "structure": "Structure analysis",
    "plan": "Plan contract",
    "plan_review": "Plan review (HITL)",
    "execution": "Tool execution",
    "context_isolation": "Context isolation",
    "collation": "Multi-source collation",
}

VIOLATION_TYPE_LABELS: Dict[str, str] = {
    "zero_rows": "Empty output",
    "mass_row_loss": "Catastrophic row loss",
    "metric_column_wiped": "Metric column wiped",
    "aggregate_sum_mismatch": "Sum mismatch",
    "dimension_mismatch": "Context bleed",
    "metric_mismatch": "Metric mismatch",
    "memory_scope": "Wrong source memory",
    "context_grounding_critical": "Plan context bleed",
    "context_lineage_missing": "Missing sheet lineage",
    "union_false_duplicate": "False union duplicate",
    "layout_alignment": "Layout alignment",
    "signal_consistency": "Signal inconsistency",
    "tool_contract_sequence": "Tool order issue",
    "required_tools_missing": "Missing required tool",
    "unnecessary_tools": "Unnecessary tool",
    "context_completeness": "Incomplete context",
    "structure_plan_alignment": "Structure/plan mismatch",
    "plan_confidence_sanity": "Plan confidence noise",
    "review_miss": "Missed review question",
    "review_noise": "Noisy review question",
    "grain_tool_executed_pre_collation": "Grain tool ran pre-collation",
    "grain_tool_missing_deferral": "Grain deferral missing",
    "sparse_dimension_false_positive": "Sparse dimension false positive",
    "grouped_layout_false_positive": "Grouped layout false positive",
    "argument_correctness": "Tool argument mismatch",
    "plan_adherence_drift": "Plan adherence drift",
}

# Key metrics to surface in Evals UI per stage (rollup shows min/avg as appropriate).
STAGE_SCORE_METRICS: Dict[str, Tuple[str, ...]] = {
    "structure": (
        "layout_scan_score",
        "sparse_dimension_precision",
        "analyzer_confidence",
        "sparse_dimension_false_positive_count",
    ),
    "plan": ("tool_contract_score", "final_tool_contract_score", "argument_correctness_score"),
    "plan_review": ("necessity_score",),
    "execution": ("deferral_ok", "invocation_adherence_score", "plan_adherence_score"),
}

EXECUTION_DISPLAY_METRICS: Tuple[str, ...] = (
    "deferral_ok",
    "invocation_adherence_score",
    "plan_adherence_score",
    "invocation_mismatch_count",
    "invocation_extra_count",
    "invocation_missing_count",
    "grain_tools_deferred",
    "grain_tools_executed",
    "verifier_issue_count",
)

STAGE_SNAPSHOT_HINTS: Dict[str, Dict[str, str]] = {
    "structure": {"label": "state.analyze_structure", "phase": "node_after"},
    "plan": {"label": "state.generate_plan", "phase": "node_after"},
    "plan_review": {"label": "state.generate_plan", "phase": "node_after"},
    "execution": {"label": "state.execute_tools.deferral", "phase": "deferral_decision"},
    "context_isolation": {"label": "job.multi_source.source_done", "phase": "after_process_file"},
    "collation": {"label": "job.multi_source.collation", "phase": "after_duplicate_check_before_deferred"},
}


def _snapshot_hint_for_issue(stage: str, source_id: str) -> Dict[str, str]:
    base = dict(STAGE_SNAPSHOT_HINTS.get(stage) or {})
    if stage == "collation":
        base["source_id"] = "__collation__"
    elif source_id:
        base["source_id"] = source_id
    return base

VERDICT_SORT_KEY = {"blocked": 0, "advisory": 1, "pending": 2, "pass": 3}


def _source_labels_from_job(job: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    labels: Dict[str, Dict[str, str]] = {}
    for row in list(job.get("source_registry") or []):
        if not isinstance(row, dict):
            continue
        sid = str(row.get("source_id") or "").strip()
        if not sid:
            continue
        labels[sid] = {
            "sheet_name": str(row.get("sheet_name") or "").strip(),
            "file_name": str(row.get("file_name") or job.get("filename") or "").strip(),
        }
    return labels


def _sheet_name_for_source(source_id: str, labels: Dict[str, Dict[str, str]]) -> str:
    if not source_id:
        return ""
    meta = labels.get(source_id) or {}
    sheet = str(meta.get("sheet_name") or "").strip()
    if sheet:
        return sheet
    # Fallback: last segment of source_id
    parts = source_id.replace(":", "_").split("_")
    for part in reversed(parts):
        if part and not part.isdigit() and len(part) > 2:
            return part
    return source_id


def _violation_severity(v: Dict[str, Any]) -> str:
    sev = str(v.get("severity") or "").lower()
    if sev == "critical":
        return "critical"
    vtype = str(v.get("type") or "")
    if vtype in CRITICAL_VIOLATION_TYPES:
        return "critical"
    return "advisory"


def _type_label(vtype: str) -> str:
    return VIOLATION_TYPE_LABELS.get(vtype, vtype.replace("_", " ").title())


def _collect_issues(
    pe: Dict[str, Any],
    source_labels: Dict[str, Dict[str, str]],
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    per = dict(pe.get("per_source") or {})

    for sid, bucket in per.items():
        if not isinstance(bucket, dict):
            continue
        for stage in STAGE_ORDER:
            if stage == "collation":
                continue
            stage_row = bucket.get(stage)
            if not isinstance(stage_row, dict):
                continue
            for v in stage_row.get("violations") or []:
                if not isinstance(v, dict):
                    continue
                vtype = str(v.get("type") or "")
                issue: Dict[str, Any] = {
                        "severity": _violation_severity(v),
                        "stage": stage,
                        "stage_label": STAGE_LABELS.get(stage, stage),
                        "type": vtype,
                        "type_label": _type_label(vtype),
                        "source_id": str(sid),
                        "sheet_name": _sheet_name_for_source(str(sid), source_labels),
                        "message": str(v.get("message") or ""),
                        "evidence": dict(v.get("evidence") or {}),
                    }
                for field in (
                    "key",
                    "expected_spends",
                    "actual_spends",
                    "expected_impressions",
                    "actual_impressions",
                ):
                    if field in v:
                        issue[field] = v[field]
                issue["snapshot_hint"] = _snapshot_hint_for_issue(stage, str(sid))
                issues.append(issue)

    coll = pe.get("collation")
    if isinstance(coll, dict):
        for v in coll.get("violations") or []:
            if not isinstance(v, dict):
                continue
            vtype = str(v.get("type") or "")
            issues.append(
                {
                    "severity": _violation_severity(v),
                    "stage": "collation",
                    "stage_label": STAGE_LABELS["collation"],
                    "type": vtype,
                    "type_label": _type_label(vtype),
                    "source_id": "",
                    "sheet_name": "",
                    "message": str(v.get("message") or ""),
                    "evidence": dict(v.get("evidence") or {}),
                    "snapshot_hint": _snapshot_hint_for_issue("collation", ""),
                }
            )

    issues.sort(key=lambda x: (0 if x["severity"] == "critical" else 1, x.get("stage") or ""))
    return issues


def _merge_stage_metrics(pe: Dict[str, Any], stage: str) -> Dict[str, Any]:
    """Aggregate per-source stage metrics for Evals scorecard (min for scores, sum for counts)."""
    per = dict(pe.get("per_source") or {})
    merged: Dict[str, Any] = {}
    score_keys = {
        "layout_scan_score",
        "sparse_dimension_precision",
        "tool_contract_score",
        "final_tool_contract_score",
        "raw_tool_contract_score",
        "necessity_score",
        "analyzer_confidence",
        "argument_correctness_score",
        "invocation_adherence_score",
        "plan_adherence_score",
    }
    count_keys = {
        "sparse_dimension_false_positive_count",
        "sparse_dimension_flagged_count",
        "block_sparse_metric_count",
        "verifier_issue_count",
        "argument_bad_ref_count",
        "invocation_mismatch_count",
        "invocation_extra_count",
        "invocation_missing_count",
    }

    if stage == "collation":
        coll = pe.get("collation")
        if isinstance(coll, dict):
            return dict(coll.get("metrics") or {})
        return merged

    for bucket in per.values():
        if not isinstance(bucket, dict):
            continue
        row = bucket.get(stage)
        if not isinstance(row, dict):
            continue
        m = dict(row.get("metrics") or {})
        for key, val in m.items():
            if key in score_keys and isinstance(val, (int, float)):
                prev = merged.get(key)
                merged[key] = min(prev, val) if isinstance(prev, (int, float)) else val
            elif key in count_keys and isinstance(val, (int, float)):
                merged[key] = int(merged.get(key) or 0) + int(val)
            elif key == "grain_tools_deferred" and isinstance(val, list):
                existing = list(merged.get(key) or [])
                for item in val:
                    if item not in existing:
                        existing.append(item)
                merged[key] = existing
            elif key not in merged:
                merged[key] = val

    if stage == "execution":
        for key in EXECUTION_DISPLAY_METRICS:
            if key in merged:
                continue
    return merged


def _stage_rollups(pe: Dict[str, Any], issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rollups: List[Dict[str, Any]] = []
    per = dict(pe.get("per_source") or {})
    has_sources = bool(per)

    for stage in STAGE_ORDER:
        critical = sum(1 for i in issues if i["stage"] == stage and i["severity"] == "critical")
        advisory = sum(1 for i in issues if i["stage"] == stage and i["severity"] == "advisory")
        stage_pass = True
        not_run = True

        if stage == "collation":
            coll = pe.get("collation")
            if isinstance(coll, dict) and coll:
                not_run = False
                stage_pass = bool(coll.get("pass", True)) and critical == 0
        else:
            for bucket in per.values():
                if not isinstance(bucket, dict):
                    continue
                row = bucket.get(stage)
                if isinstance(row, dict):
                    not_run = False
                    if row.get("pass") is False or critical > 0:
                        stage_pass = False

        status = "not_run"
        if not not_run:
            if critical > 0 or not stage_pass:
                status = "fail"
            elif advisory > 0:
                status = "warn"
            else:
                status = "pass"

        rollups.append(
            {
                "stage": stage,
                "stage_label": STAGE_LABELS.get(stage, stage),
                "pass": stage_pass and critical == 0,
                "status": status,
                "critical": critical,
                "advisory": advisory,
                "not_run": not_run and stage != "collation",
                "metrics": _merge_stage_metrics(pe, stage),
            }
        )

    if not has_sources and not pe.get("collation"):
        for r in rollups:
            if r["stage"] != "collation":
                r["not_run"] = True
                r["status"] = "not_run"

    return rollups


def _recommended_actions(
    job: Dict[str, Any],
    issues: List[Dict[str, Any]],
    export_safe: bool,
    overridden: bool,
) -> List[str]:
    actions: List[str] = ["open_debug"]
    status = str(job.get("status") or "").lower()
    if status in ("awaiting_review", "awaiting_approval"):
        actions.append("open_review")
    if status == "processing":
        actions.append("open_processing")
    if not export_safe and not overridden:
        critical_types = {i.get("type") for i in issues if i.get("severity") == "critical"}
        if critical_types & {"union_false_duplicate", "dimension_mismatch", "metric_mismatch"}:
            if "open_review" not in actions and status != "completed":
                actions.append("open_review")
    return list(dict.fromkeys(actions))


def _next_steps(
    issues: List[Dict[str, Any]],
    export_safe: bool,
    overridden: bool,
) -> List[str]:
    if export_safe and not issues:
        return ["All pipeline checks passed. Safe to export; LLM judge below is optional confirmation."]
    if overridden:
        return [
            "Analyst override is logged. Export may proceed, but review overrides in Debug before production use.",
        ]
    if export_safe:
        adv = [i for i in issues if i.get("severity") == "advisory"]
        if adv:
            return [
                "Export is not blocked, but advisory quality signals were recorded.",
                "Optional: review plan/structure notes in Debug → Pipeline Evals.",
            ]
        return ["All critical checks passed. Safe to export."]

    steps: List[str] = []
    critical = [i for i in issues if i.get("severity") == "critical"]
    types = {i.get("type") for i in critical}

    if "dimension_mismatch" in types or "context_grounding_critical" in types or "context_lineage_missing" in types:
        steps.append("Re-process affected sheet(s) or fix context bleed in Debug → Pipeline Evals.")
    if "metric_mismatch" in types:
        steps.append("Verify per-source metrics match clean template references before merging.")
    if "sparse_dimension_false_positive" in types or "grouped_layout_false_positive" in types:
        steps.append(
            "Structure layout scan disagreed with the analyzer — check Structure stage scores "
            "(layout_scan_score, sparse_dimension_precision) before trusting grouped-row plan tools."
        )
    if "union_false_duplicate" in types:
        steps.append("Open Review → relationship review before export; union keys may be hiding cross-sheet duplicates.")
    if types & {"zero_rows", "mass_row_loss", "metric_column_wiped", "aggregate_sum_mismatch"}:
        steps.append("Inspect tool execution integrity events in Debug; re-run or adjust the plan for affected sources.")
    if "memory_scope" in types:
        steps.append("Clear persisted plan state and replan so each sheet uses its own context packet.")
    if not steps:
        steps.append("Resolve critical pipeline eval failures in Debug → Pipeline Evals before export.")

    return steps[:3]


def _headline(issues: List[Dict[str, Any]], export_safe: bool, overridden: bool) -> str:
    if not issues and export_safe:
        return "All checks passed"
    critical = [i for i in issues if i.get("severity") == "critical"]
    if critical:
        top = critical[0]
        sheet = top.get("sheet_name") or ""
        label = top.get("type_label") or top.get("type") or "Critical issue"
        if sheet:
            return f"{label} on {sheet}"
        return label
    if overridden:
        return "Override logged — export allowed"
    adv = [i for i in issues if i.get("severity") == "advisory"]
    if adv:
        return f"{len(adv)} advisory signal(s)"
    if export_safe:
        return "All checks passed"
    return "Pipeline evals recorded"


def compute_context_health(job: Dict[str, Any]) -> Dict[str, Any]:
    """Rollup for Evals dashboard and processing chip."""
    pe = job.get("pipeline_evals") if isinstance(job.get("pipeline_evals"), dict) else {}
    per = dict(pe.get("per_source") or {})
    registry = list(job.get("source_registry") or [])
    source_ids = list(per.keys()) or [
        str(row.get("source_id") or "")
        for row in registry
        if isinstance(row, dict) and row.get("source_id")
    ]
    passing = 0
    bleed_count = 0
    max_hop = 0
    bleed_types = {
        "dimension_mismatch",
        "metric_mismatch",
        "context_grounding_critical",
        "memory_scope",
        "context_lineage_missing",
    }
    for sid, bucket in per.items():
        if not isinstance(bucket, dict):
            continue
        ctx = bucket.get("context_isolation") if isinstance(bucket.get("context_isolation"), dict) else {}
        if ctx.get("pass", True):
            passing += 1
        for v in ctx.get("violations") or []:
            if isinstance(v, dict) and str(v.get("type") or "") in bleed_types:
                bleed_count += 1
        metrics = ctx.get("metrics") if isinstance(ctx.get("metrics"), dict) else {}
        scoped = metrics.get("scoped_local_context") if isinstance(metrics.get("scoped_local_context"), dict) else {}
        for row in scoped.values():
            if isinstance(row, dict):
                try:
                    max_hop = max(max_hop, int(row.get("hop") or 0))
                except (TypeError, ValueError):
                    pass
    for row in job.get("context_diff_log") or []:
        if not isinstance(row, dict):
            continue
        try:
            max_hop = max(max_hop, int(row.get("hop") or 0))
        except (TypeError, ValueError):
            pass
    total = len(source_ids) or len(per)
    pass_pct = int(round(100.0 * passing / total)) if total else 100
    return {
        "sources_total": total,
        "sources_passing": passing,
        "pass_pct": pass_pct,
        "bleed_count": bleed_count,
        "max_hop": max_hop,
    }


def build_active_context_scope(job: Dict[str, Any]) -> Dict[str, Any]:
    """Chip text for Console while a source is processing."""
    cp = job.get("context_packet") if isinstance(job.get("context_packet"), dict) else {}
    ux = job.get("ux_stepper_summary") if isinstance(job.get("ux_stepper_summary"), dict) else {}
    sid = str(
        ux.get("current_source_id")
        or job.get("selected_source_id")
        or job.get("mapping_source_id")
        or (cp.get("lineage") or {}).get("source_id")
        or ""
    ).strip()
    sheet = ""
    local: Dict[str, Any] = {}
    if cp:
        sm = cp.get("source_metadata") if isinstance(cp.get("source_metadata"), dict) else {}
        lin = cp.get("lineage") if isinstance(cp.get("lineage"), dict) else {}
        sheet = str(lin.get("sheet_name") or sm.get("sheet_name") or "").strip()
        ic = cp.get("interpreted_context") if isinstance(cp.get("interpreted_context"), dict) else {}
        local = dict(ic.get("fields") or {})
    if not local and sid:
        pe = job.get("pipeline_evals") if isinstance(job.get("pipeline_evals"), dict) else {}
        bucket = dict((pe.get("per_source") or {}).get(sid) or {})
        ctx = bucket.get("context_isolation") if isinstance(bucket.get("context_isolation"), dict) else {}
        metrics = ctx.get("metrics") if isinstance(ctx.get("metrics"), dict) else {}
        local = dict(metrics.get("local_context") or {})
    if not sheet and sid:
        for row in job.get("source_registry") or []:
            if isinstance(row, dict) and str(row.get("source_id") or "") == sid:
                sheet = str(row.get("sheet_name") or "").strip()
                break
    return {
        "source_id": sid,
        "sheet_name": sheet,
        "local_context": local,
        "chip": sheet,
    }


def _resolve_quality(job: Dict[str, Any], pe: Dict[str, Any]) -> Dict[str, Any]:
    """Return stored quality score or compute on read for legacy jobs."""
    stored = pe.get("quality") if isinstance(pe.get("quality"), dict) else {}
    if stored.get("overall") is not None:
        return dict(stored)
    try:
        from sia.evals.quality_score import compute_quality_scores

        judge = pe.get("judge") if isinstance(pe.get("judge"), dict) else {}
        if not judge:
            judge = job.get("judge_result") if isinstance(job.get("judge_result"), dict) else {}
        trace = job.get("trace") if isinstance(job.get("trace"), dict) else {}
        trace_steps = trace.get("steps") or job.get("trace_steps") or []
        tools = job.get("tool_executions") or []
        schema = None
        if isinstance(trace, dict) and trace.get("final_schema"):
            schema = trace.get("final_schema")
        return compute_quality_scores(
            job,
            judge_result=judge,
            trace_steps=trace_steps,
            tool_executions=tools,
            schema=schema,
            use_deepeval=False,
        )
    except Exception:
        return {}


def build_eval_display(job: Dict[str, Any]) -> Dict[str, Any]:
    """Compute UI-facing eval summary from job.pipeline_evals."""
    pe = job.get("pipeline_evals")
    source_labels = _source_labels_from_job(job)

    if not isinstance(pe, dict) or not pe:
        return {
            "verdict": "pending",
            "headline": "Evals pending — processing not complete",
            "export_safe": True,
            "overridden": False,
            "counts": {"critical": 0, "advisory": 0, "sources": len(source_labels)},
            "stage_rollups": [
                {
                    "stage": s,
                    "stage_label": STAGE_LABELS[s],
                    "pass": True,
                    "status": "not_run",
                    "critical": 0,
                    "advisory": 0,
                    "not_run": True,
                }
                for s in STAGE_ORDER
            ],
            "issues": [],
            "source_labels": source_labels,
            "recommended_actions": _recommended_actions(job, [], True, False),
            "next_steps": ["Run processing through structure, plan, and finalize to populate pipeline evals."],
            "context_health": compute_context_health(job),
            "quality": {},
        }

    gate = pe.get("critical_gate") if isinstance(pe.get("critical_gate"), dict) else {}
    overridden = bool(gate.get("overridden"))
    gate_pass = bool(gate.get("pass", True))
    export_safe = gate_pass

    issues = _collect_issues(pe, source_labels)
    critical_count = sum(1 for i in issues if i["severity"] == "critical")
    advisory_count = sum(1 for i in issues if i["severity"] == "advisory")
    per = dict(pe.get("per_source") or {})

    if critical_count > 0 and not overridden:
        verdict = "blocked"
    elif advisory_count > 0:
        verdict = "advisory"
    elif not per and not pe.get("collation"):
        verdict = "pending"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "headline": _headline(issues, export_safe, overridden),
        "export_safe": export_safe,
        "overridden": overridden,
        "counts": {
            "critical": critical_count,
            "advisory": advisory_count,
            "sources": len(per) or len(source_labels),
        },
        "stage_rollups": _stage_rollups(pe, issues),
        "issues": issues,
        "source_labels": source_labels,
        "recommended_actions": _recommended_actions(job, issues, export_safe, overridden),
        "next_steps": _next_steps(issues, export_safe, overridden),
        "context_health": compute_context_health(job),
        "quality": _resolve_quality(job, pe),
        "per_source_quality": _resolve_per_source_quality(job, pe),
    }


def _resolve_per_source_quality(job: Dict[str, Any], pe: Dict[str, Any]) -> Dict[str, Any]:
    """Return stored per-source quality or compute on read. Never raises."""
    stored = pe.get("quality") if isinstance(pe.get("quality"), dict) else {}
    ps = stored.get("per_source") if isinstance(stored.get("per_source"), dict) else None
    if ps:
        return dict(ps)
    try:
        from sia.evals.quality_score import compute_all_source_quality

        return compute_all_source_quality(job)
    except Exception:
        return {}


def eval_display_sort_key(display: Dict[str, Any], created_at: str = "") -> Tuple[int, str]:
    verdict = str(display.get("verdict") or "pending")
    return (VERDICT_SORT_KEY.get(verdict, 2), created_at or "")
