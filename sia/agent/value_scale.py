"""Detect and apply header denomination hints (e.g. Spends in '000 → multiply by 1,000)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

KEEP_DECISIONS = {"keep", "approved", "primary", "supporting", "metadata", "context", "use as context"}


def detect_value_scale_from_header(header: str) -> Tuple[float, str]:
    """
    Parse unit scale from a physical column header.

    Returns (scale_factor, human_note). scale_factor 1.0 means no adjustment.
    """
    text = str(header or "").strip()
    if not text:
        return 1.0, ""

    lower = text.lower()

    if re.search(r"in\s*['\u2019]?\s*000", lower) or "'000" in lower or "\u2019000" in lower:
        return 1000.0, "values in thousands ('000)"
    if re.search(r"\(\s*000\s*'?s?\s*\)", lower):
        return 1000.0, "values in thousands (000)"
    if re.search(r"\bin\s+thousands?\b", lower):
        return 1000.0, "values in thousands"
    if re.search(r"\bin\s+millions?\b", lower):
        return 1_000_000.0, "values in millions"
    if re.search(r"\(\s*k\s*\)\s*$", lower) or re.search(r"\s+in\s+k\s*$", lower):
        return 1000.0, "values in thousands (k)"
    if re.search(r"\(\s*m\s*\)\s*$", lower) and any(
        token in lower for token in ("spend", "cost", "impression", "click", "budget", "revenue")
    ):
        return 1_000_000.0, "values in millions (m)"

    return 1.0, ""


def enrich_mapping_value_scale(row: Dict[str, Any]) -> Dict[str, Any]:
    """Attach ``value_scale`` / ``value_scale_note`` from header text when not already set."""
    out = dict(row)
    col = str(out.get("source_column") or out.get("column_name") or "").strip()
    try:
        existing = float(out.get("value_scale") or 1.0)
    except (TypeError, ValueError):
        existing = 1.0
    if existing not in (0.0, 1.0):
        out.setdefault("value_scale", existing)
        return out

    scale, note = detect_value_scale_from_header(col)
    out["value_scale"] = scale
    if note:
        out["value_scale_note"] = note
        reasoning = str(out.get("reasoning") or "").strip()
        if note.lower() not in reasoning.lower():
            suffix = f"Header denomination: {note}."
            out["reasoning"] = f"{reasoning} {suffix}".strip() if reasoning else suffix
    return out


def value_scale_summary_from_mappings(
    mappings: Optional[List[Dict[str, Any]]],
) -> List[str]:
    """Human-readable lines for planner prompts and planning_summary."""
    lines: List[str] = []
    for item in mappings or []:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or "").strip().lower()
        if decision not in KEEP_DECISIONS:
            continue
        try:
            scale = float(item.get("value_scale") or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        if scale in (0.0, 1.0):
            continue
        src = str(item.get("source_column") or item.get("column_name") or "").strip()
        tgt = str(item.get("target_column") or "").strip()
        note = str(item.get("value_scale_note") or f"multiply by {scale:g}").strip()
        if tgt and tgt != "No match":
            lines.append(f"{src} → {tgt}: {note}")
        elif src:
            lines.append(f"{src}: {note}")
    return lines


def build_target_scales_from_mappings(
    mappings: Optional[List[Dict[str, Any]]],
) -> Dict[str, float]:
    """
    Target column → scale factor after rename.

    When multiple sources map to one target, scales must agree; last wins with a warning
    left to the caller (planner logs in reasoning).
    """
    scales: Dict[str, float] = {}
    for item in mappings or []:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or "").strip().lower()
        if decision not in KEEP_DECISIONS:
            continue
        target = str(item.get("target_column") or "").strip()
        if not target or target.lower() == "no match":
            continue
        try:
            scale = float(item.get("value_scale") or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        if scale in (0.0, 1.0):
            continue
        scales[target] = scale
    return scales
