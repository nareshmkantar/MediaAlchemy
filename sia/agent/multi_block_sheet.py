"""Process multiple main-data blocks on one sheet like separate sources, then collate."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sia.agent.scoped_source import build_scoped_source

logger = logging.getLogger(__name__)

BLOCK_VIRTUAL_SEP = "::block::"


def block_virtual_source_id(parent_source_id: str, block: Dict[str, Any]) -> str:
    """Stable virtual source id for one demarcated main block."""
    parent = str(parent_source_id or "").strip()
    bid = str(block.get("id") or block.get("block_id") or "").strip()
    if not bid:
        coords = block.get("coordinates", block) if isinstance(block, dict) else {}
        bid = (
            f"{coords.get('start_col', 0)}_{coords.get('end_col', 0)}_"
            f"{coords.get('start_row', 0)}_{coords.get('end_row', 0)}"
        )
    return f"{parent}{BLOCK_VIRTUAL_SEP}{bid}"


def parse_block_virtual_source_id(source_id: str) -> Optional[Tuple[str, str]]:
    """Return (parent_source_id, block_id) when ``source_id`` is a block virtual id."""
    text = str(source_id or "").strip()
    if BLOCK_VIRTUAL_SEP not in text:
        return None
    parent, block_id = text.split(BLOCK_VIRTUAL_SEP, 1)
    parent = parent.strip()
    block_id = block_id.strip()
    if not parent or not block_id:
        return None
    return parent, block_id


def is_block_virtual_source_id(source_id: str) -> bool:
    return parse_block_virtual_source_id(source_id) is not None


def _block_coords(block: Dict[str, Any]) -> Dict[str, int]:
    coords = block.get("coordinates", block) if isinstance(block, dict) else {}
    return {
        "start_row": int(coords.get("start_row", coords.get("header_row", 0))),
        "end_row": int(coords.get("end_row", coords.get("data_end_row", 0))),
        "start_col": int(coords.get("start_col", coords.get("col_start", 0))),
        "end_col": int(coords.get("end_col", coords.get("col_end", 0))),
        "header_row": int(coords.get("header_row", coords.get("start_row", 0))),
    }


def blocks_are_column_disjoint(blocks: List[Dict[str, Any]]) -> bool:
    """True when main blocks sit side-by-side (no column overlap)."""
    spans: List[Tuple[int, int]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        c = _block_coords(block)
        spans.append((c["start_col"], c["end_col"]))
    if len(spans) < 2:
        return False
    for i, a in enumerate(spans):
        for j, b in enumerate(spans):
            if j <= i:
                continue
            if not (a[1] < b[0] or b[1] < a[0]):
                return False
    return True


def get_main_blocks_for_source(job: Dict[str, Any], source_id: str) -> List[Dict[str, Any]]:
    """Approved main blocks for a registry source (demarcation batch, scope registry, or layout registry)."""
    sid = str(source_id or "").strip()
    batch = job.get("demarcation_batch_proposals") or {}
    if sid in batch:
        blocks = [
            b
            for b in (batch[sid].get("blocks") or [])
            if isinstance(b, dict)
            and str(b.get("decision", "Keep")).strip().lower() in ("keep", "approved", "")
            and str(b.get("category", "Main Data")).strip().lower().replace(" ", "")
            in ("maindata", "main_data", "")
        ]
        if blocks:
            return blocks

    scope = dict((job.get("source_scope_registry") or {}).get(sid) or {})
    main = list(scope.get("main_blocks") or [])
    if main:
        return main

    out: List[Dict[str, Any]] = []
    for row in job.get("layout_registry") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("source_id") or "").strip() != sid:
            continue
        cat = str(row.get("block_category") or "").strip().lower().replace(" ", "").replace("_", "")
        dec = str(row.get("decision") or "").strip().lower()
        if cat != "maindata" or dec in ("discard", "context"):
            continue
        if dec not in ("keep", "approved", ""):
            continue
        hr = row.get("header_row", row.get("start_row", 0))
        nested = row.get("coordinates") or {}
        coords: Dict[str, Any] = {
            "start_row": int(row.get("start_row", 0)),
            "end_row": int(row.get("end_row", 0)),
            "start_col": int(row.get("start_col", 0)),
            "end_col": int(row.get("end_col", 0)),
            "header_row": int(hr),
        }
        if row.get("header_row_end") is not None:
            coords["header_row_end"] = int(row["header_row_end"])
        if row.get("header_mode") is not None:
            coords["header_mode"] = str(row["header_mode"]).strip().lower()
        out.append(
            {
                "id": str(row.get("block_id") or row.get("layout_id") or "block"),
                "category": "Main Data",
                "decision": "Keep",
                "coordinates": coords,
            }
        )
    return out


def should_process_main_blocks_separately(job: Dict[str, Any], source_id: str) -> bool:
    """Multiple disjoint horizontal main blocks → one LangGraph run per block, then collate."""
    if is_block_virtual_source_id(source_id):
        return False
    blocks = get_main_blocks_for_source(job, source_id)
    return len(blocks) >= 2 and blocks_are_column_disjoint(blocks)


def register_block_runs(
    job: Dict[str, Any],
    parent_source_id: str,
    blocks: List[Dict[str, Any]],
) -> List[str]:
    """Register virtual block sources on the job and return their ids."""
    registry: Dict[str, Any] = dict(job.get("_block_run_registry") or {})
    virtual_ids: List[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        vid = block_virtual_source_id(parent_source_id, block)
        registry[vid] = {
            "parent_source_id": str(parent_source_id).strip(),
            "block": dict(block),
            "block_id": parse_block_virtual_source_id(vid)[1],
        }
        virtual_ids.append(vid)
    job["_block_run_registry"] = registry
    return virtual_ids


def resolve_block_run(job: Dict[str, Any], virtual_source_id: str) -> Optional[Dict[str, Any]]:
    return dict((job.get("_block_run_registry") or {}).get(str(virtual_source_id)) or {}) or None


def build_scoped_source_for_block(
    job: Dict[str, Any],
    parent_source_id: str,
    block: Dict[str, Any],
    *,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Scope limited to a single main block (same pattern as one demarcated table)."""
    parent_scope = dict((job.get("source_scope_registry") or {}).get(parent_source_id) or {})
    sheet_frame = parent_scope.get("sheet_frame") or {}
    total_rows = sheet_frame.get("rows")
    total_cols = sheet_frame.get("cols")
    header_row = _block_coords(block).get("header_row")
    if parent_scope.get("header_row") is not None:
        header_row = int(parent_scope["header_row"])

    scoped = build_scoped_source(
        sheet_name=sheet_name or parent_scope.get("sheet_name"),
        total_rows=total_rows,
        total_cols=total_cols,
        blocks=[block],
        user_selected_header_row=job.get("user_selected_header_row") if job.get("user_selected_header_row") is not None else header_row,
    )
    scoped["parent_source_id"] = str(parent_source_id).strip()
    scoped["block_id"] = str(block.get("id") or block.get("block_id") or "")
    scoped["process_blocks_separately"] = True
    scoped["multi_block_batch"] = True
    return scoped


