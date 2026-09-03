"""Deterministic structure-analyzer consistency checks."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from sia.evals.schema import empty_stage_result, stage_passes, violation

_FP_NOTE_PATTERNS = (
    re.compile(r"populated .+ throughout", re.I),
    re.compile(r"not sparse", re.I),
    re.compile(r"false positive", re.I),
    re.compile(r"anomaly,?\s+but", re.I),
    re.compile(r"reported sparse.+but sampled", re.I),
)


def _norm_label(value: Any) -> str:
    return str(value or "").strip().casefold()


def _iter_structure_columns(sa: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect column dicts from structure_analyzer JSON (column_analysis, tables, …)."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(col: Any) -> None:
        if not isinstance(col, dict):
            return
        label = _norm_label(col.get("column_label") or col.get("label") or col.get("name"))
        key = label or str(col.get("column_index", ""))
        if key in seen:
            return
        seen.add(key)
        out.append(col)

    for key in ("column_analysis", "columns"):
        for col in sa.get(key) or []:
            add(col)
    for tbl in sa.get("tables") or []:
        if not isinstance(tbl, dict):
            continue
        for col in tbl.get("columns") or tbl.get("column_analysis") or []:
            add(col)
    return out


def _structure_column_by_label(sa: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    by_label: Dict[str, Dict[str, Any]] = {}
    for col in _iter_structure_columns(sa):
        label = _norm_label(col.get("column_label") or col.get("label") or col.get("name"))
        if label:
            by_label[label] = col
    return by_label


def _notes_text(col: Dict[str, Any]) -> str:
    notes = col.get("notes") or col.get("note") or []
    if isinstance(notes, str):
        return notes
    if isinstance(notes, list):
        return " ".join(str(n) for n in notes if n)
    return ""


def _llm_contradicts_sparse_flag(col: Dict[str, Any]) -> bool:
    if col.get("is_blank") is False:
        return True
    text = _notes_text(col)
    if not text:
        return False
    return any(p.search(text) for p in _FP_NOTE_PATTERNS)


def _detect_sparse_dimension_false_positives(
    signals: Dict[str, Any],
    sa: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
  Return false-positive sparse-dimension flags.

  A flag is a false positive when the deterministic grid scan listed the column under
  ``sparse_dimension_columns`` but the structure analyzer reports populated values or
  explicit notes contradicting sparsity (e.g. Publisher on flat CP-07 sheets).
  """
    sparse_dims = [d for d in (signals.get("sparse_dimension_columns") or []) if isinstance(d, dict)]
    if not sparse_dims:
        return []

    by_label = _structure_column_by_label(sa)
    false_positives: List[Dict[str, Any]] = []

    for entry in sparse_dims:
        label = str(entry.get("column_label") or entry.get("column_name") or "").strip()
        if not label:
            continue
        text_fill = entry.get("text_fill_rate")
        if text_fill is not None:
            try:
                if float(text_fill) >= 0.9:
                    false_positives.append(
                        {
                            "column_label": label,
                            "reason": "high_text_fill_rate",
                            "text_fill_rate": float(text_fill),
                            "blank_ratio": entry.get("blank_ratio"),
                        }
                    )
                    continue
            except (TypeError, ValueError):
                pass

        col = by_label.get(_norm_label(label))
        if col and _llm_contradicts_sparse_flag(col):
            false_positives.append(
                {
                    "column_label": label,
                    "reason": "structure_analyzer_contradiction",
                    "notes": _notes_text(col)[:240],
                    "blank_ratio": entry.get("blank_ratio"),
                }
            )

    return false_positives


def _layout_scan_score(
    *,
    analyzer_confidence: float,
    sparse_flagged: int,
    sparse_fp: int,
    grouped_rows_likely: bool,
    block_sparse_count: int,
    signal_violation_count: int,
) -> float:
    score = 1.0
    if sparse_flagged:
        precision = max(0.0, (sparse_flagged - sparse_fp) / sparse_flagged)
        score -= 0.35 * (1.0 - precision)
    if grouped_rows_likely and block_sparse_count == 0 and sparse_fp > 0:
        score -= 0.2
    if grouped_rows_likely and block_sparse_count == 0:
        score -= 0.1
    score -= 0.08 * signal_violation_count
    score = 0.55 * score + 0.45 * max(0.0, min(1.0, analyzer_confidence))
    return round(max(0.0, min(1.0, score)), 3)


