"""Guards and helpers for ``layout.stack`` — stack only when blocks are identified and schemas align."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

_DUPLICATE_COL_SUFFIX = re.compile(r"^(.+)\.(\d+)$", re.I)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def looks_like_horizontal_block_failure(df: Optional[pd.DataFrame]) -> bool:
    """
    True when columns look like ``Date``, ``Date.1``, ``Spend.2`` — side-by-side blocks
    flattened without vertical stack (see ``scoped_source._make_unique_columns``).
    """
    if df is None or df.empty:
        return False
    cols = [str(c) for c in df.columns]
    bases = {c for c in cols if not _DUPLICATE_COL_SUFFIX.match(c)}
    dup_suffix = 0
    for c in cols:
        m = _DUPLICATE_COL_SUFFIX.match(c)
        if m and m.group(1) in bases:
            dup_suffix += 1
    return dup_suffix >= 2


def block_header_signature(
    df: pd.DataFrame,
    header_row: int,
    col_start: int,
    col_end: int,
) -> List[str]:
    """Normalized header labels for one horizontal block (for alignment checks)."""
    if header_row < 0 or header_row >= len(df):
        return []
    row = df.iloc[header_row, col_start:col_end]
    sig: List[str] = []
    for v in row.tolist():
        text = str(v).strip().lower() if pd.notna(v) else ""
        if not text:
            continue
        text = text.replace("_", " ").replace("-", " ")
        sig.append(text)
    return sig


def _normalize_block_coords(block_def: Dict[str, Any]) -> Optional[Dict[str, int]]:
    if not isinstance(block_def, dict):
        return None
    col_start = _safe_int(block_def.get("col_start", block_def.get("start_col")), -1)
    col_end = _safe_int(block_def.get("col_end", block_def.get("end_col")), -1)
    if col_end < col_start:
        return None
    row_start = _safe_int(block_def.get("row_start"), -1)
    row_end = _safe_int(block_def.get("row_end"), -1)
    out: Dict[str, int] = {"col_start": col_start, "col_end": col_end}
    if row_start >= 0:
        out["row_start"] = row_start
    if row_end >= 0:
        out["row_end"] = row_end
    return out


def assess_stack_readiness(
    df: pd.DataFrame,
    header_row: int,
    blocks: List[Dict[str, Any]],
    *,
    min_shared_headers: int = 2,
) -> Tuple[bool, str]:
    """
    Return (ready, reason). Stack only when ≥2 column-disjoint blocks share enough header roles.
    """
    if df is None or df.empty:
        return False, "empty grid"
    if header_row < 0 or header_row >= len(df):
        return False, f"invalid header_row {header_row}"
    if not blocks or len(blocks) < 2:
        return False, "layout.stack requires at least two block definitions"

    normalized: List[Dict[str, int]] = []
    for b in blocks:
        nb = _normalize_block_coords(b)
        if nb is None:
            return False, "each block needs col_start <= col_end"
        width = nb["col_end"] - nb["col_start"] + 1
        if width < 1:
            return False, "block column span is empty"
        normalized.append(nb)

    # Column ranges must not overlap (separate horizontal blocks).
    for i, a in enumerate(normalized):
        for j, b in enumerate(normalized):
            if j <= i:
                continue
            if not (a["col_end"] < b["col_start"] or b["col_end"] < a["col_start"]):
                return False, "block column ranges overlap; use layout.extract per block instead"

    signatures = [
        block_header_signature(df, header_row, b["col_start"], b["col_end"] + 1)
        for b in normalized
    ]
    if any(len(s) < min_shared_headers for s in signatures):
        return False, "block headers are too sparse to align schemas"

    # Require meaningful overlap of header roles across blocks (same metric/dimension names).
    common = set(signatures[0])
    for sig in signatures[1:]:
        common &= set(sig)
    if len(common) < min_shared_headers:
        return False, (
            "block headers do not share enough column roles to stack safely; "
            "extract each block separately first"
        )

    return True, "ok"


def build_blocks_from_main_blocks(
    main_blocks: List[Dict[str, Any]],
    *,
    header_row: int,
    data_end_row: Optional[int] = None,
) -> List[Dict[str, int]]:
    """Turn approved demarcation ``main_blocks`` into ``layout.stack`` block coords."""
    out: List[Dict[str, int]] = []
    for block in main_blocks or []:
        if not isinstance(block, dict):
            continue
        coords = block.get("coordinates", block) if isinstance(block.get("coordinates"), dict) else block
        nb = _normalize_block_coords(coords)
        if nb is None:
            continue
        row_start = coords.get("data_start_row", coords.get("start_row"))
        row_end = coords.get("data_end_row", coords.get("end_row", data_end_row))
        if row_start is not None:
            nb["row_start"] = _safe_int(row_start, header_row + 1)
        else:
            nb["row_start"] = header_row + 1
        if row_end is not None:
            nb["row_end"] = _safe_int(row_end, nb["row_start"])
        elif data_end_row is not None:
            nb["row_end"] = data_end_row
        out.append(nb)
    # Sort left-to-right for stable stacking order.
    out.sort(key=lambda b: (b["col_start"], b["col_end"]))
    return out


def build_blocks_from_structure_tables(
    tables: List[Dict[str, Any]],
    *,
    header_row: int,
) -> List[Dict[str, int]]:
    """Build stack blocks from structure analyzer ``tables`` entries."""
    out: List[Dict[str, int]] = []
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        coords = t.get("coordinates", t) if isinstance(t.get("coordinates"), dict) else t
        nb = _normalize_block_coords(coords)
        if nb is None:
            continue
        r0 = coords.get("data_start_row", coords.get("start_row", header_row + 1))
        r1 = coords.get("data_end_row", coords.get("end_row"))
        nb["row_start"] = _safe_int(r0, header_row + 1)
        if r1 is not None:
            nb["row_end"] = _safe_int(r1, nb["row_start"])
        out.append(nb)
    out.sort(key=lambda b: (b["col_start"], b["col_end"]))
    return out


def detect_horizontal_blocks_by_blank_columns(
    df: pd.DataFrame,
    header_row: int,
    *,
    min_block_width: int = 3,
) -> List[Dict[str, int]]:
    """Infer side-by-side blocks separated by blank columns (fallback when planner omits blocks)."""
    if df is None or df.empty or header_row >= len(df):
        return []
    ncols = len(df.columns)
    blank_cols: List[int] = []
    for c in range(ncols):
        col_vals = df.iloc[header_row + 1 :, c] if header_row + 1 < len(df) else df.iloc[:, c]
        if col_vals.isna().all() or (col_vals.astype(str).str.strip() == "").all():
            blank_cols.append(c)

    boundaries = [-1] + blank_cols + [ncols]
    blocks: List[Dict[str, int]] = []
    for i in range(len(boundaries) - 1):
        c0 = boundaries[i] + 1
        c1 = boundaries[i + 1] - 1
        if c1 - c0 + 1 >= min_block_width:
            blocks.append(
                {
                    "col_start": c0,
                    "col_end": c1,
                    "row_start": header_row + 1,
                    "row_end": len(df) - 1,
                }
            )
    return blocks


def sanitize_layout_stack_params(
    params: Dict[str, Any],
    *,
    structure_analysis: Optional[Dict[str, Any]] = None,
    scoped_source: Optional[Dict[str, Any]] = None,
    context_packet: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Fill or validate ``layout.stack`` params. Returns (params, warnings).
    Does not invent blocks when readiness cannot be established.
    """
    warnings: List[str] = []
    p = dict(params or {})
    scoped = scoped_source if isinstance(scoped_source, dict) else {}
    cp = context_packet if isinstance(context_packet, dict) else {}
    sa = structure_analysis if isinstance(structure_analysis, dict) else {}

    header_row = p.get("header_row")
    if header_row is None:
        header_row = scoped.get("header_row")
    if header_row is None:
        layout = (cp.get("approved_layout") or cp.get("planning_summary", {}).get("layout_summary") or {})
        if isinstance(layout, dict):
            header_row = layout.get("header_row")
    p["header_row"] = _safe_int(header_row, 0)

    blocks = p.get("blocks")
    if not isinstance(blocks, list) or len(blocks) < 2:
        main_blocks = list(scoped.get("main_blocks") or [])
        if not main_blocks:
            approved = cp.get("approved_layout") or {}
            if isinstance(approved, dict):
                main_blocks = list(approved.get("main_blocks") or [])
        bounds = scoped.get("analysis_bounds") or {}
        data_end = bounds.get("end_row") if isinstance(bounds, dict) else None
        built = build_blocks_from_main_blocks(
            main_blocks,
            header_row=p["header_row"],
            data_end_row=_safe_int(data_end, -1) if data_end is not None else None,
        )
        if len(built) >= 2:
            p["blocks"] = built
            warnings.append("layout.stack blocks filled from approved main_blocks")
        else:
            tables = sa.get("tables") or []
            built_t = build_blocks_from_structure_tables(tables, header_row=p["header_row"])
            if len(built_t) >= 2:
                p["blocks"] = built_t
                warnings.append("layout.stack blocks filled from structure analysis tables")

    return p, warnings


