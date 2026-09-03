"""
Media hierarchy registration — publisher detection, grain inference,
combined-field proposals, and cross-source hierarchy comparison.

Used on Upload before Guided Setup so analysts confirm which publisher
business hierarchy each file sits at, and whether packed columns need splitting.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_CATALOG_CACHE: Optional[Dict[str, Any]] = None
_SAMPLE_ROWS = 80
_PREVIEW_ROWS = 6
_PREVIEW_MAX_COLS = 18

# Native platform headers that still fold onto the shared Config spine.
# Mapping dictionary is the source of truth; this is a last-resort remap if a
# leftover id (insertion_order, line_item) reaches grain inference.
_SPINE_ALIASES = {
    "insertion_order": "campaign",
    "io": "campaign",
    "line_item": "ad_group",
}

_PUBLISHER_DETECT_ORDER_FALLBACK = [
    "amazon_dsp",
    "retail_media",
    "google_ads",
    "dv360",
    "cm360",
    "facebook",
    "tiktok",
    "linkedin",
]

_DEFAULT_DELIMITERS = ["_", " | ", "|", " - ", " / ", "/", " > ", ">"]


def _catalog_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "config" / "media_hierarchies.json"


def load_catalog(force_reload: bool = False) -> Dict[str, Any]:
    """Load publisher hierarchies + standard fields from config."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None and not force_reload:
        return _CATALOG_CACHE
    path = _catalog_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.warning("Could not load media_hierarchies.json: %s", exc)
        data = {
            "standard_fields": [],
            "mapping_dictionary": [],
            "publishers": {},
            "enterprise_hierarchy": {},
            "cross_cut_dimensions": [],
            "detection_order": list(_PUBLISHER_DETECT_ORDER_FALLBACK),
            "combined_field_delimiters": list(_DEFAULT_DELIMITERS),
            "value_token_hints": {},
        }
    _CATALOG_CACHE = data
    return data