def evaluate_structure_consistency(
    structure_analysis: Optional[Dict[str, Any]],
    *,
    context_packet: Optional[Dict[str, Any]] = None,
    metric_layout_signals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out = empty_stage_result()
    violations: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {}

    sa = dict(structure_analysis or {})
    cp = context_packet or {}
    signals = dict(metric_layout_signals or {})
    if not signals and isinstance(sa.get("visual_patterns"), dict):
        signals = dict(sa["visual_patterns"].get("metric_layout_signals") or {})

    tables = [t for t in (sa.get("tables") or []) if isinstance(t, dict)]
    layout = cp.get("approved_layout") if isinstance(cp.get("approved_layout"), dict) else {}
    main_blocks = [b for b in (layout.get("main_blocks") or layout.get("blocks") or []) if isinstance(b, dict)]

    metrics["table_count"] = len(tables)
    metrics["main_block_count"] = len(main_blocks)
    metrics["analyzer_confidence"] = float(sa.get("confidence") or 0.0)

    if main_blocks and tables:
        delta = abs(len(tables) - len(main_blocks))
        metrics["layout_alignment_delta"] = delta
        if delta > 2:
            violations.append(
                violation(
                    vtype="layout_alignment",
                    message=(
                        f"Analyzer reported {len(tables)} tables but approved layout has "
                        f"{len(main_blocks)} main blocks"
                    ),
                    severity="advisory",
                )
            )

    sparse_dims = [d for d in (signals.get("sparse_dimension_columns") or []) if isinstance(d, dict)]
    block_sparse = [m for m in (signals.get("block_sparse_metrics") or []) if isinstance(m, dict)]
    metrics["sparse_dimension_flagged_count"] = len(sparse_dims)
    metrics["block_sparse_metric_count"] = len(block_sparse)

    false_positives = _detect_sparse_dimension_false_positives(signals, sa)
    metrics["sparse_dimension_false_positive_count"] = len(false_positives)
    flagged = len(sparse_dims)
    if flagged:
        metrics["sparse_dimension_precision"] = round(
            max(0.0, (flagged - len(false_positives)) / flagged),
            3,
        )
    else:
        metrics["sparse_dimension_precision"] = 1.0

    for fp in false_positives:
        label = fp.get("column_label") or "column"
        violations.append(
            violation(
                vtype="sparse_dimension_false_positive",
                message=(
                    f"Grid layout scan flagged {label!r} as sparse, but structure analysis "
                    f"indicates populated values ({fp.get('reason', 'contradiction')})"
                ),
                severity="advisory",
                evidence=dict(fp),
            )
        )

    grouped = bool(signals.get("grouped_rows_likely"))
    metrics["grouped_rows_likely"] = grouped
    if grouped and not block_sparse and false_positives:
        violations.append(
            violation(
                vtype="grouped_layout_false_positive",
                message=(
                    "grouped_rows_likely was set from sparse-dimension false positives only — "
                    "no block-sparse metrics detected on spend/impression columns"
                ),
                severity="advisory",
            )
        )

    if grouped:
        uncertainties = [u for u in (sa.get("uncertainty") or []) if isinstance(u, dict)]
        has_alloc = any(
            str(u.get("topic") or "").strip() == "block_metric_allocation" for u in uncertainties
        )
        metrics["block_metric_uncertainty_present"] = has_alloc
        if block_sparse and not has_alloc:
            violations.append(
                violation(
                    vtype="signal_consistency",
                    message=(
                        "metric_layout_signals indicate grouped/block-sparse metrics but "
                        "structure_analysis uncertainty lacks block_metric_allocation"
                    ),
                    severity="advisory",
                )
            )

    metrics["layout_scan_score"] = _layout_scan_score(
        analyzer_confidence=metrics["analyzer_confidence"],
        sparse_flagged=flagged,
        sparse_fp=len(false_positives),
        grouped_rows_likely=grouped,
        block_sparse_count=len(block_sparse),
        signal_violation_count=sum(
            1 for v in violations if v.get("type") in ("signal_consistency", "layout_alignment")
        ),
    )

    out["metrics"] = metrics
    out["violations"] = violations
    out["pass"] = stage_passes(violations)
    return out