def layout_extract_params_for_block(block: Dict[str, Any], scoped_source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deterministic layout.extract rectangle for one block."""
    c = _block_coords(block)
    header_row = c["header_row"]
    if scoped_source and scoped_source.get("header_row") is not None:
        header_row = int(scoped_source["header_row"])
    data_start = c.get("data_start_row")
    if data_start is None:
        coords = block.get("coordinates", block) if isinstance(block, dict) else {}
        data_start = coords.get("data_start_row", header_row + 1)
    return {
        "start_row": int(c["start_row"]),
        "end_row": int(c["end_row"]),
        "start_col": int(c["start_col"]),
        "end_col": int(c["end_col"]),
        "header_row": int(header_row),
    }


def expand_multi_block_source_ids(
    job: Dict[str, Any],
    source_ids: List[str],
) -> Tuple[List[str], bool]:
    """
    Replace a parent source id with virtual per-block ids when the sheet has multiple main blocks.

    Returns (expanded_ids, any_expanded).
    """
    expanded: List[str] = []
    any_expanded = False
    for sid in source_ids or []:
        sid_s = str(sid or "").strip()
        if not sid_s:
            continue
        if is_block_virtual_source_id(sid_s):
            expanded.append(sid_s)
            continue
        if should_process_main_blocks_separately(job, sid_s):
            blocks = get_main_blocks_for_source(job, sid_s)
            vids = register_block_runs(job, sid_s, blocks)
            if len(vids) >= 2:
                expanded.extend(vids)
                any_expanded = True
                logger.info(
                    "[MULTI_BLOCK] Expanded source %s into %d block run(s): %s",
                    sid_s,
                    len(vids),
                    vids,
                )
                continue
        expanded.append(sid_s)
    return expanded, any_expanded


def ensure_block_union_relationship(job: Dict[str, Any], block_source_ids: List[str]) -> None:
    """Ensure collate step union-stacks block frames (same as multi-sheet union)."""
    if len(block_source_ids) < 2:
        return
    ids = [str(x).strip() for x in block_source_ids if str(x).strip()]
    existing = list(job.get("approved_file_relationships") or [])
    for rel in existing:
        if not isinstance(rel, dict):
            continue
        kind = str(rel.get("relationship_kind") or "").lower()
        rel_ids = [str(x).strip() for x in (rel.get("source_ids") or [])]
        if kind == "union" and set(rel_ids) == set(ids):
            return
    parent = None
    for vid in ids:
        parsed = parse_block_virtual_source_id(vid)
        if parsed:
            parent = parsed[0]
            break
    existing.append(
        {
            "relationship_kind": "union",
            "relationship_type": "union_stack",
            "source_ids": ids,
            "description": (
                f"Union stack of {len(ids)} main-data blocks on sheet"
                + (f" (source {parent})" if parent else "")
            ),
            "auto_generated": True,
            "same_sheet_multi_block": True,
        }
    )
    job["approved_file_relationships"] = existing


def enrich_context_packet_for_block_run(
    packet: Dict[str, Any],
    *,
    virtual_source_id: str,
    parent_source_id: str,
    block: Dict[str, Any],
) -> Dict[str, Any]:
    """Tag context so planner/executor use layout.extract on this block only (no layout.stack)."""
    out = dict(packet or {})
    sm = dict(out.get("source_metadata") or {})
    sm["source_id"] = virtual_source_id
    sm["parent_source_id"] = parent_source_id
    sm["block_id"] = str(block.get("id") or block.get("block_id") or "")
    sm["process_blocks_separately"] = True
    sm["multi_block_batch"] = True
    out["source_metadata"] = sm
    out["process_blocks_separately"] = True
    out["multi_block_batch"] = True
    layout = dict(out.get("approved_layout") or {})
    layout["main_blocks_count"] = 1
    layout["main_blocks"] = [block]
    layout["blocks"] = [block]
    out["approved_layout"] = layout
    lineage = dict(out.get("lineage") or {})
    lineage["source_id"] = virtual_source_id
    lineage["parent_source_id"] = parent_source_id
    lineage["block_id"] = sm["block_id"]
    out["lineage"] = lineage
    return out


def block_ui_label(block: Dict[str, Any], index: int = 0) -> str:
    """Human label for mapping UI sections."""
    bid = str(block.get("id") or block.get("block_id") or f"block_{index + 1}").strip()
    excel = str(block.get("excel_range") or "").strip()
    if excel:
        return f"Main data block {index + 1} ({excel})"
    c = _block_coords(block)
    return f"Main data block {index + 1} (cols {c['start_col']}–{c['end_col']})"


def normalize_mapping_row_column_name(row: Dict[str, Any]) -> Dict[str, Any]:
    """Guided Setup cards use ``column_name``; registry rows often only have ``source_column``."""
    item = dict(row)
    col = str(item.get("column_name") or item.get("source_column") or "").strip()
    if col:
        item["column_name"] = col
        if not str(item.get("source_column") or "").strip():
            item["source_column"] = col
    return item


def attach_block_metadata_to_mapping_rows(
    rows: List[Dict[str, Any]],
    block: Dict[str, Any],
    *,
    index: int = 0,
) -> List[Dict[str, Any]]:
    """Tag each proposed/saved mapping row with ``block_id`` for per-block agent runs."""
    bid = str(block.get("id") or block.get("block_id") or f"block_{index}").strip()
    label = block_ui_label(block, index)
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = normalize_mapping_row_column_name(row)
        item["block_id"] = bid
        item["block_label"] = label
        out.append(item)
    return out


def group_mapping_rows_by_block(
    registry_rows: List[Dict[str, Any]],
    main_blocks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build ``mapping_blocks`` payload for Guided Setup UI."""
    block_order = [
        str(b.get("id") or b.get("block_id") or f"block_{i}")
        for i, b in enumerate(main_blocks or [])
        if isinstance(b, dict)
    ]
    by_id: Dict[str, List[Dict[str, Any]]] = {bid: [] for bid in block_order}
    for row in registry_rows or []:
        if not isinstance(row, dict):
            continue
        bid = str(row.get("block_id") or "").strip()
        if not bid:
            continue
        by_id.setdefault(bid, []).append(normalize_mapping_row_column_name(row))
    sections: List[Dict[str, Any]] = []
    for i, block in enumerate(main_blocks or []):
        if not isinstance(block, dict):
            continue
        bid = str(block.get("id") or block.get("block_id") or f"block_{i}").strip()
        sections.append(
            {
                "block_id": bid,
                "block_label": block_ui_label(block, i),
                "excel_range": block.get("excel_range") or "",
                "mapping": by_id.get(bid, []),
            }
        )
    return sections


def flatten_mapping_blocks(mapping_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flat: List[Dict[str, Any]] = []
    for section in mapping_blocks or []:
        if not isinstance(section, dict):
            continue
        for row in section.get("mapping") or []:
            if isinstance(row, dict):
                flat.append(dict(row))
    return flat
