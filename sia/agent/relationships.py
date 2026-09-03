from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from sia.agent.target_template_utils import template_union_grain_columns


PLATFORM_HINTS = {
    "meta": {"meta", "facebook", "instagram"},
    "youtube": {"youtube", "yt"},
    "search": {"search", "google ads", "google", "bing"},
    "social": {"social", "meta", "facebook", "instagram", "linkedin", "tiktok"},
    "reference": {"lookup", "reference", "dictionary", "mapping"},
}

_JOIN_KEY_FALLBACK_ORDER = (
    "date",
    "market",
    "brand",
    "channel",
    "publisher_name",
    "publisher",
    "campaign_name",
)


def enrich_source_summaries_for_relationships(
    source_summaries: Sequence[Dict[str, Any]],
    *,
    target_template: Optional[Dict[str, Any]] = None,
    frames_by_source: Optional[Dict[str, pd.DataFrame]] = None,
) -> List[Dict[str, Any]]:
    """Attach template grain + per-source output columns for join-key inference."""
    grain = template_union_grain_columns(target_template) if target_template else []
    enriched: List[Dict[str, Any]] = []
    for summary in source_summaries or []:
        if not isinstance(summary, dict):
            continue
        out = dict(summary)
        if grain and not out.get("template_grain_columns"):
            out["template_grain_columns"] = list(grain)
        sid = str(out.get("source_id") or "").strip()
        if frames_by_source and sid:
            frame = frames_by_source.get(sid)
            if frame is not None and hasattr(frame, "columns"):
                out["output_columns"] = [str(c) for c in frame.columns]
        enriched.append(out)
    return enriched


def refresh_union_join_keys_in_proposals(
    proposals: Sequence[Dict[str, Any]],
    source_summaries: Sequence[Dict[str, Any]],
    *,
    target_template: Optional[Dict[str, Any]] = None,
    frames_by_source: Optional[Dict[str, pd.DataFrame]] = None,
) -> List[Dict[str, Any]]:
    """Recompute union ``join_keys`` when post-execution frames include derived columns."""
    enriched = enrich_source_summaries_for_relationships(
        source_summaries,
        target_template=target_template,
        frames_by_source=frames_by_source,
    )
    main_sources = [
        item for item in enriched
        if item.get("contains_main_data", True) and not item.get("contains_reference_data")
    ]
    fresh_keys = _common_join_keys(main_sources) if len(main_sources) > 1 else []
    refreshed: List[Dict[str, Any]] = []
    for proposal in proposals or []:
        if not isinstance(proposal, dict):
            continue
        record = dict(proposal)
        if str(record.get("relationship_kind") or "").lower() == "union" and fresh_keys:
            record["join_keys"] = list(fresh_keys)
        refreshed.append(record)
    return refreshed


def propose_file_relationships(
    source_summaries: Sequence[Dict[str, Any]],
    *,
    target_template: Optional[Dict[str, Any]] = None,
    frames_by_source: Optional[Dict[str, pd.DataFrame]] = None,
) -> List[Dict[str, Any]]:
    summaries = enrich_source_summaries_for_relationships(
        source_summaries,
        target_template=target_template,
        frames_by_source=frames_by_source,
    )
    summaries = [item for item in summaries if isinstance(item, dict) and item.get("source_id")]
    if len(summaries) < 2:
        return []

    proposals: List[Dict[str, Any]] = []
    main_sources = [item for item in summaries if item.get("contains_main_data", True) and not item.get("contains_reference_data")]
    reference_sources = [item for item in summaries if item.get("contains_reference_data")]

    if len(main_sources) > 1:
        union_group = [item["source_id"] for item in main_sources]
        shared_keys = _common_join_keys(main_sources)
        proposals.append({
            "relationship_id": f"relationship_union_{len(proposals) + 1}",
            "relationship_kind": "union",
            "source_ids": union_group,
            "join_keys": shared_keys,
            "confidence": round(_union_confidence(main_sources), 2),
            "recommended_action": "approve",
            "status": "proposed",
            "summary": _build_union_summary(main_sources),
            "rationale": _build_union_rationale(main_sources, shared_keys),
        })

    for source in reference_sources:
        candidates = [item for item in main_sources if item["source_id"] != source["source_id"]]
        if not candidates:
            continue
        best = max(candidates, key=lambda item: _join_confidence(source, item))
        join_keys = _common_join_keys([source, best])
        proposals.append({
            "relationship_id": f"relationship_join_{len(proposals) + 1}",
            "relationship_kind": "lookup/reference",
            "source_ids": [best["source_id"], source["source_id"]],
            "join_keys": join_keys,
            "confidence": round(_join_confidence(source, best), 2),
            "recommended_action": "approve" if join_keys else "modify",
            "status": "proposed",
            "summary": f"{best.get('file_name')} + {source.get('file_name')} -> reference join",
            "rationale": _build_join_rationale(source, best, join_keys),
        })

    covered = {source_id for proposal in proposals for source_id in proposal.get("source_ids", [])}
    for source in summaries:
        if source["source_id"] not in covered:
            proposals.append({
                "relationship_id": f"relationship_independent_{len(proposals) + 1}",
                "relationship_kind": "independent",
                "source_ids": [source["source_id"]],
                "join_keys": [],
                "confidence": 0.55,
                "recommended_action": "approve",
                "status": "proposed",
                "summary": f"{source.get('file_name')} -> independent source",
                "rationale": "No strong join or union signal was detected from metadata and mapped target coverage.",
            })

    return proposals


