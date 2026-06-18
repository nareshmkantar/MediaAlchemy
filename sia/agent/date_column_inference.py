"""
Heuristic date cadence inference from column values (for planning / logging).

Uses parsed timestamps, **sorted unique** dates, and **median gaps in days** between
consecutive values. Complements Guided Setup `date_granularity` (analyst intent)
with what the **data** actually looks like after parsing.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from dateutil import parser as dateutil_parser
except Exception:  # pragma: no cover
    dateutil_parser = None  # type: ignore

_RANGE_SPLIT = re.compile(r"\s+to\s+", re.IGNORECASE)
# Do **not** treat a bare ASCII hyphen as a range delimiter (``26-Jun``, ``2026-01-01`` ISO dates).
# Allow: typographical en/em dash, or ASCII hyphen only when surrounded by whitespace (`` - ``).
_RANGE_DASH_TYPO = re.compile(r"\s*[–—]\s*")
_RANGE_DASH_SPACED = re.compile(r"\s+-\s+")


_START_NAME_HINT = re.compile(
    r"(^|_)(start|begin|from|open|period_start|flight_start|week_start|range_start)($|_)",
    re.IGNORECASE,
)
_END_NAME_HINT = re.compile(
    r"(^|_)(end|finish|through|until|close|period_end|flight_end|week_end|range_end|stop)($|_)",
    re.IGNORECASE,
)


def _header_start_score(col_name: str) -> float:
    """Loose name hint for **start** side of a range (word-boundary safe — avoids ``publisher``)."""
    n = str(col_name).lower().replace("_", " ")
    if re.search(r"\b(start|begin)\b", n):
        return 1.0
    if re.search(r"\b(period_start|flight_start|range_start)\b", n):
        return 1.0
    if re.search(r"\bfrom\b", n) and "copy" not in n:
        return 0.55
    return 0.0


def _header_end_score(col_name: str) -> float:
    """Loose name hint for **end** side of a range."""
    n = str(col_name).lower().replace("_", " ")
    if re.search(r"\b(end|finish|through|until)\b", n):
        return 1.0
    if re.search(r"\b(period_end|flight_end|range_end)\b", n):
        return 1.0
    return 0.0


def _columns_parseable_as_dates(work: pd.DataFrame, min_rate: float = 0.72) -> List[str]:
    """Columns that mostly parse as **real calendar** datetimes (excludes epoch-ish numeric noise)."""
    out: List[str] = []
    for c in work.columns:
        t = pd.to_datetime(work[c], errors="coerce", utc=False, format="mixed")
        rate = float(t.notna().mean()) if len(work) else 0.0
        if rate < min_rate:
            continue
        ok = t.notna()
        if not ok.any():
            continue
        years = t[ok].dt.year
        wall = float(((years >= 1980) & (years <= 2100)).mean())
        if wall < 0.75:
            continue
        out.append(str(c))
    return out


def _scan_date_range_pairs_on_columns(
    work: pd.DataFrame,
    column_candidates: List[str],
    *,
    min_rows: int,
    min_valid_rate: float,
) -> Optional[Dict[str, Any]]:
    """Pick the strongest (start,end) ordered pair from a restricted column list."""
    cols = [c for c in column_candidates if c in work.columns]
    if len(cols) < 2:
        return None

    def _score_pair(
        start: pd.Series,
        end: pd.Series,
        *,
        start_col: str,
        end_col: str,
    ) -> Optional[Dict[str, Any]]:
        both = start.notna() & end.notna()
        if not bool(both.any()):
            return None

        def _epoch_dominated_norm(t: pd.Series) -> bool:
            ok = t.notna()
            if not ok.any():
                return False
            n = t.dt.normalize()
            return bool((n == pd.Timestamp("1970-01-01")).mean() > 0.35)

        if _epoch_dominated_norm(start) or _epoch_dominated_norm(end):
            return None

        ordered = both & (end >= start)
        n_valid = int(ordered.sum())
        if n_valid < min_rows:
            return None
        rate = float(n_valid) / float(int(both.sum()) or 1)
        if rate < min_valid_rate:
            return None
        sub_start = start[ordered]
        sub_end = end[ordered]
        spans = (sub_end - sub_start).dt.days.astype("float64") + 1.0
        med_span = float(np.nanmedian(spans)) if len(spans) else 0.0
        if not np.isfinite(med_span) or med_span < 1.0:
            return None

        has_range_name_signal = (
            (_START_NAME_HINT.search(start_col) or _header_start_score(start_col) >= 0.55)
            and (_END_NAME_HINT.search(end_col) or _header_end_score(end_col) >= 0.55)
        )
        max_span = 10000.0 if has_range_name_signal else 400.0
        if med_span > max_span:
            return None

        return {
            "n_pairs_evaluated": n_valid,
            "valid_rate": round(rate, 3),
            "median_span_days": round(med_span, 3),
        }

    best: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for i, c1 in enumerate(cols):
        for c2 in cols[i + 1 :]:
            s1 = pd.to_datetime(work[c1], errors="coerce", utc=False, format="mixed").dt.normalize()
            s2 = pd.to_datetime(work[c2], errors="coerce", utc=False, format="mixed").dt.normalize()
            meta_a = _score_pair(s1, s2, start_col=c1, end_col=c2)
            meta_b = _score_pair(s2, s1, start_col=c2, end_col=c1)
            chosen: Optional[Tuple[str, str, Dict[str, Any]]] = None
            if meta_a and meta_b:
                if meta_a["valid_rate"] >= meta_b["valid_rate"]:
                    chosen = (c1, c2, meta_a)
                else:
                    chosen = (c2, c1, meta_b)
            elif meta_a:
                chosen = (c1, c2, meta_a)
            elif meta_b:
                chosen = (c2, c1, meta_b)
            else:
                continue

            start_c, end_c, meta = chosen
            name_bonus = 0.0
            if _START_NAME_HINT.search(start_c) or _header_start_score(start_c) >= 0.55:
                name_bonus += 0.06
            if _END_NAME_HINT.search(end_c) or _header_end_score(end_c) >= 0.55:
                name_bonus += 0.06
            score = float(meta["valid_rate"]) + min(0.12, name_bonus)

            if score > best_score:
                best_score = score
                conf = min(1.0, 0.45 + 0.55 * float(meta["valid_rate"]) + min(0.15, name_bonus))
                best = {
                    "start_date_col": start_c,
                    "end_date_col": end_c,
                    "confidence": round(conf, 3),
                    "valid_row_count": int(meta["n_pairs_evaluated"]),
                    "valid_rate": meta["valid_rate"],
                    "median_span_days": meta["median_span_days"],
                    "inference_method": "column_subset_scan",
                }

    return best


def _split_range_string(s: str) -> Optional[Tuple[str, str]]:
    """
    Split ``left to right``, ``left – right`` (typographical dash), or ``left - right`` (spaced ASCII).

    Returns a pair only when **both** halves parse as real dates — avoids ``26-Jun`` style labels
    where a bare hyphen is part of one token, not a start–end separator.
    """
    v = str(s).strip()
    if not v:
        return None
    splits: List[Tuple[str, str]] = []
    m = _RANGE_SPLIT.search(v)
    if m:
        splits.append((v[: m.start()].strip(), v[m.end() :].strip()))
    m2 = _RANGE_DASH_TYPO.search(v)
    if m2:
        splits.append((v[: m2.start()].strip(), v[m2.end() :].strip()))
    m3 = _RANGE_DASH_SPACED.search(v)
    if m3:
        splits.append((v[: m3.start()].strip(), v[m3.end() :].strip()))
    for left, right in splits:
        if len(left) < 2 or len(right) < 2:
            continue
        ts_a = _parse_date_piece(left)
        ts_b = _parse_date_piece(right)
        if pd.notna(ts_a) and pd.notna(ts_b):
            return left, right
    return None


def _parse_date_piece(piece: str) -> pd.Timestamp:
    """Parse a single date fragment (supports many human labels via dateutil fuzzy)."""
    piece = str(piece).strip()
    if not piece:
        return pd.NaT
    ts = pd.to_datetime(piece, errors="coerce", utc=False, format="mixed")
    if pd.notna(ts):
        return pd.Timestamp(ts).normalize()
    if dateutil_parser is not None:
        try:
            dt = dateutil_parser.parse(piece, fuzzy=True, default=None)
            return pd.Timestamp(dt).normalize()
        except (ValueError, TypeError, OverflowError):
            return pd.NaT
    return pd.NaT


def _whole_string_parses_as_single_date(v: str) -> bool:
    """
    True when the entire cell text is one calendar instant (not a splittable start–end range).

    Used to avoid false positives where ISO-like strings (``2026-06-28``) or Excel
    string serializations (``2026-06-28 00:00:00``) contain hyphens that are **not**
    start–end range separators.
    """
    s = str(v).strip()
    if not s:
        return False
    if _split_range_string(s):
        return False
    ts = pd.to_datetime(s, errors="coerce", utc=False, format="mixed")
    return bool(pd.notna(ts))


def _norm_guided_grain(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s in ("daily", "day", "d", "date"):
        return "daily"
    if s in ("weekly", "week", "w"):
        return "weekly"
    if s in ("monthly", "month", "m"):
        return "monthly"
    if s in ("quarterly", "quarter", "q"):
        return "quarterly"
    if s in ("range", "date_range", "daterange", "flight", "flighting"):
        return "range"
    return s or "unknown"


def _classify_median_gap_days(median_days: float) -> Tuple[str, int]:
    """Map median spacing (days) to a coarse cadence label and rounded bucket."""
    if median_days is None or not np.isfinite(median_days) or median_days <= 0:
        return "irregular", int(round(median_days)) if np.isfinite(median_days) else 0
    m = float(median_days)
    rounded = int(round(m))
    if m < 1.4:
        return "daily", max(1, rounded)
    if 5.5 <= m <= 8.5:
        return "weekly", 7
    if 25.0 <= m <= 35.0:
        return "monthly", 30
    if 85.0 <= m <= 98.0:
        return "quarterly", 91
    return "irregular", rounded


def _median_positive_gap_days(sorted_unique: Any) -> Tuple[Optional[float], np.ndarray]:
    """
    Median of **positive** day gaps between consecutive sorted unique datetimes.

    Mirrors the usual definition: sort, take consecutive differences in days, drop
    non-positive gaps, then ``np.median``. With **one** distinct timestamp there is
    no gap (returns ``None``). With **two** distinct timestamps there is a single
    gap ``g``; the median of one value is ``g``, so cadence can still be inferred.
    """
    u = pd.DatetimeIndex(pd.Series(sorted_unique).dropna().sort_values())
    if len(u) < 2:
        return None, np.array([], dtype=float)
    gaps = np.array([(u[i] - u[i - 1]).days for i in range(1, len(u))], dtype=float)
    gaps = gaps[gaps > 0]
    if gaps.size == 0:
        return None, gaps
    return float(np.median(gaps)), gaps


def _gap_p25_p75_days(gaps: np.ndarray) -> Tuple[float, float]:
    """IQR-style quartiles on positive gaps; one gap → p25 = p75 = that gap."""
    if gaps.size == 0:
        return 0.0, 0.0
    if gaps.size == 1:
        g = float(gaps[0])
        return g, g
    return float(np.percentile(gaps, 25)), float(np.percentile(gaps, 75))


def _collect_period_starts_and_windows(sub: pd.Series) -> Tuple[List[pd.Timestamp], List[int], int, int, int]:
    """
    Expand single dates and textual date ranges into period-start timestamps.

    Returns:
        starts: one timestamp per row (range → start; scalar → parsed date)
        window_days: span in whole days for rows where both range endpoints parsed (+1 inclusive)
        n_non_null: number of non-null input cells
        range_attempts: cells that looked like a split range
        range_resolved: range cells where both ends parsed
    """
    starts: List[pd.Timestamp] = []
    window_days: List[int] = []
    raw = sub.dropna()
    n_non_null = int(len(raw))
    range_attempts = 0
    range_resolved = 0
    if n_non_null == 0:
        return starts, window_days, 0, 0, 0

    for val in raw:
        if not isinstance(val, str):
            try:
                ts = pd.Timestamp(val)
                if pd.notna(ts):
                    starts.append(ts.normalize())
            except (ValueError, TypeError):
                pass
            continue
        s = str(val).strip()
        if not s:
            continue
        parts = _split_range_string(s)
        if parts:
            range_attempts += 1
            ts_a = _parse_date_piece(parts[0])
            ts_b = _parse_date_piece(parts[1])
            if pd.notna(ts_a) and pd.notna(ts_b):
                if ts_b < ts_a:
                    ts_a, ts_b = ts_b, ts_a
                starts.append(ts_a)
                span = int((ts_b - ts_a).days) + 1
                window_days.append(max(1, span))
                range_resolved += 1
            elif pd.notna(ts_a):
                starts.append(ts_a)
        else:
            ts = _parse_date_piece(s)
            if pd.notna(ts):
                starts.append(ts)

    return starts, window_days, n_non_null, range_attempts, range_resolved


def _range_like_share(raw: pd.Series, sample_cap: int = 400) -> float:
    s = raw.dropna().head(sample_cap)
    if s.empty:
        return 0.0
    hits = 0
    n = 0
    for val in s:
        n += 1
        if not isinstance(val, str):
            continue
        v = str(val).strip()
        if not v or _whole_string_parses_as_single_date(v):
            continue
        if _split_range_string(v):
            hits += 1
    return hits / n if n else 0.0


def _compute_confidence(profile: Dict[str, Any]) -> float:
    """Heuristic 0–1 score for how much to trust ``inferred_cadence``."""
    pr = float(profile.get("parse_rate") or 0.0)
    cadence = str(profile.get("inferred_cadence") or "")
    if cadence == "unparsed_text":
        return 0.0
    if cadence == "insufficient_data":
        return min(0.35, pr * 0.5)

    score = pr
    nu = int(profile.get("n_unique_parsed_dates") or 0)
    if nu < 4:
        score *= 0.75
    if nu < 3:
        score *= 0.55

    p25 = profile.get("gap_p25_days")
    p75 = profile.get("gap_p75_days")
    try:
        if p25 is not None and p75 is not None and float(p25) > 0 and float(p75) / float(p25) > 3.5:
            score *= 0.78
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    if cadence == "irregular":
        score *= 0.68

    rshare = float(profile.get("range_like_cell_share") or 0.0)
    rpr = float(profile.get("range_parse_rate") or 1.0)
    if rshare >= 0.15 and rpr < 0.88:
        score *= 0.72

    return float(min(1.0, max(0.0, score)))


def infer_date_column_profile(
    series: pd.Series,
    column_name: str,
    *,
    max_sample_rows: int = 25_000,
) -> Dict[str, Any]:
    """
    Infer cadence hints from one column.

    Expands textual **start–end** ranges into real endpoints, uses **start dates**
    for row-to-row spacing (median gap across sorted unique starts), and records
    **window length** stats for resolved ranges.
    """
    out: Dict[str, Any] = {
        "column": column_name,
        "n_non_null_sampled": 0,
        "n_parseable": 0,
        "parse_rate": 0.0,
        "n_unique_parsed_dates": 0,
        "median_gap_days": None,
        "median_gap_days_rounded": None,
        "inferred_cadence": "insufficient_data",
        "likely_range_string_cells": False,
        "range_like_cell_share": 0.0,
        "range_attempts": 0,
        "range_parse_rate": 1.0,
        "median_window_days": None,
        "sample_raw_values": [],
        "confidence": 0.0,
    }

    if series is None or not hasattr(series, "dropna"):
        return out

    sub = series.dropna()
    if sub.empty:
        return out

    if len(sub) > max_sample_rows:
        sub = sub.sample(n=max_sample_rows, random_state=0)

    out["n_non_null_sampled"] = int(len(sub))
    out["range_like_cell_share"] = round(_range_like_share(sub), 3)
    out["likely_range_string_cells"] = bool(out["range_like_cell_share"] >= 0.12)

    raw_strings = sub.astype(str)
    out["sample_raw_values"] = [raw_strings.iloc[i] for i in range(min(5, len(raw_strings)))]

    starts, window_days, n_non_null, range_attempts, range_resolved = _collect_period_starts_and_windows(sub)
    out["range_attempts"] = int(range_attempts)
    out["range_parse_rate"] = round(
        float(range_resolved / range_attempts) if range_attempts else 1.0,
        3,
    )
    n_parseable = len(starts)
    out["n_parseable"] = int(n_parseable)
    out["parse_rate"] = round(float(n_parseable / n_non_null) if n_non_null else 0.0, 3)

    if window_days:
        out["median_window_days"] = float(int(np.median(window_days)))

    if not starts:
        out["inferred_cadence"] = "unparsed_text"
        out["confidence"] = _compute_confidence(out)
        return out

    uniques = pd.to_datetime(pd.Series(list(set(starts))), errors="coerce").dropna().sort_values()
    out["n_unique_parsed_dates"] = int(len(uniques))
    if len(uniques) < 2:
        out["inferred_cadence"] = "insufficient_data"
        out["confidence"] = _compute_confidence(out)
        return out

    median_gap, gaps = _median_positive_gap_days(uniques)
    if median_gap is None:
        out["inferred_cadence"] = "insufficient_data"
        out["confidence"] = _compute_confidence(out)
        return out

    out["median_gap_days"] = median_gap
    label, rounded = _classify_median_gap_days(median_gap)
    out["median_gap_days_rounded"] = rounded
    out["inferred_cadence"] = label
    p25, p75 = _gap_p25_p75_days(gaps)
    out["gap_p25_days"] = p25
    out["gap_p75_days"] = p75
    out["confidence"] = _compute_confidence(out)
    return out


def infer_date_cadence_fast(
    series: pd.Series,
    column_name: str,
    *,
    max_rows: int = 100_000,
    vectorized_parse_threshold: float = 0.82,
) -> Dict[str, Any]:
    """
    Bulk-friendly cadence inference for mostly machine-parseable date columns.

    Uses vectorized ``pd.to_datetime``, then **sorted unique** timestamps and
    **positive day gaps** (median / quartiles) — same idea as
    ``infer_date_column_profile`` but skips per-cell string work when the
    column parses cleanly.

    Falls back to ``infer_date_column_profile`` when parse rate is low or when
    many cells look like textual start–end ranges (needs range-aware parsing).
    """
    if series is None or not hasattr(series, "dropna"):
        out = infer_date_column_profile(series, column_name)
        out["inference_method"] = "fallback"
        return out

    sub = series.dropna()
    if sub.empty:
        out = infer_date_column_profile(series, column_name)
        out["inference_method"] = "fallback"
        return out

    if len(sub) > max_rows:
        sub = sub.sample(n=max_rows, random_state=0)

    parsed = pd.to_datetime(sub, errors="coerce", utc=False, format="mixed")
    parse_rate = float(parsed.notna().mean()) if len(sub) else 0.0
    rshare = float(_range_like_share(sub))
    # ISO dates like 2026-01-01 match the range-dash heuristic; only trust rshare when parsing is imperfect.
    range_suspect = rshare >= 0.12 and parse_rate < 0.95

    if parse_rate < vectorized_parse_threshold or range_suspect:
        out = infer_date_column_profile(series, column_name)
        out["inference_method"] = "row_heuristic"
        return out

    norm = parsed.dt.normalize()
    valid = norm[parsed.notna()]
    n_parseable = int(len(valid))
    if n_parseable < 2:
        out = infer_date_column_profile(series, column_name)
        out["inference_method"] = "row_heuristic"
        return out

    u = pd.DatetimeIndex(pd.unique(valid)).sort_values()
    if len(u) < 2:
        out = infer_date_column_profile(series, column_name)
        out["inference_method"] = "row_heuristic"
        return out

    median_gap, gaps = _median_positive_gap_days(u)
    if median_gap is None:
        out = infer_date_column_profile(series, column_name)
        out["inference_method"] = "row_heuristic"
        return out

    label, rounded = _classify_median_gap_days(median_gap)
    p25, p75 = _gap_p25_p75_days(gaps)
    out: Dict[str, Any] = {
        "column": column_name,
        "n_non_null_sampled": int(len(sub)),
        "n_parseable": n_parseable,
        "parse_rate": round(parse_rate, 3),
        "n_unique_parsed_dates": int(len(u)),
        "median_gap_days": median_gap,
        "median_gap_days_rounded": rounded,
        "inferred_cadence": label,
        "likely_range_string_cells": False,
        "range_like_cell_share": round(rshare, 3),
        "range_attempts": 0,
        "range_parse_rate": 1.0,
        "median_window_days": None,
        "sample_raw_values": [],
        "gap_p25_days": p25,
        "gap_p75_days": p75,
        "inference_method": "vectorized_bulk",
    }
    out["confidence"] = _compute_confidence(out)
    return out


def _date_range_pair_mapping_targets(template: Optional[Dict[str, Any]]) -> set[str]:
    """Template targets eligible for start/end **date** column pairing (excludes generic UID paths)."""
    if not isinstance(template, dict):
        return set()
    out: set[str] = set()
    xs = template.get("x_scope")
    if isinstance(xs, dict):
        for key in ("date_columns", "primary_date_targets"):
            seq = xs.get(key)
            if isinstance(seq, list):
                for x in seq:
                    s = str(x).strip()
                    if s:
                        out.add(s)
    props = template.get("properties")
    if isinstance(props, dict):
        for name, spec in props.items():
            if not isinstance(spec, dict):
                continue
            nm = str(name).lower()
            fmt = str(spec.get("format") or "").lower()
            typ = str(spec.get("type") or "").lower()
            if "date" in nm or typ == "date" or fmt.startswith("date"):
                out.add(str(name))
    return out


def _date_like_targets(template: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(template, dict):
        return []
    targets: List[str] = []
    xs = template.get("x_scope")
    if isinstance(xs, dict):
        for key in ("uid_hierarchy", "date_columns", "primary_date_targets"):
            seq = xs.get(key)
            if isinstance(seq, list):
                targets.extend(str(x) for x in seq if str(x).strip())
    props = template.get("properties")
    if isinstance(props, dict):
        for name, spec in props.items():
            if not isinstance(spec, dict):
                continue
            nm = str(name).lower()
            fmt = str(spec.get("format") or "").lower()
            typ = str(spec.get("type") or "").lower()
            if "date" in nm or typ == "date" or fmt.startswith("date"):
                targets.append(str(name))
    # de-dupe preserving order
    seen = set()
    out: List[str] = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _keep_mappings(mappings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keep = {"keep", "approved", "primary", "supporting", "metadata", "context", "use as context"}
    rows: List[Dict[str, Any]] = []
    for item in mappings or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("decision", "")).strip().lower() not in keep:
            continue
        src = str(item.get("source_column") or "").strip()
        tgt = str(item.get("target_column") or "").strip()
        if not src or not tgt or tgt.lower() == "no match":
            continue
        rows.append(item)
    return rows


def _profile_confidences_for_date_rollup_gate(profiles: List[Dict[str, Any]]) -> List[float]:
    """Confidences that should affect ``aggregate_confidence`` for HITL gating.

    ``_date_like_targets`` includes ``uid_hierarchy`` (e.g. publisher, channel). Those
    columns still get cadence profiles, often ``unparsed_text`` with confidence 0.0.
    Taking ``min()`` across *all* profiles incorrectly flags "low aggregate confidence"
    when the real date column is clearly daily — so we exclude non-date cadence buckets
    from the rollup gate (they remain in ``columns`` / ``summary`` for transparency).
    """
    out: List[float] = []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        cad = str(p.get("inferred_cadence") or "").lower().strip()
        if cad in ("unparsed_text", "insufficient_data"):
            continue
        out.append(float(p.get("confidence") or 0.0))
    return out


def infer_date_range_column_pair(
    df: Optional[pd.DataFrame],
    approved_mappings: List[Dict[str, Any]],
    target_template: Optional[Dict[str, Any]],
    *,
    min_rows: int = 3,
    min_valid_rate: float = 0.82,
    max_sample_rows: int = 25_000,
) -> Optional[Dict[str, Any]]:
    """
    Detect two mapped source columns that behave as an inclusive **start / end date range**.

    Uses vectorized parsing, requires ``end >= start`` for most rows, and boosts scores when
    column names look like start/end. Intended for **separate date columns** (not one cell
    with ``Jan 1 to Jan 7`` text — that path uses ``infer_date_column_profile``).
    """
    if df is None or df.empty or not approved_mappings:
        return None

    targets = _date_range_pair_mapping_targets(target_template)
    if not targets:
        targets = set(_date_like_targets(target_template))
    if len(targets) < 1:
        return None

    date_mapped_sources: List[str] = []
    seen: set[str] = set()
    for item in _keep_mappings(approved_mappings):
        tgt = str(item.get("target_column") or "").strip()
        src = str(item.get("source_column") or "").strip()
        if tgt not in targets or not src or src not in df.columns:
            continue
        if src not in seen:
            seen.add(src)
            date_mapped_sources.append(src)

    if len(date_mapped_sources) < 2:
        return None

    work = df
    if len(work) > max_sample_rows:
        work = work.sample(n=max_sample_rows, random_state=0)

    out = _scan_date_range_pairs_on_columns(
        work, date_mapped_sources, min_rows=min_rows, min_valid_rate=min_valid_rate
    )
    if isinstance(out, dict):
        out["inference_method"] = "mapped_date_targets"
    return out


def infer_date_range_pair_heuristic(
    df: Optional[pd.DataFrame],
    *,
    min_rows: int = 3,
    min_valid_rate: float = 0.82,
    max_sample_rows: int = 25_000,
    max_pair_columns: int = 14,
) -> Optional[Dict[str, Any]]:
    """
    Infer start/end date columns from **sheet column names and parseability** when semantic
    mapping did not expose two distinct date targets (common cause of skipping range expansion).
    """
    if df is None or df.empty:
        return None
    work = df
    if len(work) > max_sample_rows:
        work = work.sample(n=max_sample_rows, random_state=0)

    cand = _columns_parseable_as_dates(work, min_rate=0.72)
    if len(cand) < 2:
        return None
    # Prefer columns whose headers look like start/end so we do not O(n²) on wide numeric sheets.
    hinted = [c for c in cand if _header_start_score(c) >= 0.5 or _header_end_score(c) >= 0.5]
    if len(hinted) >= 2:
        scan = hinted
    else:
        scan = cand
    if len(scan) > max_pair_columns:
        scan = sorted(
            scan,
            key=lambda c: max(_header_start_score(c), _header_end_score(c)),
            reverse=True,
        )[:max_pair_columns]

    out = _scan_date_range_pairs_on_columns(work, scan, min_rows=min_rows, min_valid_rate=min_valid_rate)
    if isinstance(out, dict):
        out["inference_method"] = "header_parseable_columns"
    return out


def build_date_column_observations_for_planning(
    df: Optional[pd.DataFrame],
    approved_mappings: List[Dict[str, Any]],
    target_template: Optional[Dict[str, Any]],
    *,
    guided_date_granularity: str = "",
) -> Dict[str, Any]:
    """
    Build planner-facing observations for mapped date / UID columns.

    Returns ``columns`` (list of profiles), ``summary`` (one line), optional
    ``mismatch_note`` when Guided Setup grain disagrees with inferred cadence.
    """
    empty: Dict[str, Any] = {
        "columns": [],
        "summary": "",
        "mismatch_note": "",
        "inferred_date_range_pair": None,
    }
    if df is None or df.empty or not approved_mappings:
        return empty

    targets = set(_date_like_targets(target_template))
    if not targets:
        return empty

    inferred_pair = infer_date_range_column_pair(df, approved_mappings, target_template)
    if not inferred_pair:
        inferred_pair = infer_date_range_pair_heuristic(df)

    profiles: List[Dict[str, Any]] = []
    for item in _keep_mappings(approved_mappings):
        tgt = str(item.get("target_column") or "").strip()
        src = str(item.get("source_column") or "").strip()
        if tgt not in targets:
            continue
        if src not in df.columns:
            continue
        prof = infer_date_column_profile(df[src], src)
        prof["target_column"] = tgt
        profiles.append(prof)

    if not profiles:
        out_empty = dict(empty)
        out_empty["inferred_date_range_pair"] = inferred_pair
        return out_empty

    bits = [
        f"{p['column']}→{p.get('target_column','')}: cadence≈{p.get('inferred_cadence')}"
        f" (median gap {p.get('median_gap_days')} d, conf {round(float(p.get('confidence') or 0), 2)})"
        for p in profiles[:4]
    ]
    summary = "; ".join(bits)
    if inferred_pair:
        summary += (
            f" | two-column range: {inferred_pair.get('start_date_col')}→"
            f"{inferred_pair.get('end_date_col')} (median span≈{inferred_pair.get('median_span_days')} d, "
            f"conf {inferred_pair.get('confidence')})"
        )

    mismatch = ""
    guided = _norm_guided_grain(guided_date_granularity)
    primary = profiles[0]
    inferred = str(primary.get("inferred_cadence") or "")
    pair_conf = float(inferred_pair.get("confidence") or 0) if isinstance(inferred_pair, dict) else 0.0
    pair_ok = (
        isinstance(inferred_pair, dict)
        and inferred_pair.get("start_date_col")
        and inferred_pair.get("end_date_col")
        and pair_conf >= 0.55
    )
    # Guided "range" / flighting is about start–end span semantics; an end column can
    # still have ~1d median gaps between rows. That is not contradictory to "range".
    if guided == "range" and inferred == "daily" and pair_ok:
        mismatch = ""
    elif guided not in ("", "unknown") and inferred not in ("insufficient_data", "unparsed_text", "irregular", ""):
        if guided != inferred:
            mismatch = (
                f"Guided Setup date_granularity is {guided_date_granularity!r} but column "
                f"{primary.get('column')!r} looks ~{inferred} from median gaps; reconcile before rollup."
            )

    confidences = _profile_confidences_for_date_rollup_gate(profiles)
    aggregate_confidence = round(min(confidences), 3) if confidences else None

    return {
        "columns": profiles,
        "summary": summary,
        "mismatch_note": mismatch,
        "aggregate_confidence": aggregate_confidence,
        "guided_date_granularity": str(guided_date_granularity or ""),
        "inferred_date_range_pair": inferred_pair,
    }


def build_date_inference_approval_items(date_observations_package: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    At most **one** planner decision when date cadence is shaky or conflicts with Guided Setup.

    Uses a multiple-choice block instead of stacking Yes/No rows.
    """
    if not isinstance(date_observations_package, dict):
        return []
    cols = list(date_observations_package.get("columns") or [])
    if not cols:
        return []

    mismatch = str(date_observations_package.get("mismatch_note") or "").strip()
    agg = date_observations_package.get("aggregate_confidence")
    try:
        agg_f = float(agg) if agg is not None else 1.0
    except (TypeError, ValueError):
        agg_f = 1.0
    has_low = agg_f < 0.72
    if not mismatch and not has_low:
        return []

    primary = next((p for p in cols if isinstance(p, dict)), {})
    cname = str(primary.get("column") or "mapped date column")
    tgt = str(primary.get("target_column") or "")
    inferred = str(primary.get("inferred_cadence") or "unknown")
    guided = str(date_observations_package.get("guided_date_granularity") or "").strip() or "not set"
    summary_line = str(date_observations_package.get("summary") or "")[:500]

    preview_bits = [summary_line] if summary_line else []
    if mismatch:
        preview_bits.append(mismatch[:500])
    preview_note = " · ".join(preview_bits)[:900]

    return [
        {
            "question": f"How should the planner treat date spacing for column {cname!r} (target {tgt}) before rollup?",
            "summary": "Date cadence: choose one policy for planning.",
            "options": [
                {
                    "id": "follow_guided_setup",
                    "label": f"Prefer Guided Setup date grain ({guided}).",
                },
                {
                    "id": "follow_auto_signal",
                    "label": f"Prefer automatic sheet signal (~{inferred}, aggregate confidence {agg_f:.0%}).",
                },
                {
                    "id": "analyst_replan_dates",
                    "label": "Neither — I'll describe the correct pattern in notes and want a replan.",
                },
            ],
            "preview_note": preview_note,
            "reviewer_topic": "date_cadence",
        }
    ]
