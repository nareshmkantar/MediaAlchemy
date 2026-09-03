"""
Convert a messy planning-matrix / crosstab sheet into a standard tidy table.

Output shape (wide):
    Date | <dimension columns> | <one column per metric>

Rows are Date × dimension combinations; metric values sit under their columns.
Title bands, totals, and comment/noise islands are excluded.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from sia.agent.sheet_layout_class import (
    _cell_text,
    _is_empty,
    _is_numeric,
    _tiny_island_cells,
)

logger = logging.getLogger(__name__)

STANDARDIZED_ROOT = Path(__file__).resolve().parent.parent.parent / "runtime" / "standardized"
STANDARDIZED_SHEET_NAME = "Standard"
DATE_COLUMN = "Date"
VALUE_FALLBACK = "Value"

_JOB_KEY = "layout_standardize_by_source"

_DEFAULT_INCLUDE = {
    "time": True,
    "dimensions": True,
    "metrics": True,
    "values": True,
    "totals": False,
    "noise": False,
    "metadata": False,
}


def needs_standardize_step(report: Optional[Dict[str, Any]]) -> bool:
    """True when the sheet is messy / crosstab / high-complexity — not a flat table."""
    if not isinstance(report, dict) or not report:
        return False
    classification = str(report.get("classification") or "").strip().lower()
    try:
        score = float(report.get("complexity_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    if classification == "messy":
        return True
    if score >= 50:
        return True
    if report.get("crosstab"):
        return True
    if report.get("adapter_used") or report.get("use_adapter"):
        return True
    return False


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _uniq_name(name: str, used: set) -> str:
    base = re.sub(r"\s+", " ", str(name or "").strip()) or "Column"
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base} {n}"
        n += 1
    used.add(candidate)
    return candidate


def _infer_dimension_names(
    df: pd.DataFrame,
    stub_cols: Sequence[int],
    header_rows: Sequence[int],
    body_start: int,
) -> List[str]:
    used: set = set()
    names: List[str] = []
    name_row = max(header_rows) if header_rows else max(0, body_start - 1)
    for i, col in enumerate(stub_cols):
        label = ""
        if 0 <= name_row < df.shape[0] and 0 <= int(col) < df.shape[1]:
            label = _cell_text(df.iat[name_row, int(col)])
        if not label and header_rows:
            for hr in reversed(list(header_rows)):
                if 0 <= hr < df.shape[0] and 0 <= int(col) < df.shape[1]:
                    label = _cell_text(df.iat[hr, int(col)])
                    if label:
                        break
        names.append(_uniq_name(label or f"Dimension {i + 1}", used))
    return names


def _ffill_column(df: pd.DataFrame, col: int, start_row: int, end_row: int) -> Dict[int, str]:
    filled: Dict[int, str] = {}
    last = ""
    for r in range(start_row, end_row + 1):
        if r >= df.shape[0] or col >= df.shape[1]:
            filled[r] = last
            continue
        text = _cell_text(df.iat[r, col])
        if text:
            last = text
        filled[r] = last
    return filled


def _ffill_header_row(df: pd.DataFrame, row: int, start_col: int, end_col: int) -> Dict[int, str]:
    filled: Dict[int, str] = {}
    last = ""
    for c in range(start_col, end_col + 1):
        if row >= df.shape[0] or c >= df.shape[1]:
            filled[c] = last
            continue
        text = _cell_text(df.iat[row, c])
        if text:
            last = text
        filled[c] = last
    return filled


def _period_labels(
    df: pd.DataFrame,
    header_rows: Sequence[int],
    start_col: int,
    end_col: int,
) -> Dict[int, str]:
    row_fills = [_ffill_header_row(df, hr, start_col, end_col) for hr in header_rows if 0 <= hr < df.shape[0]]
    labels: Dict[int, str] = {}
    for c in range(start_col, end_col + 1):
        parts: List[str] = []
        for fill in row_fills:
            text = fill.get(c) or ""
            if text and text not in parts:
                parts.append(text)
        labels[c] = " · ".join(parts)
    return labels


def build_role_assignment(
    df: pd.DataFrame,
    report: Optional[Dict[str, Any]],
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge detected axes with optional user overrides into a concrete assignment."""
    report = report if isinstance(report, dict) else {}
    axes = report.get("axes") or {}
    region = report.get("data_region") or {}
    ov = overrides if isinstance(overrides, dict) else {}

    header_rows = [int(r) for r in (ov.get("header_rows") or axes.get("header_rows") or []) if r is not None]
    stub_cols = [int(c) for c in (ov.get("stub_columns") or axes.get("stub_columns") or []) if c is not None]
    metric_col = _safe_int(ov.get("metric_column"), _safe_int(axes.get("metric_column")))
    metric_axis = ov.get("metric_name_axis") or axes.get("metric_name_axis")
    metric_header_row = _safe_int(ov.get("metric_header_row"), _safe_int(axes.get("metric_header_row")))
    date_axis = ov.get("date_axis") or axes.get("date_axis")
    date_column = _safe_int(ov.get("date_column"), _safe_int(axes.get("date_column")))

    sr = int(region.get("start_row") or 0)
    er = int(region.get("end_row") or max(df.shape[0] - 1, 0))
    sc = int(region.get("start_col") or 0)
    ec = int(region.get("end_col") or max(df.shape[1] - 1, 0))

    title_end = _safe_int(axes.get("title_band_end_row"))
    body_start = _safe_int(ov.get("body_start_row"))
    if body_start is None:
        body_start = (max(header_rows) + 1) if header_rows else sr
        if title_end is not None:
            body_start = max(body_start, int(title_end) + 1)
    body_end = _safe_int(ov.get("body_end_row"), er)

    value_start = _safe_int(ov.get("value_start_col"))
    if value_start is None:
        value_start = sc
        if metric_col is not None:
            value_start = max(value_start, int(metric_col) + 1)
        elif stub_cols:
            value_start = max(value_start, max(stub_cols) + 1)
    value_end = _safe_int(ov.get("value_end_col"), ec)

    include = dict(_DEFAULT_INCLUDE)
    include.update(ov.get("include_roles") or {})

    dim_names = ov.get("dimension_names")
    if not isinstance(dim_names, list) or len(dim_names) != len(stub_cols):
        dim_names = _infer_dimension_names(df, stub_cols, header_rows, body_start)

    return {
        "date_axis": date_axis,
        "date_column": date_column,
        "header_rows": header_rows,
        "stub_columns": stub_cols,
        "dimension_names": [str(n).strip() or f"Dimension {i + 1}" for i, n in enumerate(dim_names)],
        "metric_name_axis": metric_axis,
        "metric_column": metric_col,
        "metric_header_row": metric_header_row,
        "value_start_col": int(value_start),
        "value_end_col": int(value_end),
        "body_start_row": int(body_start),
        "body_end_row": int(body_end),
        "exclude_totals": bool(ov.get("exclude_totals", True)),
        "exclude_noise": bool(ov.get("exclude_noise", True)),
        "include_roles": include,
        "total_rows": [int(r) for r in (axes.get("total_rows") or [])],
        "total_cols": [int(c) for c in (axes.get("total_cols") or [])],
        "tiny_islands": list(report.get("tiny_islands") or []),
    }


