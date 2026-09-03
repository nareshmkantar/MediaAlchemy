"""Materialize Column shaping splits into mapping-ready columns.

Guided Setup maps *named* parts (from split/combine), not the packed source
column. Primary roles share one canonical target name across sources;
Supporting roles get ``_{source_index}`` suffixes so they stay distinct.

Catalog dimensions chosen in Column shaping (Country, Brand, Campaign Objective,
…) are carried forward as ``target_column`` ids on the expanded mapping rows.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd


def source_suffix_index(job: Optional[Dict[str, Any]], source_id: str) -> int:
    """1-based index of ``source_id`` in the job source registry (for Supporting suffixes)."""
    sid = str(source_id or "")
    for i, source in enumerate(job.get("source_registry") or [] if job else []):
        if isinstance(source, dict) and str(source.get("source_id") or "") == sid:
            return i + 1
    return 1


def get_column_shaping_splits(job: Optional[Dict[str, Any]], source_id: str) -> List[Dict[str, Any]]:
    """All column-shaping split rows for a source (accepted multipart + keep-as-single)."""
    sid = str(source_id or "")
    if not job or not sid:
        return []
    for row in job.get("column_standardize_registry") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("source_id") or "") != sid:
            continue
        return [s for s in (row.get("splits") or []) if isinstance(s, dict)]
    return []


def attribute_label_by_id(catalog: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Finite catalog dimension id → display label (Country, Brand, …)."""
    try:
        from sia.agent.hierarchy_register import mapping_attribute_target_options

        return {
            str(o["id"]): str(o.get("name") or o["id"])
            for o in mapping_attribute_target_options(catalog)
            if o.get("id")
        }
    except Exception:
        return {}