def apply_relationship_decisions(proposals: Sequence[Dict[str, Any]], overrides: Sequence[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    override_map = {
        item.get("relationship_id"): item
        for item in (overrides or [])
        if isinstance(item, dict) and item.get("relationship_id")
    }
    applied: List[Dict[str, Any]] = []
    for proposal in proposals:
        record = dict(proposal)
        override = override_map.get(record.get("relationship_id"))
        if override:
            record.update({
                "relationship_kind": override.get("relationship_kind") or record.get("relationship_kind"),
                "join_keys": list(override.get("join_keys") or record.get("join_keys") or []),
                "status": override.get("status") or "approved",
                "rationale": override.get("rationale") or override.get("analyst_notes") or record.get("rationale"),
            })
            if override.get("duplicate_merge_mode") is not None:
                dm = str(override.get("duplicate_merge_mode") or "").strip()
                if dm:
                    record["duplicate_merge_mode"] = dm
            if override.get("duplicate_key_group_decisions") is not None:
                ddc = override.get("duplicate_key_group_decisions")
                if isinstance(ddc, dict):
                    record["duplicate_key_group_decisions"] = dict(ddc)
        else:
            record["status"] = record.get("status") or "approved"
        applied.append(record)
    return applied


def _normalize_frames_keys(frames_by_source: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    def _sid_key(x: Any) -> str:
        return str(x).strip() if x is not None else ""

    return {_sid_key(k): v for k, v in frames_by_source.items() if _sid_key(k)}


def _union_stack_source_ids(
    norm_frames: Dict[str, pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
) -> List[str]:
    """Source ids that participate in a union edge, in stable encounter order (matches baseline collate)."""
    ordered: List[str] = []
    for relationship in relationships or []:
        source_ids = [
            str(sid).strip()
            for sid in relationship.get("source_ids", [])
            if str(sid).strip() in norm_frames
        ]
        if not source_ids:
            continue
        kind = str(relationship.get("relationship_kind") or "independent").lower()
        if kind != "union":
            continue
        for sid in source_ids:
            if sid not in ordered:
                ordered.append(sid)
    return ordered


def _union_stack_column_intersection(
    norm_frames: Dict[str, pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
) -> List[str]:
    """Columns shared by every frame in the union stack — used to dedupe cross-file duplicates when join_keys are empty."""
    ids = _union_stack_source_ids(norm_frames, relationships)
    if len(ids) < 2:
        return []
    inter: Optional[set] = None
    for sid in ids:
        cols = set(norm_frames[sid].columns)
        inter = cols if inter is None else inter & cols
    if not inter:
        return []
    return sorted(inter)


def _collate_relationship_groups(
    norm_frames: Dict[str, pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """Partition source ids the same way as :func:`_collate_frames_baseline`."""

    def _sid_key(x: Any) -> str:
        return str(x).strip() if x is not None else ""

    grouped: Dict[str, List[str]] = defaultdict(list)
    independent: List[str] = []
    for relationship in relationships or []:
        source_ids = [
            _sid_key(source_id)
            for source_id in relationship.get("source_ids", [])
            if _sid_key(source_id) in norm_frames
        ]
        if not source_ids:
            continue
        kind = str(relationship.get("relationship_kind") or "independent").lower()
        if kind == "union":
            grouped["union"].extend(source_ids)
        elif kind in {"lookup/reference", "lookup", "reference", "join"}:
            grouped["join"].extend(source_ids)
        else:
            independent.extend(source_ids)

    union_ids = list(dict.fromkeys(grouped.get("union", [])))
    join_ids = list(dict.fromkeys(grouped.get("join", [])))
    independent_ids = list(dict.fromkeys(independent))
    consumed = set(union_ids + join_ids + independent_ids)
    remaining_ids = [source_id for source_id in norm_frames if source_id not in consumed]
    return union_ids, join_ids, independent_ids, remaining_ids


def _collate_frames_baseline(
    norm_frames: Dict[str, pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
) -> pd.DataFrame:
    if not norm_frames:
        return pd.DataFrame()

    union_ids, join_ids, independent_ids, remaining_ids = _collate_relationship_groups(
        norm_frames, relationships
    )

    frames: List[pd.DataFrame] = []
    if union_ids:
        frames.append(_concat_frames([norm_frames[source_id] for source_id in union_ids]))
    join_present = [source_id for source_id in join_ids if source_id in norm_frames]
    if len(join_present) >= 2:
        frames.append(_join_frames([norm_frames[source_id] for source_id in join_present]))
    frames.extend(norm_frames[source_id] for source_id in independent_ids)
    frames.extend(norm_frames[source_id] for source_id in remaining_ids)

    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0]
    return _concat_frames(frames)


def baseline_collate_stack_layout(
    frames_by_source: Dict[str, pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
) -> Tuple[List[int], List[Dict[str, Any]]]:
    """Row breaks and per-segment metadata matching :func:`_collate_frames_baseline` order.

    Each segment entry includes ``source_id``, ``segment_kind`` (``union_member``,
    ``join``, ``independent``, ``remaining``), and optional ``label_suffix`` for UI.
    """
    norm_frames = _normalize_frames_keys(frames_by_source)
    if not norm_frames:
        return [], []

    union_ids, join_ids, independent_ids, remaining_ids = _collate_relationship_groups(
        norm_frames, relationships
    )
    segments: List[Tuple[str, int, str, str]] = []

    for source_id in union_ids:
        frame = norm_frames.get(source_id)
        row_count = int(len(frame)) if frame is not None and not frame.empty else 0
        if row_count > 0:
            segments.append((source_id, row_count, "union_member", ""))

    join_present = [source_id for source_id in join_ids if source_id in norm_frames]
    if len(join_present) >= 2:
        joined = _join_frames([norm_frames[source_id] for source_id in join_present])
        row_count = int(len(joined)) if joined is not None and not joined.empty else 0
        if row_count > 0:
            segments.append((join_present[0], row_count, "join", " (join)"))

    for source_id in independent_ids:
        frame = norm_frames.get(source_id)
        row_count = int(len(frame)) if frame is not None and not frame.empty else 0
        if row_count > 0:
            segments.append((source_id, row_count, "independent", ""))

    for source_id in remaining_ids:
        frame = norm_frames.get(source_id)
        row_count = int(len(frame)) if frame is not None and not frame.empty else 0
        if row_count > 0:
            segments.append((source_id, row_count, "remaining", ""))

    breaks: List[int] = []
    meta: List[Dict[str, Any]] = []
    offset = 0
    for source_id, row_count, kind, suffix in segments:
        meta.append(
            {
                "source_id": source_id,
                "segment_kind": kind,
                "label_suffix": suffix,
            }
        )
        offset += row_count
        if offset > 0:
            breaks.append(offset)
    if breaks:
        breaks.pop()
    return breaks, meta


def relationships_applicable_to_frames(
    relationships: Sequence[Dict[str, Any]],
    norm_frames: Dict[str, pd.DataFrame],
) -> List[Dict[str, Any]]:
    """Drop relationship edges whose ``source_ids`` are not all present in ``norm_frames``."""
    applicable: List[Dict[str, Any]] = []
    for relationship in relationships or []:
        if not isinstance(relationship, dict):
            continue
        source_ids = [str(sid).strip() for sid in (relationship.get("source_ids") or []) if str(sid).strip()]
        if not source_ids:
            continue
        if all(sid in norm_frames for sid in source_ids):
            applicable.append(relationship)
    return applicable


def collate_frames_detailed(
    frames_by_source: Dict[str, pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
    *,
    target_template: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    """Like :func:`collate_frames` but also returns trace steps and optional hierarchical debug events.

    For two or more sources, the third element is the collation-only observer event list
    (same shape as :meth:`LLMObserver.get_hierarchical_events`) including CSV snapshots.
    Single-source collation returns ``None`` for the third element.
    """
    norm_frames = _normalize_frames_keys(frames_by_source)
    if len(norm_frames) < 2:
        df = _collate_frames_baseline(norm_frames, relationships)
        meta = [
            {
                "step": "collate_baseline",
                "rows": int(len(df)) if df is not None else 0,
                "cols": int(len(df.columns)) if df is not None and not df.empty else 0,
                "sources": list(norm_frames.keys()),
            }
        ]
        return df, meta, None
    from sia.agent.parent_collation_graph import run_parent_collation_graph

    df, steps, events = run_parent_collation_graph(
        norm_frames, list(relationships or []), target_template=target_template
    )
    return df, steps, events


def collate_frames(
    frames_by_source: Dict[str, pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
    *,
    target_template: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    df, _, _ = collate_frames_detailed(frames_by_source, relationships, target_template=target_template)
    return df


def union_stack_break_row_indices(
    frames_by_source: Dict[str, pd.DataFrame],
    relationships: Sequence[Dict[str, Any]],
) -> List[int]:
    """0-based row indices where a new union member's rows begin (after the first member).

    Matches the union stacking order used by :func:`collate_frames`. Empty when there is
    not a multi-source union, so previews can draw a separator between stacked files.
    """
    if not frames_by_source:
        return []

    norm_frames = _normalize_frames_keys(frames_by_source)

    def _sid_key(x: Any) -> str:
        return str(x).strip() if x is not None else ""

    grouped: Dict[str, List[str]] = defaultdict(list)
    independent: List[str] = []
    for relationship in relationships or []:
        source_ids = [
            _sid_key(sid)
            for sid in relationship.get("source_ids", [])
            if _sid_key(sid) in norm_frames
        ]
        if not source_ids:
            continue
        kind = str(relationship.get("relationship_kind") or "independent").lower()
        if kind == "union":
            grouped["union"].extend(source_ids)
        elif kind in {"lookup/reference", "lookup", "reference", "join"}:
            grouped["join"].extend(source_ids)
        else:
            independent.extend(source_ids)
    union_ids = list(dict.fromkeys(grouped.get("union", [])))
    if len(union_ids) < 2:
        return []
    breaks: List[int] = []
    offset = 0
    for source_id in union_ids[:-1]:
        frame = norm_frames.get(source_id)
        n = len(frame) if frame is not None and not frame.empty else 0
        offset += n
        breaks.append(offset)
    return breaks


def _concat_frames(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    valid_frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid_frames:
        return pd.DataFrame()
    return pd.concat(valid_frames, ignore_index=True, sort=False)


def _join_frames(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    valid_frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid_frames:
        return pd.DataFrame()
    result = valid_frames[0]
    for frame in valid_frames[1:]:
        join_keys = [column for column in ("date", "market", "brand", "channel", "publisher_name") if column in result.columns and column in frame.columns]
        if not join_keys:
            result = _concat_frames([result, frame])
            continue
        frame_columns = [column for column in frame.columns if column not in join_keys]
        result = result.merge(frame[join_keys + frame_columns], how="left", on=join_keys)
    return result


def _resolve_column_on_frame(name: str, columns: Sequence[str]) -> Optional[str]:
    token = str(name or "").strip()
    if not token:
        return None
    cols = [str(c) for c in columns]
    if token in cols:
        return token
    lower = {str(c).lower(): str(c) for c in cols}
    return lower.get(token.lower())


def _join_key_pool_for_source(source: Dict[str, Any]) -> set[str]:
    pool: set[str] = set()
    for item in source.get("mapped_targets") or []:
        token = str(item or "").strip()
        if token:
            pool.add(token)
    for item in source.get("uid") or []:
        token = str(item or "").strip()
        if token:
            pool.add(token)

    output_cols = [str(c) for c in (source.get("output_columns") or [])]
    grain = [str(c) for c in (source.get("template_grain_columns") or [])]
    for col in grain:
        resolved = _resolve_column_on_frame(col, output_cols)
        if resolved:
            pool.add(resolved)
    return pool


def _ordered_join_keys(common_lower: set[str], source_group: Sequence[Dict[str, Any]]) -> List[str]:
    if not common_lower:
        return []

    grain: List[str] = []
    for source in source_group:
        cols = source.get("template_grain_columns")
        if cols:
            grain = [str(c) for c in cols]
            break

    order_tokens: List[str] = []
    seen: set[str] = set()
    for col in list(grain) + list(_JOIN_KEY_FALLBACK_ORDER):
        key = str(col).lower()
        if key in common_lower and key not in seen:
            order_tokens.append(key)
            seen.add(key)

    reference_cols: List[str] = []
    for source in source_group:
        for col in source.get("output_columns") or source.get("mapped_targets") or []:
            reference_cols.append(str(col))
        if reference_cols:
            break

    canonical: List[str] = []
    seen_names: set[str] = set()
    for key in order_tokens:
        resolved = _resolve_column_on_frame(key, reference_cols)
        if not resolved:
            continue
        norm = resolved.lower()
        if norm not in common_lower or norm in seen_names:
            continue
        canonical.append(resolved)
        seen_names.add(norm)
    return canonical


def _common_join_keys(source_group: Sequence[Dict[str, Any]]) -> List[str]:
    pools = [_join_key_pool_for_source(source) for source in source_group]
    if not pools:
        return []

    common_lower: Optional[set[str]] = None
    for pool in pools:
        lower_set = {str(c).lower() for c in pool}
        common_lower = lower_set if common_lower is None else common_lower & lower_set
    if not common_lower:
        return []

    return _ordered_join_keys(common_lower, source_group)


def _union_confidence(source_group: Sequence[Dict[str, Any]]) -> float:
    if len(source_group) < 2:
        return 0.0
    uid_overlap = len(_common_join_keys(source_group))
    variable_types = {str(item.get("variable_type") or "").lower() for item in source_group if item.get("variable_type")}
    granularity = {str(item.get("date_granularity") or "").lower() for item in source_group if item.get("date_granularity")}
    return min(0.95, 0.55 + (0.08 * uid_overlap) + (0.12 if len(variable_types) == 1 else 0.0) + (0.08 if len(granularity) <= 1 else 0.0))


def _join_confidence(reference_source: Dict[str, Any], main_source: Dict[str, Any]) -> float:
    join_keys = _common_join_keys([reference_source, main_source])
    mapped_reference = set(reference_source.get("mapped_targets") or [])
    mapped_main = set(main_source.get("mapped_targets") or [])
    complementary = len((mapped_reference ^ mapped_main))
    return min(0.9, 0.4 + (0.1 * len(join_keys)) + (0.05 * min(complementary, 4)))


def _build_union_summary(source_group: Sequence[Dict[str, Any]]) -> str:
    names = [source.get("file_name") or source.get("sheet_name") or source.get("source_id") for source in source_group]
    return f"{' + '.join(names)} -> union into paid media output"


def _build_union_rationale(source_group: Sequence[Dict[str, Any]], shared_keys: Sequence[str]) -> str:
    names = [source.get("file_name") or source.get("sheet_name") or source.get("source_id") for source in source_group]
    variable_types = sorted({str(source.get("variable_type") or "unknown") for source in source_group})
    return (
        f"These sources look like parallel main-data feeds ({', '.join(names)}) with compatible variable types "
        f"({', '.join(variable_types)}). Shared grain signals: {', '.join(shared_keys) if shared_keys else 'limited'}."
    )


def _build_join_rationale(reference_source: Dict[str, Any], main_source: Dict[str, Any], join_keys: Sequence[str]) -> str:
    ref_name = reference_source.get("file_name") or reference_source.get("source_id")
    main_name = main_source.get("file_name") or main_source.get("source_id")
    return (
        f"{ref_name} looks more like reference/context data while {main_name} looks like main fact data. "
        f"Suggested join keys: {', '.join(join_keys) if join_keys else 'analyst should confirm keys'}."
    )
