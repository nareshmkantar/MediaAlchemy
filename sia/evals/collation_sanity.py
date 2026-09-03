"""Multi-source collation evals (union false duplicates, cross-source isolation)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from sia.agent.target_template_utils import normalize_target_template, template_union_grain_columns
from sia.evals.schema import empty_stage_result, stage_passes, violation

_DEFAULT_JOIN_KEYS = ["date", "channel", "market", "publisher"]
_DEFAULT_MEASURE_COLS = ["spends", "impressions"]


def resolve_collation_fingerprint_columns(
    target_template: Optional[Dict[str, Any]] = None,
    *,
    join_keys: Optional[List[str]] = None,
    measure_cols: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """Derive union fingerprint columns from template UID grain + metrics."""
    explicit_keys = [str(k) for k in (join_keys or []) if str(k).strip()]
    explicit_measures = [str(m) for m in (measure_cols or []) if str(m).strip()]

    tpl = normalize_target_template(target_template) if target_template else {}
    xs = tpl.get("x_scope") if isinstance(tpl.get("x_scope"), dict) else {}

    keys = explicit_keys
    if not keys and tpl:
        keys = template_union_grain_columns(tpl)
    if not keys:
        keys = list(_DEFAULT_JOIN_KEYS)

    measures = explicit_measures
    if not measures and tpl:
        measures = [str(m) for m in (xs.get("metrics") or []) if m is not None and str(m).strip()]
    if not measures:
        measures = list(_DEFAULT_MEASURE_COLS)

    return keys, measures


def evaluate_cross_source_isolation(
    per_source_evals: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Roll up per-source context_isolation passes."""
    out = empty_stage_result()
    per = dict(per_source_evals or {})
    failures: List[str] = []
    for sid, row in per.items():
        if not isinstance(row, dict):
            continue
        iso = row.get("context_isolation")
        if isinstance(iso, dict) and not iso.get("pass", True):
            failures.append(str(sid))
        elif row.get("pass") is False and row.get("violations"):
            failures.append(str(sid))
    out["metrics"] = {"failed_sources": failures, "source_count": len(per)}
    if failures:
        out["violations"] = [
            violation(
                vtype="metric_mismatch",
                message=f"Context isolation failed for source(s): {', '.join(failures[:6])}",
                severity="critical",
            )
        ]
        out["pass"] = False
    return out


def evaluate_union_false_duplicates(
    frames_by_source: Dict[str, pd.DataFrame],
    *,
    join_keys: Optional[List[str]] = None,
    measure_cols: Optional[List[str]] = None,
    target_template: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Detect rows identical on template grain + metrics but originating from different sources.
    """
    out = empty_stage_result()
    if not frames_by_source or len(frames_by_source) < 2:
        out["metrics"] = {"union_false_duplicates": 0}
        return out

    keys, template_measures = resolve_collation_fingerprint_columns(
        target_template,
        join_keys=join_keys,
        measure_cols=measure_cols,
    )
    violations: List[Dict[str, Any]] = []
    false_dup_count = 0

    fps_by_source: Dict[str, set] = {}
    for sid, df in frames_by_source.items():
        if df is None or df.empty:
            continue
        use_cols = [c for c in keys if c in df.columns]
        measure_cols_resolved = [
            c for c in df.columns if c not in use_cols and c in template_measures
        ]
        cols = use_cols + measure_cols_resolved
        if not cols:
            continue
        sub = df[cols].copy()
        for c in sub.columns:
            sub[c] = sub[c].astype(str)
        fps_by_source[str(sid)] = set(sub.apply(lambda r: "|".join(r.values), axis=1))

    sids = list(fps_by_source.keys())
    for i, a in enumerate(sids):
        for b in sids[i + 1 :]:
            shared = fps_by_source[a] & fps_by_source[b]
            if shared:
                false_dup_count += len(shared)
                violations.append(
                    violation(
                        vtype="union_false_duplicate",
                        message=f"Sources {a} and {b} share {len(shared)} identical row fingerprint(s) on union keys",
                        severity="critical",
                        evidence={
                            "sample_fingerprint": next(iter(shared)) if shared else "",
                            "join_keys": keys,
                            "measure_cols": template_measures,
                        },
                    )
                )

    out["metrics"] = {
        "union_false_duplicates": false_dup_count,
        "collation_join_keys": keys,
        "collation_measure_cols": template_measures,
    }
    out["violations"] = violations
    out["pass"] = stage_passes(violations)
    return out
