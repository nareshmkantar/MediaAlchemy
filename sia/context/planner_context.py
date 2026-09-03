"""Sanitize context for planner prompts — strip cross-sheet dimension bleed."""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Set

from sia.integrity.context_isolation import SOURCE_LOCAL_DIMENSION_COLUMNS

_PEER_DIMENSION_KEYS = frozenset(SOURCE_LOCAL_DIMENSION_COLUMNS) | frozenset(
    {
        "modeling_period_start",
        "modeling_period_end",
        "aggregation_logic",
    }
)

_STRUCTURAL_SUMMARY_KEYS = frozenset(
    {
        "source_id",
        "file_id",
        "file_name",
        "sheet_name",
        "source_type",
        "variable_type",
        "uid",
        "date_granularity",
        "contains_main_data",
        "contains_reference_data",
        "mapped_targets",
        "template_grain_columns",
        "kept_source_columns",
        "unresolved_mandatory_targets",
    }
)

_APPROVED_REL_STATUSES = frozenset({"approved", "active"})


def relationship_allows_enrichment(
    file_relationships: Optional[List[Dict[str, Any]]],
    active_source_id: str,
) -> bool:
    """True when an approved union/join involves the active source."""
    sid = str(active_source_id or "").strip()
    if not sid:
        return False
    for row in file_relationships or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().lower() not in _APPROVED_REL_STATUSES:
            continue
        left = str(row.get("left_source_id") or row.get("source_id_a") or row.get("source_a") or "").strip()
        right = str(row.get("right_source_id") or row.get("source_id_b") or row.get("source_b") or "").strip()
        members = {x for x in (left, right) if x}
        if sid in members and len(members) >= 2:
            return True
        sources = row.get("source_ids") or row.get("sources") or []
        if isinstance(sources, list) and sid in {str(s) for s in sources}:
            return True
    return False


def sanitize_peer_source_summary(
    summary: Dict[str, Any],
    *,
    is_active: bool,
    allows_enrichment: bool,
) -> Dict[str, Any]:
    """Peer summaries carry structural hints only unless enrichment is approved."""
    if is_active or allows_enrichment:
        return dict(summary)
    row = {k: v for k, v in dict(summary).items() if k in _STRUCTURAL_SUMMARY_KEYS}
    for dim in _PEER_DIMENSION_KEYS:
        row.pop(dim, None)
    return row


def sanitize_source_graph_view(
    graph: Optional[Dict[str, Any]],
    active_source_id: str,
    *,
    allows_enrichment: bool,
) -> Dict[str, Any]:
    """Strip dimension literals from peer nodes in the source graph preview."""
    if not isinstance(graph, dict):
        return {}
    sid = str(active_source_id or "").strip()
    out = copy.deepcopy(graph)
    nodes = []
    for node in out.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        row = dict(node)
        node_sid = str(row.get("source_id") or "").strip()
        if node_sid and node_sid != sid and not allows_enrichment:
            for dim in _PEER_DIMENSION_KEYS:
                row.pop(dim, None)
            if isinstance(row.get("interpreted_fields"), dict):
                row["interpreted_fields"] = {
                    k: v
                    for k, v in row["interpreted_fields"].items()
                    if k not in _PEER_DIMENSION_KEYS
                }
        nodes.append(row)
    out["nodes"] = nodes
    return out