def _roles_for_ui(assignment: Dict[str, Any], report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    report = report if isinstance(report, dict) else {}
    detected = report.get("roles_for_review") or {}
    include = assignment.get("include_roles") or {}

    def _detail(key: str, fallback: str) -> str:
        role = detected.get(key) or {}
        return str(role.get("detail") or fallback)

    dim_bits = ", ".join(
        f"{n} (col {c + 1})"
        for n, c in zip(assignment.get("dimension_names") or [], assignment.get("stub_columns") or [])
    )
    header_rows = assignment.get("header_rows") or []
    period_detail = (
        f"Period headers on Excel row(s) {', '.join(str(r + 1) for r in header_rows)}"
        if header_rows
        else _detail("time", "Not detected")
    )
    metric_col = assignment.get("metric_column")
    metric_detail = (
        f"KPI names in column {int(metric_col) + 1}"
        if metric_col is not None and assignment.get("metric_name_axis") == "rows"
        else _detail("metrics", "Not detected")
    )
    return [
        {
            "key": "time",
            "label": "Date / period",
            "included": bool(include.get("time", True)),
            "kind": assignment.get("date_axis") or "unknown",
            "detail": period_detail,
        },
        {
            "key": "dimensions",
            "label": "Dimensions",
            "included": bool(include.get("dimensions", True)),
            "kind": "columns" if assignment.get("stub_columns") else "unknown",
            "detail": dim_bits or _detail("dimensions", "Not detected"),
            "names": list(assignment.get("dimension_names") or []),
            "columns": list(assignment.get("stub_columns") or []),
        },
        {
            "key": "metrics",
            "label": "Metrics",
            "included": bool(include.get("metrics", True)),
            "kind": assignment.get("metric_name_axis") or "unknown",
            "detail": metric_detail,
        },
        {
            "key": "values",
            "label": "Values",
            "included": bool(include.get("values", True)),
            "kind": "body",
            "detail": (
                f"Numeric body rows {assignment['body_start_row'] + 1}–{assignment['body_end_row'] + 1}, "
                f"columns {assignment['value_start_col'] + 1}–{assignment['value_end_col'] + 1}"
            ),
        },
        {
            "key": "noise",
            "label": "Comments / noise",
            "included": False,
            "kind": "exclude",
            "detail": _detail("noise", "Excluded from the standard table"),
        },
        {
            "key": "totals",
            "label": "Totals",
            "included": False,
            "kind": "exclude",
            "detail": _detail("totals", "Excluded from the standard table"),
        },
    ]


def _preview_payload(df: pd.DataFrame, column_roles: Dict[str, str], limit: int = 24) -> Dict[str, Any]:
    cols = [str(c) for c in df.columns]
    rows: List[Dict[str, Any]] = []
    for rec in df.head(limit).to_dict(orient="records"):
        row = {}
        for col in cols:
            val = rec.get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                row[col] = ""
            else:
                row[col] = _cell_text(val) if not _is_numeric(val) else (
                    int(val) if isinstance(val, (int, float)) and float(val) == int(val) else val
                )
        rows.append(row)
    return {
        "columns": cols,
        "column_roles": {c: column_roles.get(c, "metric") for c in cols},
        "rows": rows,
        "row_count": int(len(df)),
        "preview_row_count": len(rows),
    }


def _empty_result(assignment: Dict[str, Any], report: Optional[Dict[str, Any]], message: str) -> Dict[str, Any]:
    empty = pd.DataFrame()
    return {
        "ok": False,
        "message": message,
        "assignment": assignment,
        "roles": _roles_for_ui(assignment, report),
        "dataframe": empty,
        "preview": _preview_payload(empty, {}),
        "column_roles": {},
    }


def standardize_messy_layout(
    df: pd.DataFrame,
    report: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a standard Date + Dimensions + Metrics table from a messy grid.

    Returns assignment, roles, a DataFrame, and a JSON-safe preview.
    """
    grid = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
    assignment = build_role_assignment(grid, report, overrides)
    include = assignment.get("include_roles") or {}
    if not include.get("values", True):
        return _empty_result(assignment, report, "Values region is excluded — nothing to standardize.")

    if grid.empty:
        return _empty_result(assignment, report, "Sheet is empty.")

    if assignment.get("date_axis") == "columns" and assignment.get("header_rows"):
        result_df, column_roles, message = _unpivot_crosstab(grid, assignment)
    else:
        result_df, column_roles, message = _extract_already_tabular(grid, assignment)

    if result_df is None or result_df.empty:
        return _empty_result(assignment, report, message or "No numeric values found in the identified region.")

    return {
        "ok": True,
        "message": message,
        "assignment": assignment,
        "roles": _roles_for_ui(assignment, report),
        "dataframe": result_df,
        "preview": _preview_payload(result_df, column_roles),
        "column_roles": column_roles,
    }


def _unpivot_crosstab(
    df: pd.DataFrame,
    assignment: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, str], str]:
    header_rows = list(assignment.get("header_rows") or [])
    stub_cols = list(assignment.get("stub_columns") or [])
    dim_names = list(assignment.get("dimension_names") or [])
    metric_col = assignment.get("metric_column")
    metric_axis = assignment.get("metric_name_axis")
    metric_header_row = assignment.get("metric_header_row")
    vs = int(assignment["value_start_col"])
    ve = int(assignment["value_end_col"])
    br = int(assignment["body_start_row"])
    er = int(assignment["body_end_row"])
    exclude_totals = bool(assignment.get("exclude_totals", True))
    exclude_noise = bool(assignment.get("exclude_noise", True))
    total_rows = set(assignment.get("total_rows") or [])
    total_cols = set(assignment.get("total_cols") or [])
    island_cells = _tiny_island_cells(assignment.get("tiny_islands") or []) if exclude_noise else set()

    value_cols = [c for c in range(vs, ve + 1) if not (exclude_totals and c in total_cols)]
    period_labels = _period_labels(df, header_rows, vs, ve) if assignment.get("include_roles", {}).get("time", True) else {}

    dim_fills = {
        col: _ffill_column(df, col, br, er)
        for col in stub_cols
        if assignment.get("include_roles", {}).get("dimensions", True)
    }

    metric_header_fill: Dict[int, str] = {}
    if metric_axis == "columns" and metric_header_row is not None:
        metric_header_fill = _ffill_header_row(df, int(metric_header_row), vs, ve)

    records: List[Dict[str, Any]] = []
    for r in range(br, min(er, df.shape[0] - 1) + 1):
        if exclude_totals and r in total_rows:
            continue
        if metric_axis == "rows" and metric_col is not None:
            metric = _cell_text(df.iat[r, int(metric_col)]) if int(metric_col) < df.shape[1] else ""
            if not metric:
                continue
        else:
            metric = ""

        dims: Dict[str, str] = {}
        if assignment.get("include_roles", {}).get("dimensions", True):
            for name, col in zip(dim_names, stub_cols):
                dims[name] = dim_fills.get(col, {}).get(r, "")

        for c in value_cols:
            if c >= df.shape[1]:
                continue
            if exclude_noise and (r, c) in island_cells:
                continue
            val = df.iat[r, c]
            if not _is_numeric(val):
                continue
            col_metric = metric
            if metric_axis == "columns":
                col_metric = metric_header_fill.get(c) or _cell_text(df.iat[int(metric_header_row or 0), c])
            if not col_metric:
                col_metric = VALUE_FALLBACK
            rec: Dict[str, Any] = {DATE_COLUMN: period_labels.get(c) or "", "Metric": col_metric, "Value": val}
            rec.update(dims)
            records.append(rec)

    if not records:
        return pd.DataFrame(), {}, "No numeric values found under the period headers."

    long_df = pd.DataFrame(records)
    index_cols = [DATE_COLUMN] + [n for n in dim_names if n in long_df.columns]
    wide = long_df.pivot_table(index=index_cols, columns="Metric", values="Value", aggfunc="first")
    if isinstance(wide.columns, pd.MultiIndex):
        wide.columns = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in wide.columns]
    else:
        wide.columns = [str(c) for c in wide.columns]
    wide = wide.reset_index()
    wide.columns.name = None

    column_roles = {DATE_COLUMN: "date"}
    for name in dim_names:
        if name in wide.columns:
            column_roles[name] = "dimension"
    for col in wide.columns:
        if col not in column_roles:
            column_roles[str(col)] = "metric"

    n_metrics = sum(1 for role in column_roles.values() if role == "metric")
    message = (
        f"Standard table: {len(wide)} rows × {len(wide.columns)} columns "
        f"({len(dim_names)} dimension(s), {n_metrics} metric(s))."
    )
    return wide, column_roles, message


def _extract_already_tabular(
    df: pd.DataFrame,
    assignment: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, str], str]:
    """Crop a long-ish table: keep Date + stub dimensions + remaining metric columns."""
    br = int(assignment["body_start_row"])
    er = int(assignment["body_end_row"])
    vs = int(assignment["value_start_col"])
    ve = int(assignment["value_end_col"])
    stub_cols = list(assignment.get("stub_columns") or [])
    dim_names = list(assignment.get("dimension_names") or [])
    date_column = assignment.get("date_column")
    exclude_totals = bool(assignment.get("exclude_totals", True))
    total_rows = set(assignment.get("total_rows") or [])
    total_cols = set(assignment.get("total_cols") or [])

    header_row = max(assignment.get("header_rows") or [max(0, br - 1)])
    keep_cols: List[int] = []
    names: List[str] = []
    roles: Dict[str, str] = {}
    used: set = set()

    if date_column is not None:
        keep_cols.append(int(date_column))
        names.append(_uniq_name(DATE_COLUMN, used))
        roles[names[-1]] = "date"

    for name, col in zip(dim_names, stub_cols):
        if col in keep_cols:
            continue
        keep_cols.append(int(col))
        names.append(_uniq_name(name, used))
        roles[names[-1]] = "dimension"

    for c in range(vs, ve + 1):
        if c in keep_cols or (exclude_totals and c in total_cols):
            continue
        keep_cols.append(c)
        header = _cell_text(df.iat[header_row, c]) if header_row < df.shape[0] and c < df.shape[1] else ""
        col_name = _uniq_name(header or f"Metric {c + 1}", used)
        names.append(col_name)
        roles[col_name] = "metric"

    if not keep_cols:
        return pd.DataFrame(), {}, "No columns identified for a standard table."

    rows: List[List[Any]] = []
    for r in range(br, min(er, df.shape[0] - 1) + 1):
        if exclude_totals and r in total_rows:
            continue
        row = []
        nonempty = False
        for c in keep_cols:
            val = df.iat[r, c] if c < df.shape[1] else None
            if not _is_empty(val):
                nonempty = True
            row.append(_cell_text(val) if not _is_numeric(val) else val)
        if nonempty:
            rows.append(row)

    if not rows:
        return pd.DataFrame(), {}, "No data rows in the identified body."

    out = pd.DataFrame(rows, columns=names)
    if DATE_COLUMN not in out.columns and date_column is None:
        # Synthesize an empty Date column so mapping always sees the standard shape.
        out.insert(0, DATE_COLUMN, "")
        roles = {DATE_COLUMN: "date", **roles}

    message = f"Standard table: {len(out)} rows × {len(out.columns)} columns (cropped long table)."
    return out, roles, message


def _sanitize_token(value: str) -> str:
    text = re.sub(r"[^\w.\-]+", "_", str(value or "").strip(), flags=re.UNICODE)
    text = text.strip("._") or "source"
    return text[:120]


def write_standardized_workbook(job_id: str, source_id: str, df: pd.DataFrame) -> Path:
    out_dir = STANDARDIZED_ROOT / _sanitize_token(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_sanitize_token(source_id)}_standard.xlsx"
    df.to_excel(path, sheet_name=STANDARDIZED_SHEET_NAME, index=False)
    return path


def store_standardize_on_job(
    job: Dict[str, Any],
    source_id: str,
    result: Dict[str, Any],
    path: Optional[Path] = None,
    applied: bool = False,
) -> Dict[str, Any]:
    sid = str(source_id or "")
    preview = result.get("preview") or {}
    meta = {
        "source_id": sid,
        "applied": bool(applied),
        "ok": bool(result.get("ok")),
        "message": result.get("message"),
        "assignment": result.get("assignment") or {},
        "roles": result.get("roles") or [],
        "preview": preview,
        "column_roles": result.get("column_roles") or {},
        "columns": list(preview.get("columns") or []),
        "row_count": int(preview.get("row_count") or 0),
        "path": str(path) if path else None,
        "sheet_name": STANDARDIZED_SHEET_NAME if path else None,
    }
    job.setdefault(_JOB_KEY, {})[sid] = meta
    return meta


def get_standardize_meta(job: Optional[Dict[str, Any]], source_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not job or not source_id:
        return None
    meta = (job.get(_JOB_KEY) or {}).get(str(source_id))
    return dict(meta) if isinstance(meta, dict) else None


def try_load_standardized_dataframe(
    job: Optional[Dict[str, Any]],
    source_id: Optional[str],
) -> Optional[pd.DataFrame]:
    meta = get_standardize_meta(job, source_id)
    if not meta or not meta.get("applied"):
        return None
    path = Path(str(meta.get("path") or ""))
    if not path.is_file():
        return None
    try:
        return pd.read_excel(path, sheet_name=meta.get("sheet_name") or STANDARDIZED_SHEET_NAME)
    except Exception:
        logger.warning("Could not read standardized workbook %s", path, exc_info=True)
        return None


def load_visual_grid_dataframe(file_path: str, sheet_name: Optional[str]) -> pd.DataFrame:
    """Load the same visual-normalized grid Guided Setup uses (merged cells filled)."""
    from sia.modules.visual_normalizer import VisualNormalizer

    visual_normalizer = VisualNormalizer(include_hidden=True)
    allowed = {str(sheet_name)} if sheet_name else None
    grids, _ = visual_normalizer.normalize_workbook(
        file_path,
        only_sheet_names=allowed,
    )
    target = sheet_name if sheet_name and sheet_name in grids else (next(iter(grids), None) if grids else None)
    if not target:
        from sia.agent.scoped_source import load_raw_sheet_dataframe

        return load_raw_sheet_dataframe(file_path, sheet_name)
    return grids[target].to_dataframe()
