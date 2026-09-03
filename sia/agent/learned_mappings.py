"""Mappings the app learns from analysts, kept apart from the curated config.

Two tiers, both live:

* **validated** — an analyst reviewed the entry in Config. A review marker only.
* **unvalidated** — captured automatically from a confirmed mapping or an ingest.

Both tiers apply, so a header an analyst mapped once is recognised the next time
without a second confirmation. ``validated`` exists so Config can show what has
not been looked at yet.

Two kinds of entry:

* **column aliases** — source *header* → standard id (``Media Cost`` → ``spends``).
* **field values** — source *cell value* → standard value, per field.

Field value storage follows :mod:`sia.agent.field_mapping_policy`:

* ``closed`` fields (Country, Publisher, Brand) hold a fixed list the analyst
  owns. What lands here is a review inbox of values that matched nothing, so
  they can be pointed at a standard value.
* ``open`` fields (Campaign, Creative, Ad group) cannot have a fixed list, so
  they keep a rolling queue of the most recent unique values. The queue only
  helps recognise *which column* the data belongs to; it never rewrites values.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_VERSION = "1.0"

# Rolling queue depth for open (high-cardinality) fields.
OPEN_QUEUE_CAP = 100
# Review inbox depth for closed fields, so a dirty source cannot grow the file
# without bound. Validated entries are never evicted.
CLOSED_INBOX_CAP = 100

_CACHE: Optional[Dict[str, Any]] = None
_LOCK = threading.RLock()
_BATCH_DEPTH = 0
_BATCH_DIRTY = False


@contextmanager
def batch() -> Any:
    """Group many ``record_*`` calls into a single write.

    An ingest touches every mapped column; without this each one would rewrite
    the file.
    """
    global _BATCH_DEPTH, _BATCH_DIRTY
    with _LOCK:
        _BATCH_DEPTH += 1
    try:
        yield
    finally:
        with _LOCK:
            _BATCH_DEPTH -= 1
            if _BATCH_DEPTH <= 0:
                _BATCH_DEPTH = 0
                if _BATCH_DIRTY:
                    _BATCH_DIRTY = False
                    if _CACHE is not None:
                        _write(_CACHE)


def _path() -> Path:
    # Tests and throwaway environments point this elsewhere so a run never
    # writes into the checked-in config directory.
    override = os.environ.get("SIA_LEARNED_MAPPINGS_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "config" / "learned_mappings.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty() -> Dict[str, Any]:
    return {
        "_version": _VERSION,
        "_description": "Mappings learned from analyst confirmations and ingests.",
        "column_aliases": {},
        "field_values": {},
    }


def _normalize(value: Any) -> str:
    """Case/space-insensitive key used to decide whether two entries are the same."""
    return " ".join(str(value or "").strip().casefold().split())


def load_learned(force_reload: bool = False) -> Dict[str, Any]:
    """Return the learned store, reading from disk at most once per process."""
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not force_reload:
            return _CACHE
        path = _path()
        data = _empty()
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update(raw)
            except Exception as exc:
                logger.warning("Could not load learned_mappings.json: %s", exc)
        if not isinstance(data.get("column_aliases"), dict):
            data["column_aliases"] = {}
        if not isinstance(data.get("field_values"), dict):
            data["field_values"] = {}
        _CACHE = data
        return _CACHE


def save_learned(data: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the learned store and refresh the cache."""
    global _CACHE, _BATCH_DIRTY
    if not isinstance(data, dict):
        raise ValueError("Learned mappings must be a JSON object")
    with _LOCK:
        out = dict(data)
        out["_version"] = _VERSION
        out.setdefault(
            "_description",
            "Mappings learned from analyst confirmations and ingests.",
        )
        out.setdefault("column_aliases", {})
        out.setdefault("field_values", {})
        _CACHE = out
        if _BATCH_DEPTH > 0:
            _BATCH_DIRTY = True
            return out
        return _write(out)


def _write(out: Dict[str, Any]) -> Dict[str, Any]:
    with _LOCK:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        tmp.replace(path)
        # Downstream alias lookups memoise the merged list; drop those too.
        _invalidate_consumers()
        return out


def _invalidate_consumers() -> None:
    try:
        from .schema_mapper import invalidate_column_synonym_cache

        invalidate_column_synonym_cache()
    except Exception:
        pass


def field_mode(field_id: str) -> str:
    """``closed``, ``open`` or ``none`` for a field id."""
    try:
        from .field_mapping_policy import field_value_mode

        return field_value_mode(field_id)
    except Exception:
        return "closed"


# --------------------------------------------------------------------------
# Column aliases
# --------------------------------------------------------------------------