def save_catalog(data: Dict[str, Any]) -> Dict[str, Any]:
    """Persist catalog to config/media_hierarchies.json and refresh cache."""
    global _CATALOG_CACHE
    if not isinstance(data, dict):
        raise ValueError("Catalog must be a JSON object")
    required_soft = ("publishers", "standard_fields")
    for key in required_soft:
        if key not in data:
            raise ValueError(f"Catalog missing required key: {key}")
    path = _catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(data)
    out.setdefault("_version", "3.0")
    out.setdefault(
        "_description",
        "Enterprise info defaults + simplified media hierarchy + common attributes.",
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    _CATALOG_CACHE = out
    return out


# Schema Mapping Primary Column targets. IDs and other attributes stay Supporting Meta.
_MAPPING_PRIMARY_METRICS = ("spends", "impressions", "clicks")
_MAPPING_PRIMARY_ALWAYS = ("date", "country", "category", "brand")


def mapping_primary_targets(catalog: Optional[Dict[str, Any]] = None) -> List[str]:
    """Targets that default to Primary Column in Schema Mapping.

    Built from Config, not the target template: enterprise fields that exist
    (Country / Category / Brand and any other required enterprise info), the
    media-hierarchy spine, Date, and Spends / Impressions / Clicks. Everything
    else — including identifier columns — defaults to Supporting Meta.
    """
    cat = catalog or load_catalog()
    out: List[str] = []
    seen = set()

    def add(field_id: Any) -> None:
        fid = str(field_id or "").strip()
        if not fid or fid in seen:
            return
        seen.add(fid)
        out.append(fid)

    ei = cat.get("enterprise_info") or {}
    for field in ei.get("mandatory_fields") or []:
        if isinstance(field, dict):
            add(field.get("id"))
    for fid in _MAPPING_PRIMARY_ALWAYS:
        add(fid)
    for level in media_hierarchy_levels(cat):
        add(level.get("id"))
    for fid in _MAPPING_PRIMARY_METRICS:
        add(fid)
    return out


def media_hierarchy_levels(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Shared grain spine from Config — the same list for every publisher."""
    cat = catalog or load_catalog()
    levels = list(cat.get("media_hierarchy") or [])
    if levels:
        return [
            {
                "level": int(lv.get("level") or i + 1),
                "id": lv.get("id") or _name_slug(lv.get("name")),
                "name": lv.get("name") or lv.get("id"),
                "mandatory": bool(lv.get("mandatory", False)),
            }
            for i, lv in enumerate(levels)
            if isinstance(lv, dict)
        ]
    # Fallback if catalog is older
    return [
        {"level": 1, "id": "publisher", "name": "Publisher", "mandatory": True},
        {"level": 2, "id": "campaign", "name": "Campaign", "mandatory": True},
        {"level": 3, "id": "ad_group", "name": "Ad Group", "mandatory": False},
        {"level": 4, "id": "ad", "name": "Ad", "mandatory": False},
        {"level": 5, "id": "creative", "name": "Creative", "mandatory": False},
    ]


def normalize_option_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """
    Normalize a field option to many-to-one harmonization:
    {standard: str, aliases: [str, ...]}
    Legacy {value, label} and plain strings are accepted.
    """
    if isinstance(raw, dict):
        standard = str(
            raw.get("standard")
            or raw.get("label")
            or raw.get("name")
            or raw.get("value")
            or ""
        ).strip()
        aliases_raw = raw.get("aliases")
        aliases: List[str] = []
        if isinstance(aliases_raw, list):
            aliases = [str(a).strip() for a in aliases_raw if str(a).strip()]
        else:
            # Legacy value/label pair → both become aliases
            for key in ("value", "code", "label", "name"):
                v = str(raw.get(key) or "").strip()
                if v and v not in aliases:
                    aliases.append(v)
        if not standard and aliases:
            standard = aliases[0]
        if not standard:
            return None
        if standard not in aliases:
            aliases = [standard] + aliases
        # de-dupe preserve order (case-insensitive)
        seen = set()
        uniq: List[str] = []
        for a in aliases:
            key = a.lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(a)
        return {"standard": standard, "aliases": uniq, "label": standard, "value": standard}
    s = str(raw or "").strip()
    if not s:
        return None
    return {"standard": s, "aliases": [s], "label": s, "value": s}


def normalize_field_options(raw_options: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in raw_options or []:
        opt = normalize_option_entry(item)
        if opt:
            out.append(opt)
    out.sort(key=lambda o: str(o.get("standard") or o.get("label") or "").lower())
    return out


def enterprise_info_defaults(catalog: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Job-level enterprise info template from config."""
    cat = catalog or load_catalog()
    info = dict(cat.get("enterprise_info") or {})
    fields: List[Dict[str, Any]] = []
    for f in info.get("mandatory_fields") or []:
        if isinstance(f, dict):
            fields.append({
                **f,
                "mandatory": True,
                "enabled": True,
                "options": normalize_field_options(f.get("options")),
            })
    for f in info.get("optional_fields") or []:
        if isinstance(f, dict):
            fields.append({
                **f,
                "mandatory": False,
                "enabled": False,
                "options": normalize_field_options(f.get("options")),
            })

    additional: List[Dict[str, Any]] = []
    for col in info.get("additional_columns") or []:
        if not isinstance(col, dict) or not col.get("id"):
            continue
        additional.append({
            "id": str(col.get("id")),
            "name": str(col.get("name") or col.get("id")),
            "help": str(col.get("help") or ""),
            "default_value": str(col.get("default_value") or col.get("default") or ""),
            "default_source_column": str(col.get("default_source_column") or ""),
            "default_mode": "fixed",
            "enabled": False,
            "mode": "fixed",
            "value": str(col.get("default_value") or col.get("default") or ""),
            "source_column": "",
        })
    # Back-compat: seed advertiser from legacy key if missing from fields AND additional
    has_adv_field = any(str(f.get("id")) == "advertiser" for f in fields)
    if not has_adv_field and not any(c["id"] == "advertiser" for c in additional):
        adv = dict(info.get("advertiser") or {})
        additional.insert(0, {
            "id": "advertiser",
            "name": adv.get("name") or "Advertiser",
            "help": adv.get("help") or "",
            "default_value": str(adv.get("default") or ""),
            "default_source_column": "",
            "default_mode": "fixed",
            "enabled": False,
            "mode": "fixed",
            "value": str(adv.get("default") or ""),
            "source_column": "",
        })

    advertiser_col = next((c for c in additional if c["id"] == "advertiser"), None) or {}
    adv_field = next((f for f in fields if str(f.get("id")) == "advertiser"), None) or {}
    # Show Advertiser + Business Unit in the enterprise grid by default
    enabled_optional = [
        str(f["id"]) for f in fields
        if not f.get("mandatory") and str(f.get("id")) in ("advertiser", "business_unit")
    ]
    return {
        "fields": fields,
        "values": {str(f["id"]): str(f.get("default") or "") for f in fields if f.get("id")},
        "additional_columns": additional,
        "advertiser": {
            "mode": advertiser_col.get("mode") or "fixed_or_column",
            "value": str(
                advertiser_col.get("value")
                or adv_field.get("default")
                or ""
            ),
            "source_column": str(advertiser_col.get("source_column") or ""),
            "name": advertiser_col.get("name") or adv_field.get("name") or "Advertiser",
        },
        "enabled_optional_ids": enabled_optional,
    }


def catalog_for_api() -> Dict[str, Any]:
    """Public catalog payload for GET /api/hierarchy/catalog."""
    cat = load_catalog()
    publishers = cat.get("publishers") or {}
    return {
        "standard_fields": list(cat.get("standard_fields") or []),
        "enterprise_info": dict(cat.get("enterprise_info") or {}),
        "enterprise_info_defaults": enterprise_info_defaults(cat),
        "media_hierarchy": media_hierarchy_levels(cat),
        "common_attributes": list(cat.get("common_attributes") or []),
        "metrics": list(cat.get("metrics") or []),
        # Legacy keys kept for Settings editor compatibility
        "enterprise_hierarchy": dict(cat.get("enterprise_hierarchy") or {}),
        "cross_cut_dimensions": list(cat.get("cross_cut_dimensions") or []),
        "publishers": [
            {
                "id": pub.get("id") or pid,
                "name": pub.get("name") or pid,
                "aliases": list(pub.get("aliases") or []),
                "hierarchy": list(pub.get("hierarchy") or media_hierarchy_levels(cat)),
                "dimensions": list(pub.get("dimensions") or []),
                "metrics": list(pub.get("metrics") or []),
            }
            for pid, pub in publishers.items()
            if isinstance(pub, dict)
        ],
        "mapping_dictionary": list(cat.get("mapping_dictionary") or []),
        "detection_order": list(cat.get("detection_order") or _PUBLISHER_DETECT_ORDER_FALLBACK),
        "combined_field_delimiters": list(cat.get("combined_field_delimiters") or _DEFAULT_DELIMITERS),
        "value_token_hints": dict(cat.get("value_token_hints") or {}),
        "_version": cat.get("_version"),
        "_description": cat.get("_description"),
    }


_KEY_SEPARATORS = re.compile(r"[\s_\-/|>.,;:()\[\]{}]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_key(value: Any) -> str:
    """Whole-name key for alias lookup.

    ``orderName``, ``order_name`` and ``order name`` are the same name because
    separators and case are ignored — not because camelCase is split into
    tokens. Splitting ``intervalStart`` into ``interval`` + ``Start`` would
    invent words that are not in the header.
    """
    return _NON_ALNUM.sub("", str(value or "").lower())


def _name_slug(value: Any) -> str:
    """Separator-based id fallback (``Ad Group`` → ``ad_group``)."""
    return "_".join(_KEY_SEPARATORS.sub(" ", str(value or "").lower()).split())


def _date_role_from_visible_words(name: Any) -> str:
    """Date role from words already separated in *name* — never camelCase pieces."""
    tokens = [t for t in _KEY_SEPARATORS.split(str(name or "").lower()) if t]
    if any(t in {"start", "from", "begin"} for t in tokens):
        return "range_start"
    if any(t in {"end", "to", "until", "finish"} for t in tokens):
        return "range_end"
    if "year" in tokens:
        return "part_year"
    if "month" in tokens:
        return "part_month"
    if "day" in tokens:
        return "part_day"
    if "quarter" in tokens or any(t in {"q1", "q2", "q3", "q4"} for t in tokens):
        return "part_quarter"
    return ""


def suggest_date_semantic(source_col: str, catalog: Optional[Dict[str, Any]] = None) -> str:
    """Date role for a header, using the whole name only.

    ``intervalStart`` is Range start because Config has the alias
    ``interval start`` (same compact name). The letters ``Start`` inside the
    camelCase header are not read as their own word.
    """
    cat = catalog or load_catalog()
    key = normalize_key(source_col)
    if not key:
        return ""
    for entry in cat.get("mapping_dictionary") or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "")
        if normalize_key(source) != key:
            continue
        role = _date_role_from_visible_words(source)
        if role:
            return role
    return _date_role_from_visible_words(source_col)


def _column_alias_index(catalog: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Normalized column name → target. First writer wins, most specific first."""
    index: Dict[str, Dict[str, Any]] = {}

    def add(name: Any, target: Any, confidence: int, reason: str) -> None:
        key = normalize_key(name)
        target_id = str(target or "").strip()
        if key and target_id and key not in index:
            index[key] = {"target": target_id, "confidence": confidence, "reason": reason}

    for entry in catalog.get("mapping_dictionary") or []:
        if isinstance(entry, dict):
            add(entry.get("source"), entry.get("target"), 98, "Column alias")

    for field in catalog.get("standard_fields") or []:
        if isinstance(field, dict):
            add(field.get("label"), field.get("id"), 94, "Standard column name")
            add(field.get("id"), field.get("id"), 94, "Standard column name")

    for level in media_hierarchy_levels(catalog):
        add(level.get("name"), level.get("id"), 92, "Hierarchy level name")
        add(level.get("id"), level.get("id"), 92, "Hierarchy level name")

    for attr in catalog.get("common_attributes") or []:
        if isinstance(attr, dict):
            add(attr.get("name"), attr.get("id"), 90, "Attribute name")
            add(attr.get("id"), attr.get("id"), 90, "Attribute name")

    for metric in catalog.get("metrics") or []:
        if isinstance(metric, dict):
            add(metric.get("name"), metric.get("id"), 94, "Metric name")
            add(metric.get("id"), metric.get("id"), 94, "Metric name")
            for alias in metric.get("aliases") or []:
                add(alias, metric.get("id"), 92, "Metric alias")

    # Headers an analyst already confirmed count the same as configured aliases.
    try:
        from .learned_mappings import learned_column_aliases

        for target, aliases in learned_column_aliases().items():
            for alias in aliases:
                add(alias, target, 90, "Learned from a confirmed mapping")
    except Exception as exc:
        logger.warning("Could not load learned column aliases: %s", exc)

    return index


def suggest_column_target(source_col: str, catalog: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Map a source column to a standard field by whole-name alias match.

    Deliberately exact: a header counts only when its full name matches an alias
    (separators and case ignored). Substring matching used to read
    ``orderCurrency`` as Campaign (via ``order``) and
    ``actions.onsite_conversion`` as Publisher (via ``site``). Unmatched
    columns are left for the analyst rather than guessed at.
    """
    cat = catalog or load_catalog()
    key = normalize_key(source_col)
    hit = _column_alias_index(cat).get(key)
    if hit:
        return dict(hit)
    return {"target": None, "confidence": 0, "reason": "No alias match — map it in Schema Mapping"}


def detect_publisher(
    file_name: str,
    columns: Optional[Sequence[str]] = None,
    sample_values: Optional[Sequence[str]] = None,
    catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Detect publisher from filename aliases first, then column/value evidence.
    Never silently defaults to a publisher — returns null when unknown.
    """
    cat = catalog or load_catalog()
    pubs = cat.get("publishers") or {}
    order = list(cat.get("detection_order") or _PUBLISHER_DETECT_ORDER_FALLBACK)
    name_l = str(file_name or "").lower()

    for pid in order:
        pub = pubs.get(pid)
        if not isinstance(pub, dict):
            continue
        aliases = [str(a).lower() for a in (pub.get("aliases") or [])]
        compact_id = pid.replace("_", "")
        if any(a and a in name_l for a in aliases) or compact_id in name_l.replace("_", "").replace("-", "").replace(" ", ""):
            return {
                "publisher_id": pid,
                "publisher_name": pub.get("name") or pid,
                "confidence": 92,
                "reason": "Filename alias match",
            }

    # Soft filename fallbacks (still evidence-based, not DV360 default)
    if "amazon" in name_l and "retail" not in name_l:
        pub = pubs.get("amazon_dsp") or {}
        return {
            "publisher_id": "amazon_dsp",
            "publisher_name": pub.get("name") or "Amazon DSP",
            "confidence": 70,
            "reason": "Filename contains amazon",
        }
    if "google" in name_l:
        pub = pubs.get("google_ads") or {}
        return {
            "publisher_id": "google_ads",
            "publisher_name": pub.get("name") or "Google Ads",
            "confidence": 70,
            "reason": "Filename contains google",
        }

    # Column / value evidence
    blob_parts = [str(c) for c in (columns or [])]
    blob_parts.extend(str(v) for v in (sample_values or [])[:40])
    blob = " ".join(blob_parts).lower()
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for pid in order:
        pub = pubs.get(pid)
        if not isinstance(pub, dict):
            continue
        score = 0
        for alias in pub.get("aliases") or []:
            a = str(alias).lower()
            if a and a in blob:
                score += 2 if len(a) > 3 else 1
        if score > best_score:
            best_score = score
            best = {
                "publisher_id": pid,
                "publisher_name": pub.get("name") or pid,
                "confidence": min(88, 50 + score * 12),
                "reason": "Column/value evidence",
            }
    if best and best_score > 0:
        return best

    return {
        "publisher_id": None,
        "publisher_name": None,
        "confidence": 0,
        "reason": "Unknown — analyst must select publisher",
    }


def infer_grain(
    publisher_id: Optional[str] = None,
    columns: Sequence[str] = (),
    catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Infer the deepest Config spine level present in *this file's* headers.

    Publisher is not an input. Every source uses the same ``media_hierarchy``
    list from Config; grain is whichever of those levels the columns actually
    contain. If none of Campaign / Ad Group / Ad / Creative (or their aliases)
    are present, grain defaults to Publisher. ``publisher_id`` is accepted only
    so older callers keep working.
    """
    cat = catalog or load_catalog()
    levels = media_hierarchy_levels(cat)
    level_by_id = {lv["id"]: (idx, lv) for idx, lv in enumerate(levels)}
    matched: List[Dict[str, Any]] = []
    deepest_idx = -1
    deepest_level = None

    for col in columns:
        sug = suggest_column_target(col, cat)
        target = str(sug.get("target") or "")
        target = _SPINE_ALIASES.get(target, target)
        hit = level_by_id.get(target)
        if hit is None:
            continue
        idx, level = hit
        matched.append({
            "source_column": col,
            "target": level["id"],
            "level": level["level"],
            "level_name": level["name"],
            "confidence": sug.get("confidence"),
            "reason": sug.get("reason"),
        })
        if idx > deepest_idx:
            deepest_idx = idx
            deepest_level = level

    if deepest_level is None:
        fallback_idx, fallback = next(
            ((idx, lv) for idx, lv in enumerate(levels) if lv.get("id") == "publisher"),
            (0, levels[0] if levels else None),
        )
        if fallback is None:
            return {
                "grain_level": None,
                "grain_level_index": None,
                "grain_level_id": None,
                "grain_level_name": None,
                "detected_hierarchy_columns": matched,
                "confidence": 0,
                "reason": "No hierarchy columns found — pick the grain from the preview",
                "hierarchy": levels,
            }
        return {
            "grain_level": fallback["level"],
            "grain_level_index": fallback_idx,
            "grain_level_id": fallback["id"],
            "grain_level_name": fallback["name"],
            "detected_hierarchy_columns": matched,
            "confidence": 40,
            "reason": "No Campaign / Ad Group / Ad / Creative column found — defaulting to Publisher",
            "hierarchy": levels,
        }

    return {
        "grain_level": deepest_level["level"],
        "grain_level_index": deepest_idx,
        "grain_level_id": deepest_level["id"],
        "grain_level_name": deepest_level["name"],
        "detected_hierarchy_columns": matched,
        "confidence": 85 if len(matched) >= 2 else 70,
        "reason": f"Deepest hierarchy column present: {deepest_level['name']}",
        "hierarchy": levels,
    }


def _cell_preview(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in ("nan", "none", "nat"):
        return ""
    if len(text) > 80:
        return text[:77] + "…"
    return text


def sample_preview(
    columns: Sequence[str],
    sample_df: Optional[pd.DataFrame],
    detected_hierarchy_columns: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compact header + row sample for the Upload grain picker."""
    roles = {
        str(row.get("source_column")): str(row.get("target") or "")
        for row in (detected_hierarchy_columns or [])
        if isinstance(row, dict) and row.get("source_column")
    }
    headers = [str(c) for c in (columns or [])]
    total_columns = len(headers)
    if len(headers) > _PREVIEW_MAX_COLS:
        keep: List[str] = []
        seen = set()
        for h in headers:
            if h in roles and h not in seen:
                keep.append(h)
                seen.add(h)
        for h in headers:
            if len(keep) >= _PREVIEW_MAX_COLS:
                break
            if h not in seen:
                keep.append(h)
                seen.add(h)
        order = {h: i for i, h in enumerate(headers)}
        headers = sorted(keep, key=lambda h: order.get(h, 10_000))

    rows: List[List[str]] = []
    sampled = 0
    if sample_df is not None and not sample_df.empty:
        sampled = int(len(sample_df))
        present = [h for h in headers if h in sample_df.columns]
        subset = sample_df.loc[:, present]
        for _, row in subset.head(_PREVIEW_ROWS).iterrows():
            rows.append([_cell_preview(row[h]) if h in subset.columns else "" for h in headers])

    return {
        "headers": headers,
        "rows": rows,
        "column_roles": {h: roles[h] for h in headers if h in roles},
        "truncated": total_columns > len(headers),
        "total_columns": total_columns,
        "sampled_rows": sampled,
        "shown_rows": len(rows),
    }


def _load_value_token_map(catalog: Dict[str, Any]) -> Dict[str, str]:
    """Token → field id from closed value lists, then field labels as fallback."""
    from sia.agent.field_mapping_policy import build_value_token_map

    token_map = build_value_token_map(catalog)
    hints = catalog.get("value_token_hints") or {}
    for dim, tokens in hints.items():
        for tok in tokens or []:
            token_map.setdefault(str(tok).strip().lower(), str(dim))
    for field in catalog.get("standard_fields") or []:
        fid = str(field.get("id") or "")
        if field.get("type") in ("dimension", "hierarchy") and fid:
            token_map.setdefault(fid.replace("_", " ").lower(), fid)
            token_map.setdefault(str(field.get("label") or "").lower(), fid)
    return token_map


def _pick_delimiter(values: Sequence[str], delimiters: Sequence[str]) -> Optional[Tuple[str, float, int]]:
    """Return (delimiter, stability_score, typical_parts) for values that look packed."""
    best = None
    for delim in delimiters:
        part_counts: List[int] = []
        for raw in values:
            s = str(raw or "").strip()
            if not s or s.lower() in ("nan", "none", ""):
                continue
            if delim not in s:
                continue
            parts = [p.strip() for p in s.split(delim) if p.strip()]
            if len(parts) >= 2:
                part_counts.append(len(parts))
        if len(part_counts) < max(2, int(len(values) * 0.4)):
            continue
        # Stability: most common part count share
        from collections import Counter
        ctr = Counter(part_counts)
        mode_n, mode_count = ctr.most_common(1)[0]
        stability = mode_count / len(part_counts)
        if mode_n < 2 or stability < 0.55:
            continue
        score = stability * len(part_counts)
        if best is None or score > best[1]:
            best = (delim, stability, mode_n)
    return best


def _classify_tokens(
    sample_values: Sequence[str],
    delimiter: str,
    n_parts: int,
    token_map: Dict[str, str],
    standard_fields: Sequence[Dict[str, Any]],
    extra_field_ids: Optional[Sequence[str]] = None,
) -> List[str]:
    """Propose a target dimension per split position from value aliases only.

    A part is labelled only when its token is in ``token_map``. Unused Master
    List dimensions are never poured into leftover slots — a duplicate ALPRO
    after Brand stays blank, not Region.
    """
    dim_fields = [
        f for f in standard_fields
        if f.get("type") in ("dimension", "hierarchy") and f.get("id")
    ]
    field_ids = {str(f.get("id")) for f in dim_fields}
    field_ids.update(str(x).strip() for x in (extra_field_ids or []) if str(x).strip())
    position_votes: List[Dict[str, int]] = [dict() for _ in range(n_parts)]

    for raw in sample_values:
        s = str(raw or "").strip()
        if not s or delimiter not in s:
            continue
        parts = [p.strip() for p in s.split(delimiter, n_parts - 1)]
        while len(parts) < n_parts:
            parts.append("")
        for i, part in enumerate(parts[:n_parts]):
            key = part.lower()
            if not key or key not in token_map:
                continue
            dim = token_map[key]
            if dim in field_ids:
                position_votes[i][dim] = position_votes[i].get(dim, 0) + 1

    # One label per position: the top alias vote there. If that dimension is
    # already taken, leave the part blank. Do not fall through to a weaker
    # leftover (ALPRO + ALL at the same slot must not become Region).
    best: List[tuple] = []
    for i, votes in enumerate(position_votes):
        if not votes:
            continue
        dim, score = max(votes.items(), key=lambda kv: (kv[1], kv[0]))
        best.append((score, i, dim))
    best.sort(key=lambda c: (-c[0], c[1], c[2]))

    targets: List[str] = [""] * n_parts
    used = set()
    for _score, i, dim in best:
        if dim not in used:
            targets[i] = dim
            used.add(dim)
    return targets


def detect_combined_fields(
    df_sample: Optional[pd.DataFrame],
    columns: Optional[Sequence[str]] = None,
    catalog: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Detect columns whose sample values look packed with a stable delimiter."""
    cat = catalog or load_catalog()
    delimiters = list(cat.get("combined_field_delimiters") or _DEFAULT_DELIMITERS)
    token_map = _load_value_token_map(cat)
    standard_fields = list(cat.get("standard_fields") or [])
    results: List[Dict[str, Any]] = []

    if df_sample is None or df_sample.empty:
        return results

    cols = list(columns) if columns else [str(c) for c in df_sample.columns]
    for col in cols:
        if col not in df_sample.columns:
            continue
        series = df_sample[col].dropna().astype(str)
        # Skip mostly-numeric columns
        numericish = 0
        samples: List[str] = []
        for val in series.head(40):
            s = str(val).strip()
            if not s or s.lower() in ("nan", "none"):
                continue
            samples.append(s)
            try:
                float(s.replace(",", ""))
                numericish += 1
            except ValueError:
                pass
        if len(samples) < 3:
            continue
        if numericish / max(len(samples), 1) > 0.7:
            continue
        # Skip columns that already map cleanly to a single metric/date
        sug = suggest_column_target(col, cat)
        if sug.get("target") in ("date", "spend", "spends", "impressions", "clicks", "video_views", "roas") and sug.get("confidence", 0) >= 86:
            continue

        picked = _pick_delimiter(samples, delimiters)
        if not picked:
            continue
        delim, stability, n_parts = picked
        targets = _classify_tokens(
            samples,
            delim,
            n_parts,
            token_map,
            standard_fields,
            extra_field_ids=[
                str(a.get("id") or "")
                for a in (cat.get("common_attributes") or [])
                if isinstance(a, dict)
            ],
        )
        # Require at least 2 distinct proposed dimensions
        distinct = [t for t in targets if t]
        if len(set(distinct)) < 2:
            continue
        confidence = int(round(min(95, 55 + stability * 40)))
        part_samples: List[str] = [""] * n_parts
        for raw in samples:
            if delim not in raw:
                continue
            bits = [p.strip() for p in raw.split(delim, n_parts - 1)]
            while len(bits) < n_parts:
                bits.append("")
            for i, bit in enumerate(bits[:n_parts]):
                if bit and not part_samples[i]:
                    part_samples[i] = bit
            if all(part_samples):
                break
        parts = []
        for i in range(n_parts):
            parts.append({
                "index": i,
                "sample": part_samples[i],
                "combine_with_previous": False,
                "target": (targets[i] if i < len(targets) else "") or "",
                "custom_name": "",
            })
        results.append({
            "source_column": col,
            "delimiter": delim,
            "part_count": n_parts,
            "target_dimensions": targets,
            "part_samples": part_samples,
            "parts": parts,
            "samples": samples[:6],
            "confidence": confidence,
            "accepted": confidence >= 75,
            "single_dimension": False,
            "single_target": "",
            "single_custom_name": "",
            "reason": f"Stable delimiter {delim!r} across {stability:.0%} of samples ({n_parts} parts)",
        })
    return results


def compare_hierarchies(
    registrations: Sequence[Dict[str, Any]],
    template_uid_hierarchy: Optional[Sequence[str]] = None,
    catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compare grain levels across sources and vs template uid_hierarchy.
    Soft warnings only — analyst may acknowledge mixed grain.
    """
    cat = catalog or load_catalog()
    grains: List[Dict[str, Any]] = []
    for reg in registrations:
        if not isinstance(reg, dict):
            continue
        sid = reg.get("source_id")
        if not sid:
            continue
        grains.append({
            "source_id": sid,
            "file_name": reg.get("file_name"),
            "sheet_name": reg.get("sheet_name"),
            "publisher_id": reg.get("publisher_id"),
            "publisher_name": reg.get("publisher_name"),
            "grain_level": reg.get("grain_level"),
            "grain_level_id": reg.get("grain_level_id"),
            "grain_level_name": reg.get("grain_level_name"),
            "grain_level_index": reg.get("grain_level_index"),
        })

    warnings: List[Dict[str, Any]] = []
    grain_keys = {
        (g.get("publisher_id"), g.get("grain_level_id") or g.get("grain_level_name"))
        for g in grains
        if g.get("grain_level_id") or g.get("grain_level_name")
    }
    mixed = len({k[1] for k in grain_keys if k[1]}) > 1

    if mixed:
        detail_parts = []
        for g in grains:
            label = g.get("file_name") or g.get("source_id")
            gname = g.get("grain_level_name") or "unset"
            gl = g.get("grain_level")
            lvl = f"L{gl}" if gl is not None else "?"
            pub = g.get("publisher_name") or g.get("publisher_id") or "Unknown"
            detail_parts.append(f"{label} ({pub}) is {gname} ({lvl})")
        warnings.append({
            "code": "mixed_grain",
            "severity": "warning",
            "message": (
                "Sources sit at different hierarchy levels. "
                "Stacking without rollup can double-count. "
                + "; ".join(detail_parts)
            ),
            "sources": [g.get("source_id") for g in grains],
        })

    # Missing mandatory levels above declared grain, from the shared Config spine
    levels = media_hierarchy_levels(cat)
    for g in grains:
        idx = g.get("grain_level_index")
        if idx is None:
            continue
        try:
            idx_i = int(idx)
        except (TypeError, ValueError):
            continue
        detected = {
            str(c.get("target"))
            for c in (next((r.get("detected_hierarchy_columns") or [] for r in registrations if r.get("source_id") == g["source_id"]), []))
        }
        absent = [lv["name"] for lv in levels[:idx_i] if lv.get("mandatory") and lv["id"] not in detected]
        if absent:
            warnings.append({
                "code": "missing_mandatory_levels",
                "severity": "info",
                "message": (
                    f"{g.get('file_name') or g.get('source_id')}: "
                    f"grain is {g.get('grain_level_name')} but columns for "
                    f"{', '.join(absent)} were not detected."
                ),
                "sources": [g.get("source_id")],
            })

    # Template uid_hierarchy is output grain — note mismatch conceptually
    tpl = [str(x) for x in (template_uid_hierarchy or []) if x]
    if tpl and grains:
        grain_ids = {g.get("grain_level_id") for g in grains if g.get("grain_level_id")}
        # Soft note when publisher entity grain is finer than template UID (common, not an error)
        entity_ids = {"advertiser", "campaign", "insertion_order", "line_item", "ad_group", "creative"}
        if grain_ids & entity_ids and not (set(tpl) & entity_ids):
            warnings.append({
                "code": "template_output_grain",
                "severity": "info",
                "message": (
                    f"Target template uid_hierarchy is {tpl} (output grain). "
                    "Registered publisher entity levels are finer — confirm rollup/defaults during mapping."
                ),
                "sources": [g.get("source_id") for g in grains],
            })

    status = "mixed" if mixed else ("aligned" if grains else "empty")
    return {
        "status": status,
        "mixed_grain": mixed,
        "requires_mixed_grain_ack": mixed,
        "grains": grains,
        "warnings": warnings,
        "template_uid_hierarchy": tpl,
    }


def read_source_sample(
    file_path: str,
    sheet_name: Optional[str] = None,
    max_rows: int = _SAMPLE_ROWS,
) -> Tuple[List[str], pd.DataFrame, List[str]]:
    """
    Read a bounded raw sample. Heuristic: use first non-empty row as header
    when row 0 looks like a header; otherwise treat first row as header via pandas.
    Returns (columns, sample_df with named columns, flat sample values).
    """
    path = Path(file_path)
    if not path.exists():
        return [], pd.DataFrame(), []

    try:
        if str(path).lower().endswith(".csv"):
            raw = pd.read_csv(path, header=None, nrows=max_rows + 5)
        else:
            raw = pd.read_excel(path, sheet_name=sheet_name or 0, header=None, nrows=max_rows + 5)
    except Exception as exc:
        logger.warning("Failed to sample %s [%s]: %s", file_path, sheet_name, exc)
        return [], pd.DataFrame(), []

    if raw.empty:
        return [], pd.DataFrame(), []

    # Find first row that looks like headers (mostly strings, few numbers)
    header_idx = 0
    for i in range(min(15, len(raw))):
        row = raw.iloc[i]
        non_null = [v for v in row.tolist() if pd.notna(v) and str(v).strip()]
        if len(non_null) < 2:
            continue
        numeric = 0
        for v in non_null:
            try:
                float(str(v).replace(",", ""))
                numeric += 1
            except ValueError:
                pass
        if numeric / len(non_null) < 0.4:
            header_idx = i
            break

    headers = []
    seen: Dict[str, int] = {}
    for v in raw.iloc[header_idx].tolist():
        name = str(v).strip() if pd.notna(v) else ""
        if not name or name.lower() == "nan":
            name = f"Column_{len(headers)+1}"
        base = name
        if base in seen:
            seen[base] += 1
            name = f"{base}_{seen[base]}"
        else:
            seen[base] = 0
        headers.append(name)

    body = raw.iloc[header_idx + 1 : header_idx + 1 + max_rows].copy()
    body.columns = headers[: body.shape[1]]
    if body.shape[1] < len(headers):
        headers = headers[: body.shape[1]]
    body = body.iloc[:, : len(headers)]
    body.columns = headers

    sample_values: List[str] = []
    for col in headers:
        if col not in body.columns:
            continue
        for v in body[col].head(8).tolist():
            if pd.notna(v):
                sample_values.append(str(v).strip())

    return headers, body, sample_values


def _is_main_data_source(source: Dict[str, Any]) -> bool:
    raw = source.get("contains_main_data")
    if raw is None:
        return True
    return bool(raw)


def propose_for_source(
    source: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
    catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a hierarchy registration proposal for one source_registry row."""
    cat = catalog or load_catalog()
    file_path = str(source.get("file_path") or "")
    file_name = str(source.get("file_name") or Path(file_path).name)
    sheet_name = source.get("sheet_name")
    source_id = str(source.get("source_id") or "")

    columns, sample_df, sample_values = read_source_sample(file_path, sheet_name)
    pub_det = detect_publisher(file_name, columns, sample_values, cat)

    # Prefer existing analyst choices
    publisher_id = None
    publisher_name = None
    if existing and existing.get("publisher_id"):
        publisher_id = existing.get("publisher_id")
        publisher_name = existing.get("publisher_name")
        pub_det = {
            **pub_det,
            "publisher_id": publisher_id,
            "publisher_name": publisher_name,
            "confidence": 99,
            "reason": existing.get("publisher_reason") or "Analyst override",
        }
    else:
        publisher_id = pub_det.get("publisher_id")
        publisher_name = pub_det.get("publisher_name")

    grain = infer_grain(publisher_id, columns, cat)
    if existing and (existing.get("grain_level_id") or existing.get("grain_level") is not None):
        grain = {
            **grain,
            "grain_level": existing.get("grain_level", grain.get("grain_level")),
            "grain_level_index": existing.get("grain_level_index", grain.get("grain_level_index")),
            "grain_level_id": existing.get("grain_level_id", grain.get("grain_level_id")),
            "grain_level_name": existing.get("grain_level_name", grain.get("grain_level_name")),
            "confidence": 99,
            "reason": existing.get("grain_reason") or "Analyst override",
        }

    combined = detect_combined_fields(sample_df, columns, cat)
    # Combined fields are proposed for Column Standardization (post-layout), not Upload registration
    hierarchy = list(grain.get("hierarchy") or media_hierarchy_levels(cat))
    preview = sample_preview(columns, sample_df, grain.get("detected_hierarchy_columns") or [])

    registered = bool(
        publisher_id
        and (grain.get("grain_level_id") or grain.get("grain_level") is not None)
    )

    return {
        "source_id": source_id,
        "file_id": source.get("file_id"),
        "file_name": file_name,
        "file_path": file_path,
        "sheet_name": sheet_name,
        "contains_main_data": _is_main_data_source(source),
        "columns": columns,
        "publisher_id": publisher_id,
        "publisher_name": publisher_name,
        "publisher_confidence": pub_det.get("confidence"),
        "publisher_reason": pub_det.get("reason"),
        "hierarchy": hierarchy,
        "grain_level": grain.get("grain_level"),
        "grain_level_index": grain.get("grain_level_index"),
        "grain_level_id": grain.get("grain_level_id"),
        "grain_level_name": grain.get("grain_level_name"),
        "grain_confidence": grain.get("confidence"),
        "grain_reason": grain.get("reason"),
        "detected_hierarchy_columns": grain.get("detected_hierarchy_columns") or [],
        "preview": preview,
        "combined_field_proposals": combined,
        "registered": registered,
        "analyst_notes": (existing or {}).get("analyst_notes") or "",
        "comparison_status": (existing or {}).get("comparison_status") or "",
    }


def propose_for_job(
    job: Dict[str, Any],
    template_uid_hierarchy: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Propose hierarchy registration for all sources on a job."""
    cat = load_catalog()
    existing_list = list(job.get("hierarchy_registry") or [])
    existing_by_id = {
        str(r.get("source_id")): r
        for r in existing_list
        if isinstance(r, dict) and r.get("source_id")
    }

    proposals: List[Dict[str, Any]] = []
    for source in job.get("source_registry") or []:
        if not isinstance(source, dict) or not source.get("source_id"):
            continue
        if not _is_main_data_source(source):
            continue
        sid = str(source.get("source_id"))
        proposals.append(propose_for_source(source, existing_by_id.get(sid), cat))

    comparison = compare_hierarchies(proposals, template_uid_hierarchy, cat)
    unregistered = [p for p in proposals if not p.get("registered")]
    publishers = sorted({
        p.get("publisher_id")
        for p in proposals
        if p.get("publisher_id")
    })

    enterprise = job.get("enterprise_info")
    if not isinstance(enterprise, dict) or not enterprise.get("values"):
        enterprise = enterprise_info_defaults(cat)
    else:
        # Merge missing default field defs
        defaults = enterprise_info_defaults(cat)
        enterprise = {
            **defaults,
            **enterprise,
            "values": {**defaults.get("values", {}), **(enterprise.get("values") or {})},
            "advertiser": {**defaults.get("advertiser", {}), **(enterprise.get("advertiser") or {})},
            "fields": enterprise.get("fields") or defaults.get("fields"),
            "enabled_optional_ids": list(enterprise.get("enabled_optional_ids") or []),
        }

    return {
        "sources": proposals,
        "enterprise_info": enterprise,
        "media_hierarchy": media_hierarchy_levels(cat),
        "common_attributes": list(cat.get("common_attributes") or []),
        "comparison": comparison,
        "kpis": {
            "files": len({p.get("file_id") or p.get("file_name") for p in proposals}),
            "sources": len(proposals),
            "publishers_detected": len(publishers),
            "unregistered": len(unregistered),
            "mixed_grain": bool(comparison.get("mixed_grain")),
        },
        "hierarchy_register_complete": bool(job.get("hierarchy_register_complete")),
        "mixed_grain_acknowledged": bool(job.get("mixed_grain_acknowledged")),
    }


def _enterprise_info_complete(enterprise_info: Optional[Dict[str, Any]], catalog: Optional[Dict[str, Any]] = None) -> List[str]:
    """Validate job-level enterprise info mandatory fields."""
    cat = catalog or load_catalog()
    defaults = enterprise_info_defaults(cat)
    info = enterprise_info if isinstance(enterprise_info, dict) else {}
    values = dict(info.get("values") or {})
    reasons = []
    for field in defaults.get("fields") or []:
        if not field.get("mandatory"):
            continue
        fid = str(field.get("id") or "")
        if not str(values.get(fid) or "").strip():
            reasons.append(f"Enterprise info: {field.get('name') or fid} is required")
    return reasons


def registration_is_complete(
    hierarchy_registry: Sequence[Dict[str, Any]],
    source_registry: Sequence[Dict[str, Any]],
    mixed_grain_acknowledged: bool = False,
    catalog: Optional[Dict[str, Any]] = None,
    enterprise_info: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[str]]:
    """Return (complete, reasons) for whether Guided Setup may proceed."""
    reasons: List[str] = []
    reasons.extend(_enterprise_info_complete(enterprise_info, catalog))
    by_id = {
        str(r.get("source_id")): r
        for r in (hierarchy_registry or [])
        if isinstance(r, dict) and r.get("source_id")
    }
    main_sources = [
        s for s in (source_registry or [])
        if isinstance(s, dict) and s.get("source_id") and _is_main_data_source(s)
    ]
    if not main_sources:
        return False, reasons + ["No main-data sources to register"]

    for source in main_sources:
        sid = str(source.get("source_id"))
        reg = by_id.get(sid)
        if not reg:
            reasons.append(f"{sid}: not registered")
            continue
        if not reg.get("publisher_id"):
            reasons.append(f"{sid}: publisher required")
        if reg.get("grain_level_id") is None and reg.get("grain_level") is None:
            reasons.append(f"{sid}: grain level required")

    comparison = compare_hierarchies(list(by_id.values()), catalog=catalog)
    if comparison.get("requires_mixed_grain_ack") and not mixed_grain_acknowledged:
        reasons.append("Mixed hierarchy grain — acknowledge before continuing")

    return (len(reasons) == 0), reasons


def normalize_registration_payload(
    entries: Sequence[Dict[str, Any]],
    job: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Sanitize POST body into hierarchy_registry rows."""
    source_by_id = {
        str(s.get("source_id")): s
        for s in (job.get("source_registry") or [])
        if isinstance(s, dict) and s.get("source_id")
    }
    cat = load_catalog()
    pubs = cat.get("publishers") or {}
    out: List[Dict[str, Any]] = []

    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("source_id") or "")
        if not sid or sid not in source_by_id:
            continue
        source = source_by_id[sid]
        publisher_id = raw.get("publisher_id") or None
        if publisher_id == "" or publisher_id == "unknown":
            publisher_id = None
        pub = pubs.get(publisher_id) if publisher_id else None
        publisher_name = raw.get("publisher_name")
        if not publisher_name and isinstance(pub, dict):
            publisher_name = pub.get("name")
        if publisher_id == "custom":
            publisher_name = publisher_name or "Custom"
        hierarchy = media_hierarchy_levels(cat)

        grain_level_id = raw.get("grain_level_id")
        grain_level = raw.get("grain_level")
        grain_level_index = raw.get("grain_level_index")
        grain_level_name = raw.get("grain_level_name")
        if grain_level_id and hierarchy:
            for idx, lv in enumerate(hierarchy):
                if lv["id"] == grain_level_id or normalize_key(lv["name"]) == normalize_key(grain_level_id):
                    grain_level_index = idx
                    grain_level = lv["level"]
                    grain_level_name = lv["name"]
                    grain_level_id = lv["id"]
                    break
        elif grain_level is not None and hierarchy:
            try:
                gl = int(grain_level)
            except (TypeError, ValueError):
                gl = None
            if gl is not None:
                for idx, lv in enumerate(hierarchy):
                    if lv["level"] == gl:
                        grain_level_index = idx
                        grain_level_id = lv["id"]
                        grain_level_name = lv["name"]
                        break

        out.append({
            "source_id": sid,
            "file_id": source.get("file_id"),
            "file_name": source.get("file_name"),
            "file_path": source.get("file_path"),
            "sheet_name": source.get("sheet_name"),
            "publisher_id": publisher_id,
            "publisher_name": publisher_name,
            "publisher_confidence": raw.get("publisher_confidence"),
            "publisher_reason": raw.get("publisher_reason") or "",
            "hierarchy": hierarchy,
            "grain_level": grain_level,
            "grain_level_index": grain_level_index,
            "grain_level_id": grain_level_id,
            "grain_level_name": grain_level_name,
            "grain_confidence": raw.get("grain_confidence"),
            "grain_reason": raw.get("grain_reason") or "",
            "detected_hierarchy_columns": list(raw.get("detected_hierarchy_columns") or []),
            "analyst_notes": str(raw.get("analyst_notes") or ""),
            "comparison_status": str(raw.get("comparison_status") or ""),
            "registered": bool(
                publisher_id
                and (grain_level_id is not None or grain_level is not None)
            ),
        })
    return out


def normalize_enterprise_info(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Sanitize job-level enterprise_info payload."""
    defaults = enterprise_info_defaults()
    info = raw if isinstance(raw, dict) else {}
    values = dict(defaults.get("values") or {})
    incoming = info.get("values") if isinstance(info.get("values"), dict) else {}
    for k, v in incoming.items():
        values[str(k)] = str(v or "").strip()
    enabled = [
        str(x) for x in (info.get("enabled_optional_ids") or [])
        if str(x).strip()
    ]
    for f in defaults.get("fields") or []:
        if f.get("mandatory") and f.get("id") and f["id"] not in enabled:
            enabled.append(f["id"])

    # Additional columns (Advertiser, Publisher, …)
    by_id = {
        str(c.get("id")): dict(c)
        for c in (defaults.get("additional_columns") or [])
        if isinstance(c, dict) and c.get("id")
    }
    for col in info.get("additional_columns") or []:
        if not isinstance(col, dict) or not col.get("id"):
            continue
        cid = str(col["id"])
        base = by_id.get(cid) or {
            "id": cid,
            "name": col.get("name") or cid,
            "help": "",
            "default_value": "",
            "default_source_column": "",
            "default_mode": "fixed",
        }
        mode = "fixed"
        by_id[cid] = {
            **base,
            "enabled": bool(col.get("enabled", base.get("enabled"))),
            "mode": mode,
            "value": str(col.get("value") or "").strip(),
            "source_column": "",
            "name": str(col.get("name") or base.get("name") or cid),
        }

    # Legacy advertiser payload → fold into additional_columns
    legacy_adv = info.get("advertiser") if isinstance(info.get("advertiser"), dict) else None
    if legacy_adv and "advertiser" in by_id:
        by_id["advertiser"].update({
            "enabled": True,
            "mode": "fixed",
            "value": str(legacy_adv.get("value") or "").strip(),
            "source_column": "",
            "name": str(legacy_adv.get("name") or by_id["advertiser"].get("name") or "Advertiser"),
        })

    additional = list(by_id.values())
    adv = by_id.get("advertiser") or {}
    return {
        "fields": defaults.get("fields") or [],
        "values": values,
        "enabled_optional_ids": enabled,
        "additional_columns": additional,
        "advertiser": {
            "mode": adv.get("mode") or "fixed",
            "value": str(adv.get("value") or ""),
            "source_column": str(adv.get("source_column") or ""),
            "name": adv.get("name") or "Advertiser",
        },
    }


def apply_registry_to_source_metadata(
    job_manager: Any,
    job_id: str,
    hierarchy_registry: Sequence[Dict[str, Any]],
) -> None:
    """Copy publisher + grain onto source_registry via update_source_metadata."""
    for reg in hierarchy_registry or []:
        if not isinstance(reg, dict) or not reg.get("source_id"):
            continue
        sid = str(reg["source_id"])
        extra = {
            "media_hierarchy": {
                "publisher_id": reg.get("publisher_id"),
                "publisher_name": reg.get("publisher_name"),
                "grain_level": reg.get("grain_level"),
                "grain_level_id": reg.get("grain_level_id"),
                "grain_level_name": reg.get("grain_level_name"),
                "grain_level_index": reg.get("grain_level_index"),
            }
        }
        meta = {
            "publisher": reg.get("publisher_name") or reg.get("publisher_id") or "",
            "extra_metadata": extra,
            "status": "hierarchy_registered" if reg.get("registered") else "discovered",
        }
        job_manager.update_source_metadata(
            job_id,
            reg.get("sheet_name"),
            meta,
            source_id=sid,
        )


def get_accepted_combined_fields_for_source(
    job: Dict[str, Any],
    source_id: str,
) -> List[Dict[str, Any]]:
    """Helper for schema_mapper / column standardization."""
    sid = str(source_id)
    # Prefer column_standardize_registry (post-layout step)
    for row in job.get("column_standardize_registry") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("source_id")) != sid:
            continue
        return [
            c for c in (row.get("splits") or [])
            if isinstance(c, dict) and c.get("accepted") and not c.get("single_dimension")
        ]
    for reg in job.get("hierarchy_registry") or []:
        if not isinstance(reg, dict):
            continue
        if str(reg.get("source_id")) != sid:
            continue
        return [
            c for c in (reg.get("combined_fields") or [])
            if isinstance(c, dict) and c.get("accepted") and not c.get("single_dimension")
        ]
    for source in job.get("source_registry") or []:
        if str(source.get("source_id")) != sid:
            continue
        extra = source.get("extra_metadata") or {}
        mh = extra.get("media_hierarchy") if isinstance(extra, dict) else {}
        if isinstance(mh, dict):
            return list(mh.get("combined_fields") or [])
    return []


def get_hierarchy_entry_for_source(
    job: Dict[str, Any],
    source_id: str,
) -> Optional[Dict[str, Any]]:
    sid = str(source_id)
    for reg in job.get("hierarchy_registry") or []:
        if isinstance(reg, dict) and str(reg.get("source_id")) == sid:
            return reg
    return None


def mapping_metric_targets(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Canonical metric destinations for Schema Mapping (Spend, Impressions, Clicks, ROAS, …)."""
    cat = catalog or load_catalog()
    out: List[Dict[str, Any]] = []
    seen = set()
    for m in cat.get("metrics") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        mid = str(m["id"])
        if mid in seen:
            continue
        seen.add(mid)
        out.append({
            "id": mid,
            "name": str(m.get("name") or mid),
            "kind": "metric",
            "supports_currency": bool(m.get("supports_currency")) or mid in ("spends", "spend"),
            "group": str(m.get("group") or "metric"),
        })
    for f in cat.get("standard_fields") or []:
        if not isinstance(f, dict) or f.get("type") != "metric" or not f.get("id"):
            continue
        mid = str(f["id"])
        if mid in seen:
            continue
        seen.add(mid)
        out.append({
            "id": mid,
            "name": str(f.get("label") or mid),
            "kind": "metric",
            "supports_currency": mid in ("spends", "spend"),
            "group": "metric",
        })
    out.sort(key=lambda x: str(x.get("name") or "").lower())
    return out


def mapping_target_option_catalog(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Labeled Mapping dropdown options: metrics first (A–Z), then dimensions/hierarchy (A–Z)."""
    cat = catalog or load_catalog()
    metrics = mapping_metric_targets(cat)
    dims = mapping_attribute_target_options(cat)
    # Drop metric ids that also appear as dimensions
    metric_ids = {m["id"] for m in metrics}
    dim_opts = [
        {"id": d["id"], "name": d["name"], "kind": "dimension", "supports_currency": False}
        for d in dims
        if d.get("id") and d["id"] not in metric_ids
    ]
    return metrics + dim_opts


def mapping_attribute_targets(catalog: Optional[Dict[str, Any]] = None) -> List[str]:
    """Destination attribute ids for Schema Mapping / Column shaping (A–Z by label)."""
    cat = catalog or load_catalog()
    label_by_id: Dict[str, str] = {}
    for lv in media_hierarchy_levels(cat):
        if lv.get("id"):
            label_by_id[str(lv["id"])] = str(lv.get("name") or lv["id"])
    for attr in cat.get("common_attributes") or []:
        if isinstance(attr, dict) and attr.get("id"):
            label_by_id[str(attr["id"])] = str(attr.get("name") or attr["id"])
    ei = cat.get("enterprise_info") or {}
    for key in ("mandatory_fields", "optional_fields"):
        for f in ei.get(key) or []:
            if isinstance(f, dict) and f.get("id"):
                label_by_id.setdefault(str(f["id"]), str(f.get("name") or f["id"]))
    for f in cat.get("standard_fields") or []:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        if f.get("type") in ("dimension", "hierarchy"):
            label_by_id.setdefault(str(f["id"]), str(f.get("label") or f["id"]))
    return sorted(label_by_id.keys(), key=lambda i: label_by_id[i].lower())


def mapping_attribute_target_options(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Labeled attribute options for Column shaping dropdowns (alphabetical)."""
    cat = catalog or load_catalog()
    ids = mapping_attribute_targets(cat)
    label_by_id: Dict[str, str] = {}
    for lv in media_hierarchy_levels(cat):
        if lv.get("id"):
            label_by_id[str(lv["id"])] = str(lv.get("name") or lv["id"])
    for attr in cat.get("common_attributes") or []:
        if isinstance(attr, dict) and attr.get("id"):
            label_by_id[str(attr["id"])] = str(attr.get("name") or attr["id"])
    ei = cat.get("enterprise_info") or {}
    for key in ("mandatory_fields", "optional_fields"):
        for f in ei.get(key) or []:
            if isinstance(f, dict) and f.get("id"):
                label_by_id.setdefault(str(f["id"]), str(f.get("name") or f["id"]))
    for f in cat.get("standard_fields") or []:
        if isinstance(f, dict) and f.get("id"):
            label_by_id.setdefault(str(f["id"]), str(f.get("label") or f["id"]))
    return [{"id": i, "name": label_by_id.get(i, i)} for i in ids]
