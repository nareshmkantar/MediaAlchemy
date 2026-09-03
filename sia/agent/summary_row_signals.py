"""Detect whether a scoped sheet likely contains Total/Subtotal rows."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sia.tools.transformation_tools import TransformationTools

_DEFAULT_SUMMARY_KEYWORDS = [
    "Subtotal",
    "SUBTOTAL",
    "Total",
    "Grand Total",
    "Totals",
]

_SUMMARY_STRUCTURE_KEYS = frozenset(
    {
        "exclusion_rows",
        "summary_rows",
        "rows_to_exclude",
        "aggregate_rows",
        "total_rows",
    }
)


def _keywords_lower(keywords: Optional[List[str]] = None) -> List[str]:
    return [str(k).lower() for k in (keywords or _DEFAULT_SUMMARY_KEYWORDS)]


def _text_has_summary_keyword(text: Any, keywords_lower: List[str]) -> bool:
    return TransformationTools._summary_keyword_in_text(text, keywords_lower)


def _scan_structure_strings(obj: Any, keywords_lower: List[str], *, depth: int = 0) -> bool:
    if depth > 8 or obj is None:
        return False
    if isinstance(obj, str):
        return _text_has_summary_keyword(obj, keywords_lower)
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if key_l in _SUMMARY_STRUCTURE_KEYS and value:
                return True
            if _scan_structure_strings(value, keywords_lower, depth=depth + 1):
                return True
        return False
    if isinstance(obj, (list, tuple)):
        return any(_scan_structure_strings(item, keywords_lower, depth=depth + 1) for item in obj)
    return False


def _preview_dataframe_has_summary_rows(
    file_path: str,
    sheet_name: Optional[str],
    scoped_source: Optional[Dict[str, Any]],
    keywords_lower: List[str],
) -> bool:
    """Run the same filter_summaries preview logic used in plan review."""
    if not file_path:
        return False
    try:
        from sia.agent.scoped_source import load_scoped_dataframe
        from sia.tools.transformation_tools import generate_deletion_preview

        df, _ = load_scoped_dataframe(file_path, sheet_name, scoped_source or {})
        if df is None or df.empty:
            return False
        preview = generate_deletion_preview(
            df,
            {
                "tool": "transform.filter_summaries",
                "params": {
                    "keywords": list(_DEFAULT_SUMMARY_KEYWORDS),
                    "use_structural_detection": True,
                },
            },
        )
        if preview is None:
            return False
        return bool(preview.rows_to_delete or preview.columns_to_delete)
    except Exception:
        return False


def _resolve_scoped_preview_context(
    context_packet: Optional[Dict[str, Any]],
    structure_analysis: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str], Dict[str, Any]]:
    cp = context_packet if isinstance(context_packet, dict) else {}
    sm = cp.get("source_metadata") if isinstance(cp.get("source_metadata"), dict) else {}
    layout = cp.get("approved_layout") if isinstance(cp.get("approved_layout"), dict) else {}
    scoped = dict(layout)
    if not scoped.get("analysis_bounds") and isinstance(cp.get("scoped_source"), dict):
        scoped = dict(cp.get("scoped_source") or {})
    file_path = str(sm.get("file_path") or cp.get("file_path") or "").strip() or None
    sheet_name = sm.get("sheet_name") or layout.get("sheet_name")
    if not sheet_name and isinstance(structure_analysis, dict):
        sheet_name = structure_analysis.get("sheet_name")
    return file_path, str(sheet_name) if sheet_name else None, scoped


def sheet_likely_has_summary_rows(
    structure_analysis: Optional[Dict[str, Any]] = None,
    context_packet: Optional[Dict[str, Any]] = None,
    *,
    keywords: Optional[List[str]] = None,
    try_dataframe_preview: bool = True,
) -> bool:
    """Return True when structure signals or a scoped preview suggest Total/Subtotal rows."""
    keywords_lower = _keywords_lower(keywords)

    if isinstance(structure_analysis, dict) and _scan_structure_strings(structure_analysis, keywords_lower):
        return True

    cp = context_packet if isinstance(context_packet, dict) else {}
    for snippet in cp.get("context_block_snippets") or []:
        if not isinstance(snippet, dict):
            continue
        for line in snippet.get("text_preview") or []:
            if _text_has_summary_keyword(line, keywords_lower):
                return True
        for row in snippet.get("sample_rows") or []:
            if isinstance(row, list):
                if any(_text_has_summary_keyword(cell, keywords_lower) for cell in row):
                    return True
            elif isinstance(row, dict):
                if any(_text_has_summary_keyword(v, keywords_lower) for v in row.values()):
                    return True

    reasoning = str((structure_analysis or {}).get("reasoning") or "")
    if reasoning and _text_has_summary_keyword(reasoning, keywords_lower):
        return True

    if try_dataframe_preview:
        file_path, sheet_name, scoped = _resolve_scoped_preview_context(context_packet, structure_analysis)
        if file_path and _preview_dataframe_has_summary_rows(file_path, sheet_name, scoped, keywords_lower):
            return True

    return False