def record_column_alias(
    target_id: str,
    alias: str,
    *,
    validated: bool = False,
    source: str = "confirmation",
) -> bool:
    """Remember that ``alias`` was mapped to ``target_id``. True when stored."""
    target = str(target_id or "").strip()
    text = str(alias or "").strip()
    if not target or not text:
        return False
    if _normalize(text) == _normalize(target):
        return False
    with _LOCK:
        data = load_learned()
        entries = data["column_aliases"].setdefault(target, [])
        key = _normalize(text)
        for entry in entries:
            if _normalize(entry.get("alias")) == key:
                entry["count"] = int(entry.get("count") or 0) + 1
                entry["last_seen"] = _now()
                if validated:
                    entry["validated"] = True
                save_learned(data)
                return True
        entries.append({
            "alias": text,
            "validated": bool(validated),
            "count": 1,
            "source": source,
            "first_seen": _now(),
            "last_seen": _now(),
        })
        save_learned(data)
        return True


def record_confirmed_mapping(rows: Iterable[Any]) -> int:
    """Capture header aliases from confirmed mapping rows. Returns rows stored."""
    stored = 0
    with batch():
        stored = _record_confirmed_mapping(rows)
    return stored


def _record_confirmed_mapping(rows: Iterable[Any]) -> int:
    stored = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        target = str(row.get("target_column") or "").strip()
        if not target or target.strip().lower() in ("", "no match", "nomatch", "no-match"):
            continue
        source_column = str(
            row.get("source_column")
            or row.get("column_name")
            or row.get("original_name")
            or ""
        ).strip()
        if not source_column:
            continue
        if record_column_alias(target, source_column, source="confirmation"):
            stored += 1
    return stored


def learned_column_aliases() -> Dict[str, List[str]]:
    """``{target_id: [alias, ...]}`` across both tiers, for the mapper."""
    out: Dict[str, List[str]] = {}
    for target, entries in (load_learned().get("column_aliases") or {}).items():
        if not isinstance(entries, list):
            continue
        aliases = [
            str(entry.get("alias")).strip()
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("alias") or "").strip()
        ]
        if aliases:
            out[str(target)] = aliases
    return out


def set_alias_validated(target_id: str, alias: str, validated: bool = True) -> bool:
    with _LOCK:
        data = load_learned()
        key = _normalize(alias)
        for entry in data["column_aliases"].get(str(target_id or ""), []):
            if isinstance(entry, dict) and _normalize(entry.get("alias")) == key:
                entry["validated"] = bool(validated)
                save_learned(data)
                return True
    return False


def remove_alias(target_id: str, alias: str) -> bool:
    with _LOCK:
        data = load_learned()
        target = str(target_id or "")
        entries = data["column_aliases"].get(target)
        if not isinstance(entries, list):
            return False
        key = _normalize(alias)
        kept = [e for e in entries if not (isinstance(e, dict) and _normalize(e.get("alias")) == key)]
        if len(kept) == len(entries):
            return False
        if kept:
            data["column_aliases"][target] = kept
        else:
            data["column_aliases"].pop(target, None)
        save_learned(data)
        return True


# --------------------------------------------------------------------------
# Field values
# --------------------------------------------------------------------------


def _trim(entries: List[Dict[str, Any]], cap: int) -> List[Dict[str, Any]]:
    """Drop the least recently seen unvalidated entries beyond ``cap``."""
    if len(entries) <= cap:
        return entries
    validated = {i for i, e in enumerate(entries) if e.get("validated")}
    room = max(0, cap - len(validated))
    # A whole ingest lands in the same second, so position breaks the tie:
    # later in the list means more recently appended.
    unvalidated = sorted(
        (i for i in range(len(entries)) if i not in validated),
        key=lambda i: (str(entries[i].get("last_seen") or ""), i),
        reverse=True,
    )
    keep = validated | set(unvalidated[:room])
    return [e for i, e in enumerate(entries) if i in keep]


