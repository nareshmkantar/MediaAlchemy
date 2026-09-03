"""Per-source context isolation: local vs job-global scope and leak detection."""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from sia.context.confidence import (
    CONFIDENCE_BLOCK_PARSE,
    CONFIDENCE_PLANNER_ASSUMPTION,
    CONFIDENCE_TAB_INFERENCE,
)
from sia.context.scoped_field import (
    ScopedField,
    merge_scoped_field,
    scoped_fields_from_dict,
    scoped_fields_to_flat,
)

logger = logging.getLogger(__name__)

# Template / planner dimension columns that must come from source-local context.
SOURCE_LOCAL_DIMENSION_COLUMNS = frozenset(
    {"channel", "market", "region", "brand", "campaign", "owner", "publisher"}
)

_CHANNEL_INTERPRET_KEYS = frozenset(
    {
        "channel",
        "channel context",
        "channel_context",
        "media channel",
        "paid channel",
        "media_channel",
        "channel_type",
    }
)

_CHANNEL_TAB_NORMALIZE = {
    "digital": "digital",
    "tv": "TV",
    "radio": "radio",
    "print": "print",
    "ooh": "out of home",
    "outofhome": "out of home",
    "out_of_home": "out of home",
}


def _norm_key(line: str) -> str:
    return re.sub(r"[\s_]+", " ", str(line or "").strip().lower())


def _extract_kv(line: str, keys: set[str]) -> Optional[str]:
    """Parse ``Key: value`` or ``Key | value`` lines (shared with context_packet)."""
    separators = ["|", ":", "-", "="]
    parts = [segment.strip() for segment in line.split("|") if segment.strip()]
    if len(parts) >= 2:
        key = _norm_key(parts[0])
        if key in keys:
            return parts[1].strip()
    lower = line.lower()
    for key in sorted(keys, key=len, reverse=True):
        key_lower = key.lower()
        if lower.startswith(key_lower):
            remainder = line[len(key) :].strip()
            for sep in separators[1:]:
                if remainder.startswith(sep):
                    return remainder[len(sep) :].strip()
            if remainder:
                return remainder
    return None


def _looks_like_tab_market_code(suffix: str) -> bool:
    """True when a tab suffix looks like a geography code (UK, DE, US), not free text."""
    token = str(suffix or "").strip()
    if not token:
        return False
    return bool(re.fullmatch(r"[A-Za-z]{2,3}", token))


def infer_tab_scope_fields(sheet_name: Optional[str]) -> Dict[str, str]:
    """
    Infer channel/market from tab naming conventions (e.g. ``Digital_UK``, ``Radio_DE``).

    Used only when context blocks did not already supply the field. Results are soft
    hints (``scope=sheet``, confidence ``CONFIDENCE_TAB_INFERENCE``) for planner
    context — not authoritative finalize stamps unless separately confirmed.

    ``channel`` is set **only** when the tab prefix is a known media-channel label
    (see ``_CHANNEL_TAB_NORMALIZE``). Country / geography prefixes such as
    ``UK_Nation_Spend`` or ``US_State_Spend`` must not invent ``channel=UK/US``.
    Market is inferred from the suffix only when it looks like a short geography
    code (2–3 letters), so ``Continuation`` / ``Nation_Spend`` are not markets.
    """
    name = str(sheet_name or "").strip()
    if not name or "_" not in name:
        return {}
    channel_raw, market_raw = name.split("_", 1)
    channel_raw = channel_raw.strip()
    market_raw = market_raw.strip()
    out: Dict[str, str] = {}
    if not channel_raw:
        return out
    ch_key = channel_raw.lower().replace(" ", "_")
    channel_norm = _CHANNEL_TAB_NORMALIZE.get(ch_key)
    if not channel_norm:
        # Unknown prefix (country code, brand, free text) — do not invent channel.
        return out
    out["channel"] = channel_norm
    if market_raw and _looks_like_tab_market_code(market_raw):
        out["market"] = market_raw.upper()
    return out