def should_prefer_stack_over_single_extract(
    structure_analysis: Optional[Dict[str, Any]],
    scoped_source: Optional[Dict[str, Any]],
) -> bool:
    """True when analysis indicates side-by-side repeating column blocks (not one flat table)."""
    sa = structure_analysis if isinstance(structure_analysis, dict) else {}
    scoped = scoped_source if isinstance(scoped_source, dict) else {}
    repetitions = any(
        (t.get("column_pattern", {}) or {}).get("repetitions", 1) > 1
        for t in (sa.get("tables") or [])
        if isinstance(t, dict)
    )
    main_blocks = list(scoped.get("main_blocks") or [])
    if len(main_blocks) >= 2:
        coords = [b.get("coordinates", b) for b in main_blocks if isinstance(b, dict)]
        col_spans = []
        for c in coords:
            if not isinstance(c, dict):
                continue
            cs = _safe_int(c.get("col_start", c.get("start_col")), 0)
            ce = _safe_int(c.get("col_end", c.get("end_col")), cs)
            col_spans.append((cs, ce))
        if len(col_spans) >= 2:
            # Disjoint column spans → horizontal blocks
            for i, a in enumerate(col_spans):
                for j, b in enumerate(col_spans):
                    if j <= i:
                        continue
                    if not (a[1] < b[0] or b[1] < a[0]):
                        return False
            return True
    return repetitions
