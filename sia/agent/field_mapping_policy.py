"""How Master List columns should be mapped — minimise effort, keep quality.

Two matching layers:

1. **Column synonyms** — source *header* names → standard ids (``media cost`` → spends).
2. **Field value lists** — finite vocabularies (Country: DE, DEU, Germany → Germany).

Do **not** pre-map dates, metrics, or high-cardinality IDs (campaign/ad/creative).
Those pass through; optional standard names are capped.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# Closed vocab: maintain a synonym list. Cheap and high quality.
FIELD_VALUE_CLOSED = frozenset(
    {
        "market",
        "country",
        "category",
        "brand",
        "advertiser",
        "business_unit",
        "region",
        "sub_category_1",
        "sub_category_2",
        "sub_brand",
        "channel",
        "device",
        "campaign_objective",
        "campaign_type",
        "buying_model",
        "campaign_kpi",
        "publisher",
        "creative_format",
        "creative_size",
    }
)

# High-cardinality names/IDs: pass through. Optional list, hard cap.
FIELD_VALUE_OPEN_CAPPED = frozenset(
    {
        "campaign",
        "ad_group",
        "ad",
        "creative",
        "audience",
        "inventory",
        "creative_variant",
    }
)
FIELD_VALUE_CAP = 100

# No value synonym list — grain or numeric.
FIELD_VALUE_NONE = frozenset(
    {
        "date",
        "spends",
        "impressions",
        "clicks",
        "video_views",
        "roas",
    }
)

# Starter aliases so packed campaign names match without a blank catalog.
STARTER_VALUE_LISTS: Dict[str, List[Dict[str, Any]]] = {
    "campaign_objective": [
        {"standard": "Awareness", "aliases": ["Awareness", "AWAR", "Brand"]},
        {"standard": "Consideration", "aliases": ["Consideration", "Consider", "Traffic"]},
        {"standard": "Conversion", "aliases": ["Conversion", "Conver", "Conv", "Perf", "Performance"]},
    ],
    "buying_model": [
        {"standard": "Auction", "aliases": ["Auction", "RTB", "Real-time bidding"]},
        {"standard": "Reservation", "aliases": ["Reservation", "Reserved"]},
        {"standard": "Fixed Price", "aliases": ["Fixed", "Fixed Price"]},
        {"standard": "Programmatic Guaranteed", "aliases": ["PG", "Programmatic Guaranteed"]},
    ],
    "campaign_kpi": [
        {"standard": "ROAS", "aliases": ["ROAS", "Return on ad spend"]},
        {"standard": "CTR", "aliases": ["CTR", "Click-through rate"]},
        {"standard": "CPI", "aliases": ["CPI", "Cost per install"]},
        {"standard": "CPA", "aliases": ["CPA", "Cost per acquisition", "Cost per action"]},
        {"standard": "CPC", "aliases": ["CPC", "Cost per click"]},
        {"standard": "CPM", "aliases": ["CPM", "Cost per mille", "Cost per thousand"]},
        {"standard": "VTR", "aliases": ["VTR", "View-through rate"]},
        {"standard": "CPV", "aliases": ["CPV", "Cost per view"]},
    ],
    "campaign_type": [
        {"standard": "Prospecting", "aliases": ["Prospecting", "Prospect"]},
        {"standard": "Retargeting", "aliases": ["Retargeting", "Remarketing"]},
        {"standard": "Always on", "aliases": ["Always on", "AO", "Always-on"]},
    ],
    "device": [
        {"standard": "Desktop", "aliases": ["Desktop", "DT", "PC"]},
        {"standard": "Mobile", "aliases": ["Mobile", "MW", "Phone"]},
        {"standard": "Tablet", "aliases": ["Tablet", "Tab"]},
        {"standard": "CTV", "aliases": ["CTV", "Connected TV", "OTT"]},
    ],
    "channel": [
        {"standard": "digital", "aliases": ["digital", "Digital", "Online"]},
        {"standard": "TV", "aliases": ["TV", "Television"]},
        {"standard": "radio", "aliases": ["radio", "Radio"]},
        {"standard": "out of home", "aliases": ["OOH", "out of home", "Out of Home"]},
        {"standard": "print", "aliases": ["print", "Print"]},
    ],
}


def field_value_mode(field_id: Optional[str], *, field_type: str = "") -> str:
    """Return ``closed``, ``open``, or ``none``."""
    fid = str(field_id or "").strip()
    ftype = str(field_type or "").strip().lower()
    if ftype == "metric" or fid in FIELD_VALUE_NONE:
        return "none"
    if ftype == "date" or fid == "date":
        return "none"
    if fid in FIELD_VALUE_OPEN_CAPPED:
        return "open"
    if fid in FIELD_VALUE_CLOSED:
        return "closed"
    if ftype in ("dimension", "hierarchy"):
        return "closed"
    return "none"


def _tokens_from_option(opt: Any) -> List[str]:
    if isinstance(opt, str):
        s = opt.strip()
        return [s] if s else []
    if not isinstance(opt, dict):
        return []
    out: List[str] = []
    for key in ("standard", "label", "name", "value"):
        v = str(opt.get(key) or "").strip()
        if v:
            out.append(v)
    for a in opt.get("aliases") or []:
        v = str(a or "").strip()
        if v:
            out.append(v)
    return out


def _options_for_field(catalog: Dict[str, Any], field_id: str) -> List[Any]:
    """Curated options plus anything learned from analysts, curated first."""
    options = list(_curated_options_for_field(catalog, field_id))
    try:
        from .learned_mappings import learned_value_options

        options.extend(learned_value_options(field_id))
    except Exception:
        # A missing or unreadable learned store must not break curated mapping.
        pass
    return options


def _curated_options_for_field(catalog: Dict[str, Any], field_id: str) -> List[Any]:
    ei = catalog.get("enterprise_info") or {}
    for key in ("mandatory_fields", "optional_fields"):
        for field in ei.get(key) or []:
            if isinstance(field, dict) and str(field.get("id") or "") == field_id:
                opts = list(field.get("options") or [])
                if opts:
                    return opts
    for attr in catalog.get("common_attributes") or []:
        if isinstance(attr, dict) and str(attr.get("id") or "") == field_id:
            opts = list(attr.get("options") or [])
            if opts:
                return opts
    if field_id == "country":
        return _options_for_field(catalog, "market")
    if field_id == "publisher":
        pubs = catalog.get("publishers") or {}
        if isinstance(pubs, dict):
            return [
                {"standard": str(p.get("name") or pid), "aliases": list(p.get("aliases") or [])}
                for pid, p in pubs.items()
                if isinstance(p, dict)
            ]
        if isinstance(pubs, list):
            return [
                {"standard": str(p.get("name") or p.get("id") or ""), "aliases": list(p.get("aliases") or [])}
                for p in pubs
                if isinstance(p, dict)
            ]
    return list(STARTER_VALUE_LISTS.get(field_id) or [])


def iter_value_alias_tokens(catalog: Optional[Dict[str, Any]] = None) -> Iterable[Tuple[str, str]]:
    """Yield ``(token_lower, field_id)`` from closed-list aliases + starters."""
    cat = catalog or {}
    seen_fields = set()

    def emit(field_id: str, options: List[Any]) -> Iterable[Tuple[str, str]]:
        for opt in options:
            for tok in _tokens_from_option(opt):
                key = tok.lower()
                if not key:
                    continue
                target = field_id
                # ISO / short codes from Market belong on Country in packed names.
                if field_id == "market" and len(key) <= 3:
                    target = "country"
                yield key, target

    # Explicit mapping dimensions win when lists overlap. In particular,
    # geographic values reused from Market should identify Country in source data.
    for attr in cat.get("common_attributes") or []:
        if not isinstance(attr, dict):
            continue
        fid = str(attr.get("id") or "")
        if not fid:
            continue
        seen_fields.add(fid)
        yield from emit(fid, _options_for_field(cat, fid))

    ei = cat.get("enterprise_info") or {}
    for key in ("mandatory_fields", "optional_fields"):
        for field in ei.get(key) or []:
            if not isinstance(field, dict):
                continue
            fid = str(field.get("id") or "")
            if not fid:
                continue
            seen_fields.add(fid)
            yield from emit(fid, _options_for_field(cat, fid))

    for fid in FIELD_VALUE_CLOSED:
        if fid in seen_fields:
            continue
        yield from emit(fid, _options_for_field(cat, fid))

    # Open fields have no fixed list, but their learned queue of recent values
    # is what lets a column of opaque campaign names be recognised as Campaign.
    for fid in FIELD_VALUE_OPEN_CAPPED:
        if fid in seen_fields:
            continue
        yield from emit(fid, _options_for_field(cat, fid))


def build_value_token_map(catalog: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """First alias wins when the same token appears on two fields."""
    token_map: Dict[str, str] = {}
    for token, field_id in iter_value_alias_tokens(catalog):
        token_map.setdefault(token, field_id)
    return token_map


def build_value_harmonization_map(
    field_id: str,
    catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Return case-insensitive source value → standard value for one finite field."""
    cat = catalog or {}
    mapping: Dict[str, str] = {}
    for option in _options_for_field(cat, str(field_id or "")):
        if isinstance(option, dict):
            standard = str(
                option.get("standard")
                or option.get("label")
                or option.get("name")
                or option.get("value")
                or ""
            ).strip()
        else:
            standard = str(option or "").strip()
        if not standard:
            continue
        for token in _tokens_from_option(option):
            key = token.strip().casefold()
            if key:
                mapping.setdefault(key, standard)
        mapping.setdefault(standard.casefold(), standard)
    return mapping