def filter_interpreted_context_for_planner(
    interpreted_context: Optional[Dict[str, Any]],
    active_source_id: str,
    *,
    allows_enrichment: bool = False,
) -> Dict[str, Any]:
    """Keep only active-source scoped fields; drop foreign cross-sheet literals."""
    ic = dict(interpreted_context or {})
    sid = str(active_source_id or "").strip()
    scoped = dict(ic.get("scoped_fields") or {}) if isinstance(ic.get("scoped_fields"), dict) else {}
    filtered_scoped: Dict[str, Any] = {}
    flat_fields: Dict[str, str] = {}

    if scoped:
        for name, row in scoped.items():
            if not isinstance(row, dict):
                continue
            field_sid = str(row.get("source_id") or "").strip()
            scope = str(row.get("scope") or "").strip().lower()
            if scope == "cross_sheet" and not allows_enrichment:
                continue
            if field_sid and sid and field_sid != sid and scope != "workbook":
                continue
            filtered_scoped[name] = row
            val = str(row.get("value") or "").strip()
            if val:
                flat_fields[name] = val
    else:
        flat_fields = {
            str(k): str(v).strip()
            for k, v in dict(ic.get("fields") or {}).items()
            if str(v or "").strip()
        }

    ic["scoped_fields"] = filtered_scoped
    ic["fields"] = flat_fields
    evidence = [e for e in (ic.get("evidence") or []) if isinstance(e, dict)]
    if sid:
        evidence = [
            e
            for e in evidence
            if not str(e.get("source_id") or "").strip() or str(e.get("source_id")) == sid
        ]
    ic["evidence"] = evidence[:24]
    return ic


def sanitize_available_source_summaries(
    summaries: Optional[List[Dict[str, Any]]],
    active_source_id: str,
    *,
    allows_enrichment: bool = False,
) -> List[Dict[str, Any]]:
    sid = str(active_source_id or "").strip()
    out: List[Dict[str, Any]] = []
    for row in summaries or []:
        if not isinstance(row, dict):
            continue
        row_sid = str(row.get("source_id") or "").strip()
        out.append(
            sanitize_peer_source_summary(
                row,
                is_active=bool(sid and row_sid == sid),
                allows_enrichment=allows_enrichment,
            )
        )
    return out


def apply_boundary_sanitization_to_view(
    view: Dict[str, Any],
    context_packet: Optional[Dict[str, Any]],
    *,
    active_source_id: str = "",
) -> Dict[str, Any]:
    """Apply Phase 3 boundary rules to a canonical planning view."""
    cp = dict(context_packet or {})
    sid = str(
        active_source_id
        or (cp.get("lineage") or {}).get("source_id")
        or (cp.get("source_metadata") or {}).get("source_id")
        or ""
    ).strip()
    rels = list(cp.get("file_relationships") or [])
    allows = relationship_allows_enrichment(rels, sid)

    out = dict(view)
    rel_ctx = dict(out.get("relationship_context") or {})
    rel_ctx["allows_enrichment"] = allows
    rel_ctx.setdefault("approved", rels)
    rel_ctx.setdefault("count", len(rels))
    out["relationship_context"] = rel_ctx

    out["interpreted_context"] = filter_interpreted_context_for_planner(
        out.get("interpreted_context") or cp.get("interpreted_context"),
        sid,
        allows_enrichment=allows,
    )

    source_summary = dict(out.get("source_summary") or {})
    if not allows:
        for dim in _PEER_DIMENSION_KEYS:
            if dim in source_summary and sid:
                pass
    out["source_summary"] = source_summary

    out["_sanitized_available_source_summaries"] = sanitize_available_source_summaries(
        cp.get("available_source_summaries"),
        sid,
        allows_enrichment=allows,
    )
    out["_sanitized_source_graph_view"] = sanitize_source_graph_view(
        cp.get("source_graph_view"),
        sid,
        allows_enrichment=allows,
    )
    return out


def prepare_planner_context_sections(
    context_packet: Optional[Dict[str, Any]],
    planning_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return planner-safe interpreted context and peer summaries."""
    from sia.agent.context_packet import build_canonical_planning_view

    cp = dict(context_packet or {})
    view = dict(planning_summary or build_canonical_planning_view(cp))
    sid = str((cp.get("lineage") or {}).get("source_id") or "").strip()
    sanitized = apply_boundary_sanitization_to_view(view, cp, active_source_id=sid)
    return {
        "interpreted_context": sanitized.get("interpreted_context") or {},
        "available_source_summaries": sanitized.get("_sanitized_available_source_summaries") or [],
        "source_graph_view": sanitized.get("_sanitized_source_graph_view") or {},
        "relationship_context": sanitized.get("relationship_context") or {},
    }