def _derive_output_columns(split: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Mirror web ``syncSplitDerivedFields`` when ``output_columns`` was not persisted."""
    existing = split.get("output_columns")
    if isinstance(existing, list) and existing:
        return [o for o in existing if isinstance(o, dict)]

    parts = [p for p in (split.get("parts") or []) if isinstance(p, dict)]
    if not parts:
        return []
    outputs: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for part in parts:
        if part.get("combine_with_previous") and current is not None:
            try:
                current["part_indexes"].append(int(part.get("index", 0)))
            except (TypeError, ValueError):
                pass
            continue
        target = str(part.get("target") or "").strip()
        custom = str(part.get("custom_name") or "").strip()
        if target in ("__custom__",):
            target = ""
        try:
            idx = int(part.get("index", len(outputs)))
        except (TypeError, ValueError):
            idx = len(outputs)
        current = {
            "part_indexes": [idx],
            "target": "" if custom and not target else target,
            "custom_name": custom if (custom and not target) else "",
        }
        outputs.append(current)
    return outputs


def _unique_column_name(base: str, used: Set[str]) -> str:
    name = str(base or "part").strip() or "part"
    if name not in used:
        used.add(name)
        return name
    n = 2
    while f"{name}_{n}" in used:
        n += 1
    out = f"{name}_{n}"
    used.add(out)
    return out


def _should_expand_split(split: Dict[str, Any]) -> bool:
    if split.get("single_dimension"):
        return False
    if split.get("accepted"):
        return True
    outputs = _derive_output_columns(split)
    for o in outputs:
        if str(o.get("target") or "").strip() or str(o.get("custom_name") or "").strip():
            return True
    for p in split.get("parts") or []:
        if isinstance(p, dict) and (
            str(p.get("target") or "").strip() or str(p.get("custom_name") or "").strip()
        ):
            return True
    return False


def packed_columns_pending_expand(splits: Sequence[Dict[str, Any]]) -> Set[str]:
    """Source columns that Column shaping will split into named parts."""
    out: Set[str] = set()
    for split in splits or []:
        if not isinstance(split, dict) or not _should_expand_split(split):
            continue
        col = str(split.get("source_column") or "").strip()
        if col:
            out.add(col)
    return out


def mapping_rows_stale_vs_shaping(
    mapping_rows: Optional[Sequence[Dict[str, Any]]],
    splits: Sequence[Dict[str, Any]],
) -> bool:
    """True when saved mapping still shows packed columns that shaping already split."""
    packed = packed_columns_pending_expand(splits)
    if not packed:
        return False
    for row in mapping_rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("column_name") or row.get("source_column") or "").strip()
        if name in packed and not row.get("split_from"):
            return True
    return False


def output_alias_for_role(
    target_column: str,
    role: str,
    source_index: int,
) -> str:
    """Primary → shared target name; Supporting → ``target_{source_index}``."""
    target = str(target_column or "").strip()
    if not target or target.lower() in {"no match", "nomatch", "no-match"}:
        return ""
    role_l = str(role or "").strip().lower()
    if role_l == "primary":
        return target
    if role_l == "supporting":
        idx = max(1, int(source_index or 1))
        return f"{target}_{idx}"
    return ""


def materialize_column_shaping(
    df: Optional[pd.DataFrame],
    splits: Sequence[Dict[str, Any]],
    *,
    label_by_id: Optional[Dict[str, str]] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Split packed columns into named part columns for semantic mapping.

    Returns ``(expanded_df, meta)`` where meta includes:
    - ``part_rows``: list of {column_name, packed_source, target, part_indexes, delimiter}
    - ``single_targets``: {source_column: target} for keep-as-single
    - ``packed_dropped``: original packed column names removed
    """
    meta: Dict[str, Any] = {
        "part_rows": [],
        "single_targets": {},
        "packed_dropped": [],
    }
    if df is None:
        return pd.DataFrame(), meta
    out = df.copy()
    used: Set[str] = {str(c) for c in out.columns}
    labels = label_by_id if label_by_id is not None else attribute_label_by_id()

    for split in splits or []:
        if not isinstance(split, dict):
            continue
        col = str(split.get("source_column") or "").strip()
        if not col:
            continue

        if split.get("single_dimension"):
            target = str(split.get("single_target") or "").strip()
            custom = str(split.get("single_custom_name") or "").strip()
            if target in ("__custom__",):
                target = ""
            if target:
                meta["single_targets"][col] = target
            elif custom and col in out.columns and custom != col:
                new_name = _unique_column_name(custom, used)
                out = out.rename(columns={col: new_name})
                meta["single_targets"][new_name] = custom
                meta["part_rows"].append({
                    "column_name": new_name,
                    "packed_source": col,
                    "target": "",
                    "part_indexes": [0],
                    "delimiter": "",
                    "keep_single": True,
                })
            continue

        if not _should_expand_split(split) or col not in out.columns:
            continue

        delim = str(split.get("delimiter") or "_") or "_"
        outputs = _derive_output_columns(split)
        if not outputs:
            continue

        max_idx = 0
        for o in outputs:
            for i in o.get("part_indexes") or [0]:
                try:
                    max_idx = max(max_idx, int(i))
                except (TypeError, ValueError):
                    pass

        raw = out[col].fillna("").astype(str)
        parts_df = raw.str.split(delim, n=max_idx, expand=True)

        for o in outputs:
            idxs_raw = o.get("part_indexes") or []
            idxs: List[int] = []
            for i in idxs_raw:
                try:
                    idxs.append(int(i))
                except (TypeError, ValueError):
                    continue
            if not idxs:
                continue
            target = str(o.get("target") or "").strip()
            if target in ("__custom__",):
                target = ""
            custom = str(o.get("custom_name") or "").strip()
            # Prefer analyst custom name, else catalog label (Country), else id, else partN
            label = (
                custom
                or (labels.get(target) if target else "")
                or target
                or f"{col}_part{idxs[0] + 1}"
            )
            name = _unique_column_name(label, used)

            series_parts: List[pd.Series] = []
            for i in idxs:
                if i < parts_df.shape[1]:
                    series_parts.append(parts_df[i].fillna("").astype(str).str.strip())
                else:
                    series_parts.append(pd.Series([""] * len(out), index=out.index))
            combined = series_parts[0]
            for sp in series_parts[1:]:
                combined = combined + delim + sp
            out[name] = combined
            meta["part_rows"].append({
                "column_name": name,
                "packed_source": col,
                "target": target,
                "part_indexes": idxs,
                "delimiter": delim,
            })

        out = out.drop(columns=[col])
        used.discard(col)
        meta["packed_dropped"].append(col)

    return out, meta


def apply_shaping_targets_and_aliases(
    mappings: List[Dict[str, Any]],
    expand_meta: Optional[Dict[str, Any]],
    *,
    source_index: int = 1,
    primary_targets: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Carry Column shaping targets into mapping rows and set Primary/Supporting aliases.

    Shaping assignments always win over synonym heuristics for expanded parts.
    """
    meta = expand_meta or {}
    by_name = {
        str(m.get("column_name") or m.get("source_column") or ""): m
        for m in mappings
        if isinstance(m, dict)
    }
    primary_set = set(primary_targets or [])
    part_by_col = {
        str(p.get("column_name") or ""): p
        for p in (meta.get("part_rows") or [])
        if isinstance(p, dict)
    }

    for name, part in part_by_col.items():
        row = by_name.get(name)
        if not row:
            continue
        packed = str(part.get("packed_source") or "")
        row["split_from"] = packed
        row["packed_source_column"] = packed
        target = str(part.get("target") or "").strip()
        if target:
            # Always retain the finite catalog dimension chosen in Column shaping
            row["target_column"] = target
            row["target_match_method"] = "column_shaping"
            row["target_match_confidence"] = max(
                float(row.get("target_match_confidence") or 0),
                0.98,
            )
            row["decision"] = "Keep"
            row["confidence"] = max(float(row.get("confidence") or 0), 0.95)
            # Do not overwrite an analyst-chosen Supporting/Exclude role
            if not row.get("role") or str(row.get("role")).lower() == "exclude":
                row["role"] = "primary"
        note = f"From column shaping split of {packed}." if packed else "From column shaping."
        row["reasoning"] = f"{row.get('reasoning') or ''} {note}".strip()

    for col, target in (meta.get("single_targets") or {}).items():
        row = by_name.get(str(col))
        if not row or not target:
            continue
        row["target_column"] = str(target)
        row["target_match_method"] = "column_shaping"
        row["target_match_confidence"] = max(
            float(row.get("target_match_confidence") or 0),
            0.98,
        )
        row["decision"] = "Keep"
        if str(target) in primary_set:
            row["role"] = "primary"
        elif not row.get("role") or row.get("role") == "exclude":
            row["role"] = "primary"
        row["reasoning"] = (
            f"{row.get('reasoning') or ''} Carry-forward from column shaping (keep as single)."
        ).strip()

    for row in mappings:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "supporting").strip().lower() or "supporting"
        target = str(row.get("target_column") or "").strip()
        if role != "exclude" and target and target.lower() not in {"no match", "nomatch", "no-match"}:
            if not role or role == "exclude":
                role = "supporting"
                row["role"] = role
        row["output_alias"] = output_alias_for_role(target, role, source_index)
        if row.get("output_alias") and role == "supporting":
            hint = f"Supporting across sources → output as {row['output_alias']}."
            if hint not in str(row.get("reasoning") or ""):
                row["reasoning"] = f"{row.get('reasoning') or ''} {hint}".strip()
        elif row.get("output_alias") and role == "primary":
            hint = f"Primary → clubs with other sources as {row['output_alias']}."
            if hint not in str(row.get("reasoning") or ""):
                row["reasoning"] = f"{row.get('reasoning') or ''} {hint}".strip()

    return mappings