def record_field_values(
    field_id: str,
    values: Iterable[Any],
    *,
    mode: Optional[str] = None,
    validated: bool = False,
    source: str = "ingest",
) -> int:
    """Queue observed values for a field. Returns the number of new entries.

    ``closed`` fields only take values that no configured alias already covers,
    so the inbox stays a genuine to-do list. ``open`` fields take everything and
    keep the most recent :data:`OPEN_QUEUE_CAP` unique values.
    """
    field = str(field_id or "").strip()
    if not field:
        return 0
    resolved_mode = str(mode or field_mode(field))
    if resolved_mode == "none":
        return 0

    known: Dict[str, str] = {}
    if resolved_mode == "closed":
        try:
            from .field_mapping_policy import build_value_harmonization_map
            from .hierarchy_register import load_catalog

            known = build_value_harmonization_map(field, load_catalog())
        except Exception as exc:
            logger.warning("Could not read configured values for %s: %s", field, exc)

    added = 0
    changed = False
    with _LOCK:
        data = load_learned()
        bucket = data["field_values"].setdefault(field, {})
        if not isinstance(bucket, dict):
            bucket = {}
            data["field_values"][field] = bucket
        bucket["mode"] = resolved_mode
        entries = bucket.setdefault("values", [])
        if not isinstance(entries, list):
            entries = []
            bucket["values"] = entries
        index = {_normalize(e.get("value")): e for e in entries if isinstance(e, dict)}

        for raw in values or []:
            text = str(raw or "").strip()
            if not text:
                continue
            key = _normalize(text)
            if not key:
                continue
            entry = index.get(key)
            if entry is not None:
                entry["count"] = int(entry.get("count") or 0) + 1
                entry["last_seen"] = _now()
                if validated:
                    entry["validated"] = True
                changed = True
                continue
            if resolved_mode == "closed" and key in known:
                continue
            entry = {
                "value": text,
                # An open queue value is its own standard; a closed inbox value
                # has no standard until an analyst picks one.
                "standard": text if resolved_mode == "open" else "",
                "validated": bool(validated),
                "count": 1,
                "source": source,
                "first_seen": _now(),
                "last_seen": _now(),
            }
            entries.append(entry)
            index[key] = entry
            added += 1
            changed = True

        cap = OPEN_QUEUE_CAP if resolved_mode == "open" else CLOSED_INBOX_CAP
        trimmed = _trim(entries, cap)
        if len(trimmed) != len(entries):
            changed = True
        bucket["values"] = trimmed
        if changed:
            save_learned(data)
    return added


def learned_value_options(field_id: str) -> List[Dict[str, Any]]:
    """Learned values for a field, shaped like curated catalog options."""
    bucket = (load_learned().get("field_values") or {}).get(str(field_id or ""))
    if not isinstance(bucket, dict):
        return []
    options: List[Dict[str, Any]] = []
    for entry in bucket.get("values") or []:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or "").strip()
        if not value:
            continue
        standard = str(entry.get("standard") or "").strip()
        # Without a standard the value maps to itself: it still identifies the
        # column, and harmonisation leaves the cell untouched.
        options.append({
            "standard": standard or value,
            "aliases": [value],
            "validated": bool(entry.get("validated")),
            "learned": True,
        })
    return options


def set_value_standard(field_id: str, value: str, standard: str, *, validated: bool = True) -> bool:
    """Point a queued value at a standard value, which is the promote action."""
    with _LOCK:
        data = load_learned()
        bucket = (data.get("field_values") or {}).get(str(field_id or ""))
        if not isinstance(bucket, dict):
            return False
        key = _normalize(value)
        for entry in bucket.get("values") or []:
            if isinstance(entry, dict) and _normalize(entry.get("value")) == key:
                entry["standard"] = str(standard or "").strip()
                entry["validated"] = bool(validated)
                save_learned(data)
                return True
    return False


def set_value_validated(field_id: str, value: str, validated: bool = True) -> bool:
    with _LOCK:
        data = load_learned()
        bucket = (data.get("field_values") or {}).get(str(field_id or ""))
        if not isinstance(bucket, dict):
            return False
        key = _normalize(value)
        for entry in bucket.get("values") or []:
            if isinstance(entry, dict) and _normalize(entry.get("value")) == key:
                entry["validated"] = bool(validated)
                save_learned(data)
                return True
    return False


def remove_value(field_id: str, value: str) -> bool:
    with _LOCK:
        data = load_learned()
        field = str(field_id or "")
        bucket = (data.get("field_values") or {}).get(field)
        if not isinstance(bucket, dict):
            return False
        entries = bucket.get("values") or []
        key = _normalize(value)
        kept = [e for e in entries if not (isinstance(e, dict) and _normalize(e.get("value")) == key)]
        if len(kept) == len(entries):
            return False
        bucket["values"] = kept
        if not kept:
            data["field_values"].pop(field, None)
        save_learned(data)
        return True


def learned_summary() -> Dict[str, Any]:
    """Counts and entries for the Config UI."""
    data = load_learned()
    aliases = data.get("column_aliases") or {}
    fields = data.get("field_values") or {}
    alias_entries = [e for entries in aliases.values() if isinstance(entries, list) for e in entries]
    value_entries = [
        e
        for bucket in fields.values()
        if isinstance(bucket, dict)
        for e in (bucket.get("values") or [])
    ]
    return {
        "column_aliases": aliases,
        "field_values": fields,
        "counts": {
            "alias_targets": len(aliases),
            "aliases": len(alias_entries),
            "aliases_unvalidated": sum(1 for e in alias_entries if not e.get("validated")),
            "value_fields": len(fields),
            "values": len(value_entries),
            "values_unvalidated": sum(1 for e in value_entries if not e.get("validated")),
        },
        "open_queue_cap": OPEN_QUEUE_CAP,
        "closed_inbox_cap": CLOSED_INBOX_CAP,
    }
