"""Structured context briefing for debug UI (curated vs archive)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sia.agent.json_safe import dumps as json_safe_dumps

BUCKET_ORDER: Tuple[str, ...] = ("prompt", "template", "docs", "tools", "memory")

BUCKET_LABELS: Dict[str, str] = {
    "prompt": "Prompt",
    "template": "Template",
    "docs": "Docs & data samples",
    "tools": "Tools catalog",
    "memory": "Memory & job",
}

SECTION_META: Dict[str, Tuple[str, str]] = {
    "task": ("prompt", "Task instructions"),
    "local_context": ("prompt", "Local context (authoritative)"),
    "mappings": ("prompt", "Approved column mappings"),
    "exclusions": ("prompt", "Approved exclusions"),
    "resolved": ("prompt", "Resolved planner decisions"),
    "dup_targets": ("prompt", "Duplicate target mappings"),
    "source_meta": ("prompt", "Source identity"),
    "date_grain": ("prompt", "Date granularity alignment"),
    "date_obs": ("prompt", "Observed date cadence"),
    "guardrail": ("prompt", "Empty-row guardrail"),
    "structure": ("docs", "Structure analysis"),
    "layout": ("docs", "Approved layout scope"),
    "snippets": ("docs", "Context block snippets"),
    "evidence": ("docs", "Supplementary evidence"),
    "template": ("template", "Target template contract"),
    "relationships": ("memory", "Source graph"),
    "propose_rels": ("memory", "Cross-source relationship hint"),
    "rules": ("memory", "Business rules"),
    "value_scale": ("memory", "Value scale notes"),
    "notes": ("memory", "User notes"),
    "pipeline_catalog": ("tools", "Pipeline tool catalog (system prompt)"),
}


def section_meta(section_id: str) -> Tuple[str, str]:
    return SECTION_META.get(str(section_id), ("prompt", str(section_id)))


def build_tools_catalog_briefing(system_prompt: str = "") -> Dict[str, Any]:
    """Tools bucket: pipeline catalog appended to system prompt."""
    from sia.tools.pipeline_catalog import PIPELINE_CATALOG_MARKDOWN_HEADER, format_catalog_markdown

    catalog_body = PIPELINE_CATALOG_MARKDOWN_HEADER + format_catalog_markdown()
    bucket, title = SECTION_META["pipeline_catalog"]
    return {
        "id": "pipeline_catalog",
        "bucket": bucket,
        "title": title,
        "delivery": "system_prompt",
        "in_curated_prompt": True,
        "chars": len(catalog_body),
        "preview": catalog_body[:500],
        "body": catalog_body,
        "truncated": len(catalog_body) > 500,
    }


def serialize_section_row(
    *,
    section_id: str,
    body: str,
    drop_priority: int,
    in_curated_prompt: bool,
    delivery: str = "user_prompt",
    truncated: bool = False,
) -> Dict[str, Any]:
    bucket, title = section_meta(section_id)
    text = str(body or "")
    return {
        "id": section_id,
        "bucket": bucket,
        "title": title,
        "delivery": delivery,
        "in_curated_prompt": bool(in_curated_prompt),
        "drop_priority": int(drop_priority),
        "chars": len(text),
        "preview": text[:480],
        "body": text,
        "truncated": truncated or len(text) > 480,
    }


def group_sections_by_bucket(sections: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    grouped: Dict[str, List[Dict[str, Any]]] = {k: [] for k in BUCKET_ORDER}
    for row in sections:
        bucket = str(row.get("bucket") or "prompt")
        if bucket not in grouped:
            grouped[bucket] = []
        grouped[bucket].append(row)
    out: Dict[str, Any] = {}
    for bucket in BUCKET_ORDER:
        rows = grouped.get(bucket) or []
        if not rows:
            continue
        included = [r for r in rows if r.get("in_curated_prompt")]
        out[bucket] = {
            "label": BUCKET_LABELS.get(bucket, bucket),
            "section_count": len(rows),
            "included_count": len(included),
            "chars_included": sum(int(r.get("chars") or 0) for r in included),
            "sections": rows,
        }
    return out


def build_archive_context_briefing(context_packet: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Archive ContextPacket grouped for debug — not what the LLM user prompt contains."""
    cp = dict(context_packet or {})
    if not cp:
        return {"label": "Archive (ContextPacket)", "buckets": {}, "note": "No context packet on this step."}

    from sia.debug.state_snapshot import compact_context_packet

    compact = compact_context_packet(cp)
    snippets = list(cp.get("context_block_snippets") or [])
    ic = cp.get("interpreted_context") if isinstance(cp.get("interpreted_context"), dict) else {}
    mappings = list(cp.get("approved_mappings") or [])[:12]

    buckets: Dict[str, Any] = {}

    buckets["docs"] = {
        "label": BUCKET_LABELS["docs"],
        "items": [
            {
                "id": "context_block_snippets_full",
                "title": "Context block snippets (full packet)",
                "chars": len(json_safe_dumps(snippets)),
                "preview": json_safe_dumps(snippets[:3])[:480],
                "body": json_safe_dumps(snippets, indent=2),
            },
            {
                "id": "interpreted_context_full",
                "title": "Interpreted context (full packet)",
                "chars": len(json_safe_dumps(ic)),
                "preview": json_safe_dumps(
                    {
                        "fields": ic.get("fields"),
                        "scoped_fields": list((ic.get("scoped_fields") or {}).keys())[:8],
                    }
                )[:480],
                "body": json_safe_dumps(ic, indent=2),
            },
        ],
    }

    buckets["memory"] = {
        "label": BUCKET_LABELS["memory"],
        "items": [
            {
                "id": "file_relationships",
                "title": "File relationships",
                "body": json_safe_dumps(list(cp.get("file_relationships") or [])[:12], indent=2),
                "preview": f"{len(cp.get('file_relationships') or [])} relationship row(s)",
            },
            {
                "id": "job_run_ledger",
                "title": "Job run ledger summary",
                "body": json_safe_dumps(cp.get("job_run_ledger_summary") or {}, indent=2),
                "preview": json_safe_dumps(cp.get("job_run_ledger_summary") or {})[:200],
            },
            {
                "id": "available_sources",
                "title": "Available sources registry",
                "body": json_safe_dumps(list(cp.get("available_sources") or [])[:8], indent=2),
                "preview": f"{len(cp.get('available_sources') or [])} source(s) in registry",
            },
        ],
    }

    buckets["prompt"] = {
        "label": "Setup (archive)",
        "items": [
            {
                "id": "approved_mappings_sample",
                "title": "Approved mappings (sample)",
                "body": json_safe_dumps(mappings, indent=2),
                "preview": ", ".join(
                    f"{m.get('source_column')}→{m.get('target_column')}"
                    for m in mappings[:6]
                    if isinstance(m, dict)
                ),
            },
            {
                "id": "planning_summary",
                "title": "Planning summary (canonical view)",
                "body": json_safe_dumps(cp.get("planning_summary") or {}, indent=2),
                "preview": json_safe_dumps((cp.get("planning_summary") or {}).get("source_summary") or {})[:200],
            },
        ],
    }

    return {
        "label": "Archive — full ContextPacket (audit / debug only)",
        "note": "The curated user prompt is a filtered view. This archive is unchanged for evals and provenance.",
        "fingerprint": cp.get("context_fingerprint"),
        "compact_snapshot": compact,
        "buckets": buckets,
    }


def assemble_briefing_payload(
    *,
    user_prompt: str,
    section_rows: Sequence[Dict[str, Any]],
    included_ids: Sequence[str],
    dropped_ids: Sequence[str],
    max_chars: int,
    context_packet: Optional[Dict[str, Any]],
    system_prompt: str = "",
    tools_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    all_rows = list(section_rows)
    if tools_row:
        all_rows.append(tools_row)
    buckets = group_sections_by_bucket(all_rows)
    curated_chars = len(user_prompt or "")
    return {
        "schema_version": 1,
        "max_chars": int(max_chars),
        "curated_user_prompt_chars": curated_chars,
        "included_section_ids": list(included_ids),
        "dropped_section_ids": list(dropped_ids),
        "sections": all_rows,
        "buckets": buckets,
        "archive": build_archive_context_briefing(context_packet),
        "system_prompt_chars": len(system_prompt or ""),
        "tools_catalog_chars": int((tools_row or {}).get("chars") or 0),
    }