def _evidence_index(interpreted_context: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    ic = dict(interpreted_context or {})
    out: Dict[str, Dict[str, Any]] = {}
    for row in ic.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        fname = str(row.get("field") or "").strip()
        if fname:
            out[fname] = row
    return out


def build_scoped_fields(
    interpreted_context: Optional[Dict[str, Any]],
    context_block_snippets: Optional[Sequence[Dict[str, Any]]],
    sheet_name: Optional[str],
    *,
    source_id: str = "",
) -> Dict[str, ScopedField]:
    """
    Build scoped local-dimension fields with provenance.

    Precedence: block evidence > interpreted packet fields > tab inference.
    """
    sheet = str(sheet_name or "").strip()
    sid = str(source_id or "").strip()
    fields: Dict[str, ScopedField] = {}
    ic = dict(interpreted_context or {})
    evidence_by_field = _evidence_index(ic)

    for key, value in dict(ic.get("fields") or {}).items():
        cleaned = str(value or "").strip()
        if not cleaned:
            continue
        name = str(key).strip()
        ev = evidence_by_field.get(name) or {}
        ev_conf = ev.get("confidence")
        if ev_conf is not None:
            conf = float(ev_conf)
        elif ev:
            conf = CONFIDENCE_BLOCK_PARSE if str(ev.get("scope") or "") == "block" else CONFIDENCE_TAB_INFERENCE
        else:
            conf = CONFIDENCE_PLANNER_ASSUMPTION
        merge_scoped_field(
            fields,
            ScopedField(
                name=name,
                value=cleaned,
                scope=str(ev.get("scope") or ("block" if ev else "interpreted")),
                source_id=str(ev.get("source_id") or sid),
                sheet_name=str(ev.get("sheet_name") or sheet),
                block_id=ev.get("block_id"),
                block_label=ev.get("block_label"),
                evidence_line=ev.get("line") or ev.get("evidence_line"),
                confidence=conf,
                hop=int(ev.get("hop", 0)),
            ),
        )

    for snippet in context_block_snippets or []:
        if not isinstance(snippet, dict):
            continue
        block_id = str(snippet.get("block_id") or "").strip() or None
        block_label = str(snippet.get("block_label") or snippet.get("block_id") or "context_block")
        for line in snippet.get("text_preview") or []:
            norm_line = " ".join(str(line).strip().split())
            if not norm_line:
                continue
            ch = _extract_kv(norm_line, _CHANNEL_INTERPRET_KEYS)
            if ch:
                ch_key = ch.lower().replace(" ", "_")
                merge_scoped_field(
                    fields,
                    ScopedField(
                        name="channel",
                        value=_CHANNEL_TAB_NORMALIZE.get(ch_key, ch),
                        scope="block",
                        source_id=sid,
                        sheet_name=sheet,
                        block_id=block_id,
                        block_label=block_label,
                        evidence_line=norm_line,
                        confidence=CONFIDENCE_BLOCK_PARSE,
                        hop=0,
                    ),
                )
            mk = _extract_kv(norm_line, {"market", "region", "country", "geo"})
            if mk:
                merge_scoped_field(
                    fields,
                    ScopedField(
                        name="market",
                        value=mk,
                        scope="block",
                        source_id=sid,
                        sheet_name=sheet,
                        block_id=block_id,
                        block_label=block_label,
                        evidence_line=norm_line,
                        confidence=CONFIDENCE_BLOCK_PARSE,
                        hop=0,
                    ),
                )

    for key, value in infer_tab_scope_fields(sheet).items():
        merge_scoped_field(
            fields,
            ScopedField(
                name=key,
                value=value,
                scope="sheet",
                source_id=sid,
                sheet_name=sheet,
                evidence_line=f"tab:{sheet}" if sheet else None,
                confidence=CONFIDENCE_TAB_INFERENCE,
                hop=1,
            ),
        )

    return fields


def append_tab_inference_evidence(
    interpreted_context: Optional[Dict[str, Any]],
    scoped: Dict[str, ScopedField],
) -> Dict[str, Any]:
    """Add evidence rows for fields filled only via tab-name inference."""
    ic = dict(interpreted_context or {})
    evidence = list(ic.get("evidence") or [])
    existing = {
        str(row.get("field") or "").strip()
        for row in evidence
        if isinstance(row, dict) and str(row.get("field") or "").strip()
    }
    for name, sf in scoped.items():
        if sf.scope != "sheet":
            continue
        if name in existing:
            continue
        evidence.append(
            {
                "field": name,
                "value": sf.value,
                "scope": "sheet",
                "source_id": sf.source_id,
                "sheet_name": sf.sheet_name,
                "line": sf.evidence_line or f"tab:{sf.sheet_name}",
                "confidence": sf.confidence,
                "hop": sf.hop,
            }
        )
        existing.add(name)
    ic["evidence"] = evidence[:24]
    return ic


def enrich_interpreted_fields(
    interpreted_context: Optional[Dict[str, Any]],
    context_block_snippets: Optional[Sequence[Dict[str, Any]]],
    sheet_name: Optional[str],
    *,
    source_id: str = "",
) -> Dict[str, str]:
    """
    Merge interpreted context-block fields with tab-scope inference.

    Context blocks win over tab naming when both define the same field.
    """
    scoped = build_scoped_fields(
        interpreted_context,
        context_block_snippets,
        sheet_name,
        source_id=source_id,
    )
    return scoped_fields_to_flat(scoped)


def local_scoped_fields(context_packet: Optional[Dict[str, Any]]) -> Dict[str, ScopedField]:
    """Authoritative scoped dimension literals for the active sheet."""
    cp = context_packet or {}
    ic = cp.get("interpreted_context") if isinstance(cp.get("interpreted_context"), dict) else {}
    stored = scoped_fields_from_dict((ic or {}).get("scoped_fields"))
    if stored:
        return stored
    lineage = cp.get("lineage") if isinstance(cp.get("lineage"), dict) else {}
    sm = cp.get("source_metadata") if isinstance(cp.get("source_metadata"), dict) else {}
    sheet_name = str(
        lineage.get("sheet_name") or sm.get("sheet_name") or cp.get("selected_source_sheet") or ""
    ).strip()
    source_id = str(lineage.get("source_id") or sm.get("source_id") or "").strip()
    return build_scoped_fields(
        ic,
        cp.get("context_block_snippets"),
        sheet_name,
        source_id=source_id,
    )


def local_context_fields(context_packet: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Flat string view of source-local dimensions (backward compatible)."""
    return scoped_fields_to_flat(local_scoped_fields(context_packet))


def rebind_source_local_plan_literals(
    tool_calls: Optional[Sequence[Dict[str, Any]]],
    context_packet: Optional[Dict[str, Any]],
    *,
    job: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Replace ``transform.add_column`` literals for source-local dimensions using
    interpreted context for *this* sheet (prevents cross-sheet plan bleed).
    """
    local = local_context_fields(context_packet)
    if not local or not tool_calls:
        return [dict(t) for t in tool_calls or [] if isinstance(t, dict)], []

    rebound: List[Dict[str, Any]] = []
    actions: List[str] = []
    for tool in tool_calls or []:
        if not isinstance(tool, dict):
            continue
        row = copy.deepcopy(tool)
        name = str(row.get("tool") or "").strip()
        params = dict(row.get("params") or {})
        if name == "transform.add_column":
            target = str(params.get("target_column") or params.get("column") or "").strip()
            if target in SOURCE_LOCAL_DIMENSION_COLUMNS and target in local:
                new_val = local[target]
                old_val = params.get("value")
                if str(old_val or "").strip().lower() != str(new_val).strip().lower():
                    params["value"] = new_val
                    actions.append(
                        f"rebound {target}: {old_val!r} → {new_val!r} (source-local context)"
                    )
                row["params"] = params
        rebound.append(row)
    if job is not None and actions:
        from sia.context.diff_log import log_rebind_actions

        log_rebind_actions(
            job,
            source_id=context_packet_source_id(context_packet),
            actions=actions,
            stage="plan_rebind",
        )
    return rebound, actions


def plan_source_id(plan: Any) -> str:
    if isinstance(plan, dict):
        return str(plan.get("source_id") or "").strip()
    return str(getattr(plan, "source_id", "") or "").strip()


def context_packet_source_id(context_packet: Optional[Dict[str, Any]]) -> str:
    cp = context_packet or {}
    lineage = cp.get("lineage") if isinstance(cp.get("lineage"), dict) else {}
    sm = cp.get("source_metadata") if isinstance(cp.get("source_metadata"), dict) else {}
    return str(lineage.get("source_id") or sm.get("source_id") or "").strip()


def plan_bound_to_wrong_source(
    resume_state: Dict[str, Any],
    context_packet: Optional[Dict[str, Any]],
) -> bool:
    """True when a persisted plan was built for a different ``source_id``."""
    active_sid = str(resume_state.get("source_id") or context_packet_source_id(context_packet) or "").strip()
    if not active_sid:
        return False
    for key in ("extraction_plan",):
        plan = resume_state.get(key)
        bound = plan_source_id(plan) if plan else ""
        if bound and bound != active_sid:
            return True
    saved_cp = resume_state.get("context_packet")
    if isinstance(saved_cp, dict):
        saved_sid = context_packet_source_id(saved_cp)
        if saved_sid and saved_sid != active_sid and resume_state.get("suggested_tools"):
            return True
    return False


def _normalize_join_key_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize date/publisher for merge and fingerprint comparisons."""
    work = df.copy()
    if "date" in work.columns:
        work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "publisher" in work.columns:
        work["publisher"] = work["publisher"].map(
            lambda v: str(v).strip() if v is not None and str(v).strip() else ""
        )
    return work


def _normalize_clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    for col in df.columns:
        low = str(col).strip().lower()
        if low == "date":
            rename[col] = "date"
        elif low == "publisher":
            rename[col] = "publisher"
        elif low in ("spend", "spends"):
            rename[col] = "spends"
        elif low in ("impression", "impressions"):
            rename[col] = "impressions"
    out = df.rename(columns=rename)
    return out


def _load_clean_template_frame(job: Dict[str, Any], source_id: str) -> Optional[pd.DataFrame]:
    templates = job.get("materialized_clean_templates") or {}
    meta = templates.get(str(source_id))
    if not isinstance(meta, dict):
        return None
    path = Path(str(meta.get("path") or ""))
    if not path.is_file():
        return None
    try:
        raw = pd.read_excel(path)
        return _normalize_clean_columns(raw)
    except Exception as exc:
        logger.debug("context_isolation: failed to read clean template %s: %s", path, exc)
        return None


def _metric_fingerprint(df: pd.DataFrame) -> Dict[str, Tuple[float, float]]:
    """Map ``date|publisher`` → (spends, impressions) for bleed detection."""
    if df is None or df.empty:
        return {}
    work = _normalize_join_key_columns(_normalize_clean_columns(df.copy()))
    key_cols = [c for c in ("date", "publisher") if c in work.columns]
    if not key_cols:
        return {}
    out: Dict[str, Tuple[float, float]] = {}
    for _, row in work.iterrows():
        key = "|".join(str(row[c]) for c in key_cols)
        spends = float(pd.to_numeric(row.get("spends"), errors="coerce") or 0.0)
        impressions = float(pd.to_numeric(row.get("impressions"), errors="coerce") or 0.0)
        out[key] = (spends, impressions)
    return out


def check_context_packet_lineage(
    context_packet: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return violations when the active packet lacks sheet lineage for stamping."""
    violations: List[Dict[str, Any]] = []
    cp = context_packet or {}
    lineage = cp.get("lineage") if isinstance(cp.get("lineage"), dict) else {}
    sm = cp.get("source_metadata") if isinstance(cp.get("source_metadata"), dict) else {}
    sheet_name = str(
        lineage.get("sheet_name") or sm.get("sheet_name") or cp.get("selected_source_sheet") or ""
    ).strip()
    if sheet_name:
        return violations
    source_id = str(lineage.get("source_id") or sm.get("source_id") or "").strip()
    violations.append(
        {
            "type": "context_lineage_missing",
            "message": (
                "context_packet missing lineage.sheet_name"
                + (f" for source_id={source_id!r}" if source_id else "")
                + "; cannot stamp source-local dimensions"
            ),
        }
    )
    return violations


def has_context_packet_lineage(context_packet: Optional[Dict[str, Any]]) -> bool:
    """True when ``lineage.sheet_name`` (or equivalent) is present on the packet."""
    return not check_context_packet_lineage(context_packet)


def check_output_matches_local_context(
    df: Optional[pd.DataFrame],
    context_packet: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return violations when output dimension values disagree with local context."""
    violations: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return violations
    local = local_context_fields(context_packet)
    for col in ("channel", "market"):
        expected = str(local.get(col) or "").strip()
        if not expected or col not in df.columns:
            continue
        distinct = {
            str(v).strip()
            for v in df[col].dropna().unique()
            if str(v).strip()
        }
        for value in sorted(distinct):
            if value.lower() != expected.lower():
                violations.append(
                    {
                        "type": "dimension_mismatch",
                        "column": col,
                        "expected": expected,
                        "actual": value,
                        "message": f"{col}={value!r} does not match source-local context ({expected!r})",
                    }
                )
    return violations


def check_output_metrics_match_clean_template(
    df: Optional[pd.DataFrame],
    job: Dict[str, Any],
    source_id: str,
) -> List[Dict[str, Any]]:
    """Detect when processed metrics match another sheet's body (row bleed)."""
    violations: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return violations
    clean = _load_clean_template_frame(job, source_id)
    if clean is None or clean.empty:
        return violations
    expected_fp = _metric_fingerprint(clean)
    actual_fp = _metric_fingerprint(_normalize_clean_columns(df.copy()))
    for key, (exp_s, exp_i) in expected_fp.items():
        if key not in actual_fp:
            continue
        act_s, act_i = actual_fp[key]
        if abs(act_s - exp_s) > 0.01 or abs(act_i - exp_i) > 0.01:
            violations.append(
                {
                    "type": "metric_mismatch",
                    "key": key,
                    "expected_spends": exp_s,
                    "actual_spends": act_s,
                    "expected_impressions": exp_i,
                    "actual_impressions": act_i,
                    "message": (
                        f"Metrics for {key} do not match this sheet's clean template "
                        f"(expected spends={exp_s}, impressions={exp_i}; "
                        f"got spends={act_s}, impressions={act_i})"
                    ),
                }
            )
    return violations


def _distinct_non_empty_values(series: pd.Series) -> set[str]:
    return {
        str(v).strip()
        for v in series.dropna().unique()
        if str(v).strip()
    }


def _column_accepts_sheet_constant_stamp(df: pd.DataFrame, col: str) -> bool:
    """
    True when a dimension column is missing, empty, or a single value (bleed fix).

    Columns with two or more distinct populated values are treated as row-level grain
    and must not be overwritten by sheet-wide context stamping.
    """
    if col not in df.columns:
        return True
    distinct = _distinct_non_empty_values(df[col])
    if not distinct:
        return True
    return len(distinct) < 2


def apply_local_context_dimensions(
    df: Optional[pd.DataFrame],
    context_packet: Optional[Dict[str, Any]],
    *,
    job: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
    min_confidence: Optional[float] = None,
) -> Optional[pd.DataFrame]:
    """Stamp source-local channel/market (and related) literals onto the output frame."""
    from sia.context.confidence import MIN_CONFIDENCE_TO_STAMP
    from sia.context.stamp_policy import stampable_local_fields

    if df is None or df.empty:
        return df
    threshold = MIN_CONFIDENCE_TO_STAMP if min_confidence is None else float(min_confidence)
    local = stampable_local_fields(context_packet, min_confidence=threshold)
    if not local:
        return df
    before_df = df.copy()
    result = df.copy()
    for col in SOURCE_LOCAL_DIMENSION_COLUMNS:
        value = str(local.get(col) or "").strip()
        if not value:
            continue
        if not _column_accepts_sheet_constant_stamp(result, col):
            continue
        result[col] = value
    if job is not None:
        from sia.context.diff_log import log_dimension_column_snapshots

        sid = str(
            source_id
            or context_packet_source_id(context_packet)
            or ""
        ).strip()
        log_dimension_column_snapshots(
            job,
            stage="stamp_dimensions",
            source_id=sid,
            df_before=before_df,
            df_after=result,
            reason="apply_local_context_dimensions",
        )
    return result


def align_output_metrics_to_clean_template(
    df: Optional[pd.DataFrame],
    job: Dict[str, Any],
    source_id: str,
    *,
    log_diff: bool = True,
) -> Optional[pd.DataFrame]:
    """
    When a materialized clean template exists, restore spends/impressions per date+publisher.

    Prevents cross-sheet row bleed from polluting per-source frames before union collation.
    """
    if df is None or df.empty:
        return df
    clean = _load_clean_template_frame(job, source_id)
    if clean is None or clean.empty:
        return df
    if not {"date", "publisher"}.issubset(clean.columns):
        return df

    work = _normalize_join_key_columns(_normalize_clean_columns(df.copy()))
    clean = _normalize_join_key_columns(_normalize_clean_columns(clean.copy()))

    merge_cols = ["date", "publisher"]
    metric_cols = [c for c in ("spends", "impressions") if c in clean.columns and c in work.columns]
    if not metric_cols:
        return df

    right = clean[merge_cols + metric_cols].drop_duplicates(subset=merge_cols, keep="first")
    merged = work.drop(columns=metric_cols, errors="ignore").merge(
        right,
        on=merge_cols,
        how="left",
        suffixes=("", "_clean"),
    )
    for col in metric_cols:
        clean_col = f"{col}_clean"
        if clean_col in merged.columns:
            merged[col] = merged[clean_col].combine_first(merged.get(col))
            merged = merged.drop(columns=[clean_col], errors="ignore")
    if log_diff and isinstance(job, dict):
        from sia.context.diff_log import log_value_change

        before_fp = _metric_fingerprint(work)
        after_fp = _metric_fingerprint(merged)
        if before_fp != after_fp:
            log_value_change(
                job,
                stage="align_metrics",
                source_id=str(source_id),
                field="metric_fingerprint",
                before=len(before_fp),
                after=len(after_fp),
                reason="align_output_metrics_to_clean_template",
                meta={
                    "keys_changed": sorted(set(before_fp) ^ set(after_fp))[:12],
                },
            )
    return merged


def assess_source_context_isolation(
    df: Optional[pd.DataFrame],
    context_packet: Optional[Dict[str, Any]],
    *,
    job: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Pipeline eval summary for one processed source (delegates to ContextVerifier)."""
    from sia.context.verifier import ContextVerifier

    return ContextVerifier.assess_output_frame(
        df,
        context_packet,
        job=job,
        source_id=source_id,
    )
