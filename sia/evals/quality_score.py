"""Composite agentic quality score for the Evals scorecard.

Turns the deterministic pipeline gate + LLM judge + DeepEval agentic metrics
into a single 0-100 Quality Score with four sub-scores, so that a *passing*
job still communicates something meaningful (not just a wall of green ticks):

  * Task Completion  — did the agent accomplish the transformation task
                       (DeepEval GEval when available, else LLM judge fallback).
  * Plan Quality     — tool contract adherence blended with judge tool accuracy.
  * Step Efficiency  — deterministic: retries / errors / redundant tools.
  * Context Quality  — deterministic: context isolation health + bleed + hops.

Everything degrades gracefully; the composite always renders.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_TASK_OUTPUT_MAX_CHARS = 16_000
_TASK_SAMPLE_ROWS = 5
_TASK_SAMPLE_COLS = 12
_TASK_CRITIQUE_MAX_CHARS = 220

SUBSCORE_WEIGHTS: Dict[str, float] = {
    "task_completion": 0.35,
    "plan_quality": 0.25,
    "context_quality": 0.25,
    "step_efficiency": 0.15,
}

SUBSCORE_LABELS: Dict[str, str] = {
    "task_completion": "Task completion",
    "plan_quality": "Plan quality",
    "context_quality": "Context quality",
    "step_efficiency": "Step efficiency",
}

# Which sub-scores are meaningful at which level.
#   - "source": can be computed per sheet/source (and rolled up to the job).
#   - "job": only meaningful for the whole run (cross-source or full-trajectory).
SUBSCORE_LEVEL: Dict[str, str] = {
    "task_completion": "both",   # per-source via judge; DeepEval GEval only at job level
    "plan_quality": "source",
    "context_quality": "source",
    "step_efficiency": "both",   # per-source approx from execution metrics; token-eff is job level
}

# Metrics/stages that are inherently whole-job and cannot be split per source/block.
JOB_LEVEL_ONLY: List[str] = [
    "collation",              # cross-source isolation + union false-duplicates
    "deepeval_task_completion",  # GEval runs once on the whole run
    "token_efficiency",       # aggregated across the entire trajectory
]


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo


def _band(overall_0_100: float) -> str:
    if overall_0_100 >= 85:
        return "excellent"
    if overall_0_100 >= 70:
        return "good"
    if overall_0_100 >= 50:
        return "fair"
    return "poor"


def _iter_plan_buckets(pe: Dict[str, Any]):
    for bucket in (pe.get("per_source") or {}).values():
        if isinstance(bucket, dict) and isinstance(bucket.get("plan"), dict):
            yield bucket["plan"]


def _avg(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _score_plan_quality(pe: Dict[str, Any], judge_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    contract_scores: List[float] = []
    critical_plan_viol = 0
    for plan in _iter_plan_buckets(pe):
        metrics = plan.get("metrics") if isinstance(plan.get("metrics"), dict) else {}
        val = metrics.get("final_tool_contract_score")
        if val is None:
            val = metrics.get("tool_contract_score")
        if isinstance(val, (int, float)):
            contract_scores.append(float(val))
        for v in plan.get("violations") or []:
            if isinstance(v, dict) and str(v.get("severity") or "").lower() == "critical":
                critical_plan_viol += 1

    contract = _avg(contract_scores)
    tool_accuracy = None
    if isinstance(judge_result, dict):
        ta = judge_result.get("tool_accuracy")
        if isinstance(ta, (int, float)):
            tool_accuracy = float(ta)

    if contract is not None and tool_accuracy is not None:
        score = 0.6 * contract + 0.4 * tool_accuracy
        engine = "contract+judge"
    elif contract is not None:
        score = contract
        engine = "contract"
    elif tool_accuracy is not None:
        score = tool_accuracy
        engine = "judge"
    else:
        score = 0.7
        engine = "default"

    if critical_plan_viol:
        score = min(score, 0.4)

    return {
        "score": _clamp(score),
        "engine": engine,
        "detail": {
            "tool_contract_score": contract,
            "judge_tool_accuracy": tool_accuracy,
            "critical_plan_violations": critical_plan_viol,
        },
    }


def _score_context_quality(job: Dict[str, Any], pe: Dict[str, Any]) -> Dict[str, Any]:
    from sia.evals.display import compute_context_health

    health = compute_context_health(job)
    pass_pct = float(health.get("pass_pct") or 0) / 100.0
    bleed = int(health.get("bleed_count") or 0)
    max_hop = int(health.get("max_hop") or 0)

    grounding_bleed = 0
    for plan in _iter_plan_buckets(pe):
        metrics = plan.get("metrics") if isinstance(plan.get("metrics"), dict) else {}
        gb = metrics.get("context_grounding_bleed_count")
        if isinstance(gb, (int, float)):
            grounding_bleed += int(gb)

    score = pass_pct
    if bleed > 0:
        score = min(score, 0.45)
    if grounding_bleed > 0:
        score = min(score, 0.35)
    if max_hop > 1:
        score -= 0.15

    return {
        "score": _clamp(score),
        "engine": "deterministic",
        "detail": {
            "sources_pass_pct": health.get("pass_pct"),
            "bleed_count": bleed,
            "grounding_bleed": grounding_bleed,
            "max_hop": max_hop,
        },
    }


def _tool_rows_from(
    tool_executions: Optional[Sequence[Dict[str, Any]]],
    trace_steps: Optional[Sequence[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if tool_executions:
        for t in tool_executions:
            if isinstance(t, dict) and (t.get("tool") or t.get("module")):
                rows.append(t)
    if rows:
        return rows
    for s in trace_steps or []:
        if isinstance(s, dict) and s.get("tool"):
            rows.append(s)
    return rows


def _score_step_efficiency(
    tool_executions: Optional[Sequence[Dict[str, Any]]],
    trace_steps: Optional[Sequence[Dict[str, Any]]],
    judge_result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    rows = _tool_rows_from(tool_executions, trace_steps)
    tool_names: List[str] = []
    error_count = 0
    for r in rows:
        name = str(r.get("tool") or r.get("module") or "").strip()
        if name:
            tool_names.append(name)
        status = str(r.get("status") or "").lower()
        success = r.get("success")
        if status == "error" or success is False or r.get("error"):
            error_count += 1

    total = len(tool_names)
    retries = sum(1 for i in range(1, total) if tool_names[i] == tool_names[i - 1])
    redundant_verify = max(0, tool_names.count("verify.schema") - 1)

    structural = 1.0
    structural -= 0.15 * error_count
    structural -= 0.10 * retries
    structural -= 0.10 * redundant_verify
    structural = _clamp(structural)

    token_eff = None
    if isinstance(judge_result, dict):
        te = judge_result.get("token_efficiency")
        if isinstance(te, (int, float)) and te > 0:
            token_eff = float(te)

    if token_eff is not None:
        score = 0.7 * structural + 0.3 * _clamp(token_eff)
        engine = "trace+token"
    else:
        score = structural
        engine = "trace"

    if total == 0:
        score = 0.75
        engine = "no_tools"

    return {
        "score": _clamp(score),
        "engine": engine,
        "detail": {
            "tool_count": total,
            "error_count": error_count,
            "consecutive_retries": retries,
            "redundant_verify": redundant_verify,
            "token_efficiency": token_eff,
        },
    }


def _resolve_task_schema(
    job: Dict[str, Any],
    schema: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if isinstance(schema, dict) and schema.get("fields"):
        return schema
    job_schema = job.get("schema")
    if isinstance(job_schema, dict) and job_schema.get("fields"):
        return job_schema
    trace = job.get("trace") if isinstance(job.get("trace"), dict) else {}
    final_schema = trace.get("final_schema")
    if isinstance(final_schema, dict) and final_schema.get("fields"):
        return final_schema
    return schema if isinstance(schema, dict) else {}


def _schema_field_lines(schema_dict: Dict[str, Any]) -> List[str]:
    fields = schema_dict.get("fields") if isinstance(schema_dict.get("fields"), list) else []
    lines: List[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "?").strip()
        typ = str(field.get("type") or "unknown")
        role = field.get("role")
        suffix = f", role={role}" if role else ""
        lines.append(f"- {name} ({typ}{suffix})")
    return lines


def _format_row_sample(
    rows: Sequence[Dict[str, Any]],
    column_order: Optional[Sequence[str]] = None,
    *,
    max_rows: int = _TASK_SAMPLE_ROWS,
    max_cols: int = _TASK_SAMPLE_COLS,
) -> str:
    if not rows:
        return "(no rows)"
    cols: List[str] = []
    if column_order:
        cols = [str(c) for c in column_order if str(c).strip()][:max_cols]
    if not cols and isinstance(rows[0], dict):
        cols = [str(c) for c in list(rows[0].keys())[:max_cols]]
    lines: List[str] = []
    for idx, row in enumerate(rows[:max_rows]):
        if not isinstance(row, dict):
            continue
        slim = {c: row.get(c) for c in cols} if cols else dict(list(row.items())[:max_cols])
        lines.append(f"row {idx + 1}: {json.dumps(slim, default=str, ensure_ascii=False)}")
    return "\n".join(lines) if lines else "(no sample rows)"


def _best_tool_output_preview(
    job: Dict[str, Any],
    tool_executions: Optional[Sequence[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    preferred_tools = (
        "collation.verify_combined",
        "verify.schema",
        "transform.sort_rows",
        "transform.reorder_columns",
    )
    rows = list(tool_executions or job.get("tool_executions") or [])

    def _preview(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        preview = row.get("output_preview")
        if isinstance(preview, dict) and preview.get("sample"):
            return preview
        return None

    for tool_name in preferred_tools:
        for row in reversed(rows):
            if not isinstance(row, dict):
                continue
            if str(row.get("tool") or "") == tool_name:
                preview = _preview(row)
                if preview:
                    return preview

    for row in reversed(rows):
        if isinstance(row, dict):
            preview = _preview(row)
            if preview:
                return preview

    for event in reversed(list(job.get("debug_events") or [])):
        if not isinstance(event, dict):
            continue
        preview = event.get("output_preview")
        if isinstance(preview, dict) and preview.get("sample"):
            return preview
    return None


def _per_source_summary_lines(
    job: Dict[str, Any],
    judge_result: Optional[Dict[str, Any]],
) -> List[str]:
    pe = job.get("pipeline_evals") if isinstance(job.get("pipeline_evals"), dict) else {}
    per = pe.get("per_source") if isinstance(pe.get("per_source"), dict) else {}
    registry: Dict[str, Dict[str, Any]] = {}
    for row in job.get("source_registry") or []:
        if isinstance(row, dict) and row.get("source_id"):
            registry[str(row["source_id"])] = row

    judges_per: Dict[str, Any] = {}
    if isinstance(judge_result, dict) and isinstance(judge_result.get("per_source"), dict):
        judges_per = judge_result["per_source"]
    pe_judge = pe.get("judge") if isinstance(pe.get("judge"), dict) else {}
    if not judges_per and isinstance(pe_judge.get("per_source"), dict):
        judges_per = pe_judge["per_source"]

    source_ids = list(per.keys()) or list(registry.keys()) or list(judges_per.keys())
    lines: List[str] = []
    for sid in source_ids:
        sid_s = str(sid)
        bucket = per.get(sid_s) if isinstance(per.get(sid_s), dict) else {}
        reg = registry.get(sid_s) or {}
        sheet = str(reg.get("sheet_name") or sid_s).strip()

        ctx = bucket.get("context_isolation") if isinstance(bucket.get("context_isolation"), dict) else {}
        iso_pass = bool(ctx.get("pass", True))
        metrics = ctx.get("metrics") if isinstance(ctx.get("metrics"), dict) else {}
        local = metrics.get("local_context") if isinstance(metrics.get("local_context"), dict) else {}
        scoped = (
            metrics.get("scoped_local_context")
            if isinstance(metrics.get("scoped_local_context"), dict)
            else {}
        )

        jr = judges_per.get(sid_s) if isinstance(judges_per.get(sid_s), dict) else {}
        jm = jr.get("metrics") if isinstance(jr.get("metrics"), dict) else {}
        rows = jm.get("total_rows")
        if rows is None:
            rows = jm.get("output_rows")
        cols = jm.get("total_cols")

        local_bits: List[str] = []
        for key, val in local.items():
            if val is not None and str(val).strip():
                local_bits.append(f"{key}={val}")
        if not local_bits:
            for dim, scoped_row in scoped.items():
                if isinstance(scoped_row, dict) and scoped_row.get("value"):
                    local_bits.append(f"{dim}={scoped_row.get('value')}")

        line = f"- {sheet} ({sid_s}): context_isolation={'pass' if iso_pass else 'fail'}"
        if rows is not None:
            line += f", rows={rows}"
        if cols is not None:
            line += f", cols={cols}"
        if local_bits:
            line += ", stamped=" + ", ".join(local_bits[:6])
        lines.append(line)
    return lines


def _truncate_task_output(text: str, max_chars: int = _TASK_OUTPUT_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 24].rstrip() + "\n… [output truncated]"


def _task_texts(
    job: Dict[str, Any],
    judge_result: Optional[Dict[str, Any]],
    schema: Optional[Dict[str, Any]],
    *,
    tool_executions: Optional[Sequence[Dict[str, Any]]] = None,
) -> tuple[str, str]:
    template_cols: List[str] = []
    tpl = job.get("target_template") if isinstance(job.get("target_template"), dict) else {}
    props = tpl.get("properties") if isinstance(tpl.get("properties"), dict) else {}
    template_cols = list(props.keys())[:20]

    task = (
        "Transform a messy, semi-structured marketing spreadsheet into a flat, "
        "normalized, template-ready table. "
    )
    if template_cols:
        task += "Target template columns: " + ", ".join(template_cols) + ". "
    task += "Values must be faithful to the source with correct per-source context (no cross-sheet bleed)."

    schema_dict = _resolve_task_schema(job, schema)
    out_sections: List[str] = []

    field_lines = _schema_field_lines(schema_dict)
    if field_lines:
        total_rows = schema_dict.get("total_rows")
        header = "## Output columns (inferred schema)"
        if isinstance(total_rows, int) and total_rows > 0:
            header += f" — {total_rows} rows"
        out_sections.append(header + "\n" + "\n".join(field_lines))
    elif template_cols:
        out_sections.append("## Output columns (target template)\n" + ", ".join(template_cols))

    preview_rows = job.get("data_preview") if isinstance(job.get("data_preview"), list) else []
    preview_cols = (
        job.get("data_preview_column_order")
        if isinstance(job.get("data_preview_column_order"), list)
        else []
    )
    if preview_rows:
        sample = _format_row_sample(preview_rows, preview_cols)
        out_sections.append(
            f"## Combined export preview ({len(preview_rows)} rows shown)\n{sample}"
        )
    else:
        tool_preview = _best_tool_output_preview(job, tool_executions)
        if tool_preview:
            sample_rows = tool_preview.get("sample") if isinstance(tool_preview.get("sample"), list) else []
            col_order = tool_preview.get("column_order") if isinstance(tool_preview.get("column_order"), list) else []
            shape = tool_preview.get("shape") or tool_preview.get("rows")
            sample = _format_row_sample(sample_rows, col_order)
            out_sections.append(f"## Tool output preview (shape={shape})\n{sample}")

    source_lines = _per_source_summary_lines(job, judge_result)
    if source_lines:
        out_sections.append("## Per-source context\n" + "\n".join(source_lines))

    pe = job.get("pipeline_evals") if isinstance(job.get("pipeline_evals"), dict) else {}
    collation = pe.get("collation") if isinstance(pe.get("collation"), dict) else {}
    if collation:
        coll_pass = bool(collation.get("pass", True))
        viol_count = len(collation.get("violations") or [])
        out_sections.append(
            f"## Multi-source collation: {'pass' if coll_pass else 'fail'}"
            + (f" ({viol_count} violation(s))" if viol_count else "")
        )

    judge_bits: List[str] = []
    if isinstance(judge_result, dict):
        if judge_result.get("verdict"):
            judge_bits.append(f"Judge verdict: {judge_result.get('verdict')}.")
        m = judge_result.get("metrics") if isinstance(judge_result.get("metrics"), dict) else {}
        if m.get("total_rows") is not None:
            judge_bits.append(f"Judge saw {m.get('total_rows')} output rows.")
        if m.get("total_cols") is not None:
            judge_bits.append(f"Judge saw {m.get('total_cols')} output columns.")
        if m.get("output_rows") is not None and m.get("total_rows") is None:
            judge_bits.append(f"Output rows: {m.get('output_rows')}.")
        critique = str(judge_result.get("critique") or "").strip()
        if critique:
            judge_bits.append("Judge note: " + critique[:_TASK_CRITIQUE_MAX_CHARS])
    if judge_bits:
        out_sections.append("## LLM judge summary (supplementary)\n" + " ".join(judge_bits))

    if not out_sections:
        out_sections.append("Pipeline completed and produced a normalized table.")

    return task, _truncate_task_output("\n\n".join(out_sections))


def _score_task_completion(
    job: Dict[str, Any],
    judge_result: Optional[Dict[str, Any]],
    schema: Optional[Dict[str, Any]],
    *,
    llm_client: Any,
    use_deepeval: bool,
    tool_executions: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if use_deepeval and llm_client is not None:
        try:
            from sia.evals.deepeval_adapter import score_geval

            task, output = _task_texts(
                job,
                judge_result,
                schema,
                tool_executions=tool_executions,
            )
            result = score_geval(
                name="Task Completion",
                criteria=(
                    "Determine whether the ACTUAL OUTPUT shows the data-transformation task in "
                    "the INPUT was fully accomplished: a flat normalized table matching the target "
                    "template columns, faithful values, and correct per-source context."
                ),
                input_text=task,
                actual_output=output,
                llm_client=llm_client,
                threshold=0.7,
            )
            if result and result.get("score") is not None:
                return {
                    "score": _clamp(result["score"]),
                    "engine": "deepeval",
                    "reason": result.get("reason", ""),
                    "detail": {"metric": "GEval Task Completion", "threshold": result.get("threshold")},
                }
        except Exception as exc:  # pragma: no cover
            logger.warning("DeepEval task completion failed, falling back: %s", exc)

    # Fallback: derive from LLM judge.
    if isinstance(judge_result, dict) and not judge_result.get("error"):
        task_success = 1.0 if judge_result.get("task_success") else 0.0
        fidelity = judge_result.get("fidelity")
        fidelity = float(fidelity) if isinstance(fidelity, (int, float)) else 0.5
        verdict = str(judge_result.get("verdict") or "").upper()
        score = 0.5 * task_success + 0.5 * fidelity
        if verdict == "PASS":
            score = max(score, 0.7)
        elif verdict == "FAIL":
            score = min(score, 0.4)
        return {
            "score": _clamp(score),
            "engine": "judge_fallback",
            "detail": {"task_success": bool(judge_result.get("task_success")), "verdict": verdict},
        }

    # Last resort: overall confidence / export safety.
    pe = job.get("pipeline_evals") if isinstance(job.get("pipeline_evals"), dict) else {}
    gate = pe.get("critical_gate") if isinstance(pe.get("critical_gate"), dict) else {}
    conf = job.get("overall_confidence")
    if isinstance(conf, (int, float)) and conf > 0:
        score = float(conf)
    else:
        score = 0.7 if gate.get("pass", True) else 0.3
    return {"score": _clamp(score), "engine": "heuristic", "detail": {}}


def _strengths_weaknesses(subscores: Dict[str, Dict[str, Any]]) -> tuple[List[str], List[str]]:
    strengths: List[str] = []
    weaknesses: List[str] = []
    for key, row in subscores.items():
        label = SUBSCORE_LABELS.get(key, key)
        score = float(row.get("score") or 0.0)
        if score >= 0.85:
            strengths.append(f"{label} strong ({round(score * 100)}%)")
        elif score < 0.6:
            weaknesses.append(f"{label} weak ({round(score * 100)}%)")
    return strengths, weaknesses


_BLEED_TYPES = {
    "dimension_mismatch",
    "metric_mismatch",
    "context_grounding_critical",
    "memory_scope",
    "context_lineage_missing",
}


def _plan_quality_from_bucket(
    bucket: Dict[str, Any], judge_result: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    plan = bucket.get("plan") if isinstance(bucket.get("plan"), dict) else {}
    metrics = plan.get("metrics") if isinstance(plan.get("metrics"), dict) else {}
    val = metrics.get("final_tool_contract_score")
    if val is None:
        val = metrics.get("tool_contract_score")
    contract = float(val) if isinstance(val, (int, float)) else None

    critical = sum(
        1
        for v in (plan.get("violations") or [])
        if isinstance(v, dict) and str(v.get("severity") or "").lower() == "critical"
    )

    tool_accuracy = None
    if isinstance(judge_result, dict):
        ta = judge_result.get("tool_accuracy")
        if isinstance(ta, (int, float)):
            tool_accuracy = float(ta)

    if contract is not None and tool_accuracy is not None:
        score = 0.7 * contract + 0.3 * tool_accuracy
        engine = "contract+judge"
    elif contract is not None:
        score = contract
        engine = "contract"
    elif tool_accuracy is not None:
        score = tool_accuracy
        engine = "judge"
    else:
        score = 0.7
        engine = "default"

    if critical:
        score = min(score, 0.4)

    # Argument correctness (referenced columns exist) — blend when measured.
    arg_score = metrics.get("argument_correctness_score")
    arg_score = float(arg_score) if isinstance(arg_score, (int, float)) else None
    if arg_score is not None:
        score = 0.8 * score + 0.2 * _clamp(arg_score)
        engine = f"{engine}+args"

    return {
        "score": _clamp(score),
        "engine": engine,
        "detail": {
            "tool_contract_score": contract,
            "argument_correctness_score": arg_score,
            "critical_plan_violations": critical,
        },
    }


def _context_quality_from_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    ctx = bucket.get("context_isolation") if isinstance(bucket.get("context_isolation"), dict) else {}
    passed = bool(ctx.get("pass", True))
    bleed = sum(
        1
        for v in (ctx.get("violations") or [])
        if isinstance(v, dict) and str(v.get("type") or "") in _BLEED_TYPES
    )
    metrics = ctx.get("metrics") if isinstance(ctx.get("metrics"), dict) else {}
    scoped = metrics.get("scoped_local_context") if isinstance(metrics.get("scoped_local_context"), dict) else {}
    max_hop = 0
    for row in scoped.values():
        if isinstance(row, dict):
            try:
                max_hop = max(max_hop, int(row.get("hop") or 0))
            except (TypeError, ValueError):
                pass

    plan = bucket.get("plan") if isinstance(bucket.get("plan"), dict) else {}
    pm = plan.get("metrics") if isinstance(plan.get("metrics"), dict) else {}
    gb = pm.get("context_grounding_bleed_count")
    grounding_bleed = int(gb) if isinstance(gb, (int, float)) else 0

    score = 1.0 if passed else 0.5
    if bleed > 0:
        score = min(score, 0.45)
    if grounding_bleed > 0:
        score = min(score, 0.35)
    if max_hop > 1:
        score -= 0.15

    return {
        "score": _clamp(score),
        "engine": "deterministic",
        "detail": {
            "isolation_pass": passed,
            "bleed_count": bleed,
            "grounding_bleed": grounding_bleed,
            "max_hop": max_hop,
        },
    }


def _step_efficiency_from_bucket(
    bucket: Dict[str, Any], judge_result: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    execu = bucket.get("execution") if isinstance(bucket.get("execution"), dict) else {}
    m = execu.get("metrics") if isinstance(execu.get("metrics"), dict) else {}
    verifier_issues = int(m.get("verifier_issue_count") or 0) if isinstance(m.get("verifier_issue_count"), (int, float)) else 0

    plan = bucket.get("plan") if isinstance(bucket.get("plan"), dict) else {}
    pm = plan.get("metrics") if isinstance(plan.get("metrics"), dict) else {}
    necessity = pm.get("necessity_score")
    necessity = float(necessity) if isinstance(necessity, (int, float)) else None

    structural = _clamp(1.0 - 0.12 * verifier_issues)
    parts = [structural]
    if necessity is not None:
        parts.append(_clamp(necessity))

    # Plan adherence (execution ran what was planned) — an execution-stage signal.
    adherence = m.get("plan_adherence_score")
    adherence = float(adherence) if isinstance(adherence, (int, float)) else None
    if adherence is not None:
        parts.append(_clamp(adherence))

    token_eff = None
    if isinstance(judge_result, dict):
        te = judge_result.get("token_efficiency")
        if isinstance(te, (int, float)) and te > 0:
            token_eff = _clamp(te)
    if token_eff is not None:
        parts.append(token_eff)

    score = sum(parts) / len(parts)
    return {
        "score": _clamp(score),
        "engine": "deterministic",
        "detail": {
            "verifier_issue_count": verifier_issues,
            "necessity_score": necessity,
            "plan_adherence_score": adherence,
            "token_efficiency": token_eff,
        },
    }


def compute_source_quality_scores(
    bucket: Dict[str, Any],
    *,
    judge_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Per-source quality: only the sub-scores that are meaningful per sheet/source.

    Task completion (via DeepEval GEval) and collation are whole-job metrics and
    are intentionally excluded here — the caller surfaces them separately.
    Never raises.
    """
    try:
        if not isinstance(bucket, dict):
            return {}
        subscores: Dict[str, Dict[str, Any]] = {
            "plan_quality": _plan_quality_from_bucket(bucket, judge_result),
            "context_quality": _context_quality_from_bucket(bucket),
            "step_efficiency": _step_efficiency_from_bucket(bucket, judge_result),
        }
        active = {k: SUBSCORE_WEIGHTS[k] for k in subscores}
        wsum = sum(active.values()) or 1.0
        weighted = sum(
            (active[k] / wsum) * float(subscores[k].get("score") or 0.0) for k in subscores
        )
        overall = round(weighted * 100)

        ctx = bucket.get("context_isolation") if isinstance(bucket.get("context_isolation"), dict) else {}
        if not bool(ctx.get("pass", True)):
            overall = min(overall, 55)

        strengths, weaknesses = _strengths_weaknesses(subscores)
        return {
            "overall": overall,
            "band": _band(overall),
            "subscores": subscores,
            "labels": {k: SUBSCORE_LABELS[k] for k in subscores},
            "levels": {k: SUBSCORE_LEVEL.get(k, "source") for k in subscores},
            "strengths": strengths,
            "weaknesses": weaknesses,
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("compute_source_quality_scores failed: %s", exc)
        return {}


def compute_all_source_quality(job: Dict[str, Any]) -> Dict[str, Any]:
    """Map of source_id -> per-source quality score. Never raises."""
    try:
        pe = job.get("pipeline_evals") if isinstance(job.get("pipeline_evals"), dict) else {}
        per = pe.get("per_source") if isinstance(pe.get("per_source"), dict) else {}
        judge = pe.get("judge") if isinstance(pe.get("judge"), dict) else {}
        out: Dict[str, Any] = {}
        for sid, bucket in per.items():
            if not isinstance(bucket, dict):
                continue
            q = compute_source_quality_scores(bucket, judge_result=judge)
            if q:
                out[str(sid)] = q
        return out
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("compute_all_source_quality failed: %s", exc)
        return {}


def _score_step_efficiency_job(
    pe: Dict[str, Any],
    tool_executions: Optional[Sequence[Dict[str, Any]]],
    trace_steps: Optional[Sequence[Dict[str, Any]]],
    judge_result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Job-level step efficiency.

    Rolls up the per-source step efficiency (consistent with plan/context
    quality) so that legitimate per-sheet tool repetition in a multi-source run
    is not mistaken for waste. Falls back to the whole-trace heuristic only when
    no per-source buckets are available (e.g. single-source legacy jobs).
    """
    per = pe.get("per_source") if isinstance(pe.get("per_source"), dict) else {}
    source_scores: List[float] = []
    per_source_detail: Dict[str, Any] = {}
    for sid, bucket in per.items():
        if not isinstance(bucket, dict):
            continue
        r = _step_efficiency_from_bucket(bucket, judge_result)
        s = float(r.get("score") or 0.0)
        source_scores.append(s)
        per_source_detail[str(sid)] = round(s, 3)

    if source_scores:
        score = sum(source_scores) / len(source_scores)
        return {
            "score": _clamp(score),
            "engine": "per_source_avg",
            "detail": {
                "source_count": len(source_scores),
                "per_source": per_source_detail,
            },
        }

    # Fallback: whole-trace heuristic (single-source / no per-source metrics).
    return _score_step_efficiency(tool_executions, trace_steps, judge_result)


def compute_quality_scores(
    job: Dict[str, Any],
    *,
    judge_result: Optional[Dict[str, Any]] = None,
    trace_steps: Optional[Sequence[Dict[str, Any]]] = None,
    tool_executions: Optional[Sequence[Dict[str, Any]]] = None,
    llm_client: Any = None,
    schema: Optional[Dict[str, Any]] = None,
    use_deepeval: bool = True,
) -> Dict[str, Any]:
    """Compute the composite quality score. Never raises."""
    try:
        pe = job.get("pipeline_evals") if isinstance(job.get("pipeline_evals"), dict) else {}

        subscores: Dict[str, Dict[str, Any]] = {
            "task_completion": _score_task_completion(
                job,
                judge_result,
                schema,
                llm_client=llm_client,
                use_deepeval=use_deepeval,
                tool_executions=tool_executions,
            ),
            "plan_quality": _score_plan_quality(pe, judge_result),
            "context_quality": _score_context_quality(job, pe),
            "step_efficiency": _score_step_efficiency_job(pe, tool_executions, trace_steps, judge_result),
        }

        weighted = sum(
            SUBSCORE_WEIGHTS[key] * float(subscores[key].get("score") or 0.0) for key in SUBSCORE_WEIGHTS
        )
        overall = round(weighted * 100)

        gate = pe.get("critical_gate") if isinstance(pe.get("critical_gate"), dict) else {}
        gate_blocked = not bool(gate.get("pass", True))
        if gate_blocked:
            overall = min(overall, 40)

        strengths, weaknesses = _strengths_weaknesses(subscores)

        from sia.evals.deepeval_adapter import deepeval_available

        return {
            "overall": overall,
            "band": _band(overall),
            "gate_blocked": gate_blocked,
            "subscores": subscores,
            "weights": dict(SUBSCORE_WEIGHTS),
            "labels": dict(SUBSCORE_LABELS),
            "levels": dict(SUBSCORE_LEVEL),
            "job_level_only": list(JOB_LEVEL_ONLY),
            "strengths": strengths,
            "weaknesses": weaknesses,
            "deepeval": {
                "available": deepeval_available(),
                "used_for": ["task_completion"] if subscores["task_completion"].get("engine") == "deepeval" else [],
            },
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("compute_quality_scores failed: %s", exc)
        return {
            "overall": 0,
            "band": "poor",
            "gate_blocked": False,
            "subscores": {},
            "error": str(exc),
        }
