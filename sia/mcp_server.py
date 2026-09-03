"""
MCP Server for SchemaAgent Transformation Tools.
Exposes data transformation capabilities via the Model Context Protocol.

This server wraps the existing TransformationTools class to allow 
external agents (Claude, Cursor, etc.) to call our transformation logic.
"""
import logging
import pandas as pd
from typing import List, Dict, Any, Union, Optional, Literal
from mcp.server.fastmcp import FastMCP

# Import the existing transformation tools
import os
import sys

# Repo root must precede ``sia/`` so ``from sia....`` imports in ``tools.*`` resolve.
# ``sia/`` alone only supported legacy ``from tools....`` (sia/tools as top-level ``tools``).
_this_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_this_dir)
for _p in (_repo_root, _this_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.transformation_tools import TransformationTools, ToolResult

# Initialize FastMCP server
mcp = FastMCP("SchemaAgent-Transform")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_server")


import time

def _json_safe_value(value: Any) -> Any:
    """Recursively coerce pandas/numpy values into JSON-safe primitives."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if pd.isna(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _json_safe_value(value.item())
        except Exception:
            pass
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _to_json_safe(df: pd.DataFrame, limit: int = 5) -> List[Dict[str, Any]]:
    """Convert DataFrame to JSON-safe records with a small limit for previews."""
    if df is None or (hasattr(df, 'empty') and df.empty):
        return []
    # Replace NaN with None for JSON compatibility
    clean_df = df.where(pd.notnull(df), None)
    records = clean_df.head(limit).to_dict(orient='records')
    return [_json_safe_value(record) for record in records]


def _result_to_dict(
    result: ToolResult, 
    start_time: float, 
    rows_in: int = 0,
    next_hints: List[str] = None
) -> Dict[str, Any]:
    """
    Convert a ToolResult to a JSON-serializable dictionary following 
    Anthropic's recommended envelope structure.
    """
    duration_ms = int((time.time() - start_time) * 1000)
    rows_out = len(result.data) if isinstance(result.data, pd.DataFrame) else 0
    
    # Tool-specific primary result (compact)
    # We return the shape as primary data; full data is in artifacts
    data_summary = {
        "rows_affected": (rows_out - rows_in) if rows_out > 0 else 0,
        "shape": list(result.data.shape) if isinstance(result.data, pd.DataFrame) else None
    }
    
    # Handle full data (optionally returned or just previewed)
    # The suggestion says "Outputs never echo the full dataset unless explicitly requested"
    # For now, we keep it in 'full_data' for our client, but maybe we should rely on refs.
    full_dataset = None
    if isinstance(result.data, pd.DataFrame):
        full_dataset = _to_json_safe(result.data, limit=len(result.data))

    payload = {
        "ok": result.success,
        "success": result.success,
        "data": data_summary,
        "message": getattr(result, "message", "") or "",
        "context": {
            "summary": result.message,
            "metrics": {
                "rows_in": rows_in,
                "rows_out": rows_out,
                "elapsed_ms": duration_ms
            },
            "sample": {
                "rows": 3,
                "table": _to_json_safe(result.data, limit=3) if isinstance(result.data, pd.DataFrame) else result.data
            },
            "next_hints": next_hints or []
        },
        "full_data": full_dataset,  # Still return for current client compatibility
        "errors": [] if result.success else [{"code": "TOOL_ERROR", "message": result.message}]
    }
    if getattr(result, "changes_made", None):
        payload["changes_made"] = _json_safe_value(result.changes_made or {})
    return payload

# ===== MCP Tool Wrappers =====

@mcp.tool(name="layout.extract")
def extract_data_block(
    data: List[List[Any]],
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    header_row: int
) -> Dict[str, Any]:
    """
    Extract a rectangular block of data from a raw spreadsheet Grid (List of Lists) and convert it to a Table (List of Dicts).
    Use this as the FIRST step after sheet inspection to isolate the data region.
    
    Args:
        data: The raw nested list grid from the spreadsheet.
        start_row: First row of data (0-based, EXCLUDING the header row).
        end_row: Last row of data to extract.
        start_col: First column index to extract.
        end_col: Last column index to extract.
        header_row: The index of the row containing column names (used for naming columns in the output Table).
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.extract_data_block(df, start_row, end_row, start_col, end_col, header_row)
    return _result_to_dict(
        result, 
        start_time, 
        rows_in=rows_in, 
        next_hints=["transform.fill_merged", "transform.filter_summaries", "transform.filter_empty"]
    )


@mcp.tool(name="transform.fill_merged")
def fill_merged_cells(
    data: List[Dict[str, Any]],
    direction: Literal["down", "right"] = "down",
    columns: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Propagate values from the first cell of a merged range to all cells in that range within a Table (List of Dicts).
    When to use: Use this specifically to handle EXCEL-LEVEL merged cell formatting that was lost during extraction.
    
    Args:
        data: The current Table (List of Dicts).
        direction: Direction to propagate ('down' or 'right').
        columns: List of specific column NAMES to process. If omitted, checks all columns.
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.fill_merged_cells(df, direction=direction, columns=columns)
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["transform.unpivot", "transform.rename"])


@mcp.tool(name="transform.unmerge_and_fill")
def unmerge_and_fill_cells(
    data: List[Dict[str, Any]],
    direction: Literal["down", "right"] = "down",
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Propagate parent-cell values into blank rows left by Excel merged cells (forward-fill).

    Same behavior as ``transform.fill_merged``. Use **dimension / hierarchy columns only**
    (e.g. channel, market). For merged **metric** totals (e.g. block spend), use
    ``transform.expand_grouped_block`` or ``transform.allocate_block_metric`` instead.
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.unmerge_and_fill(df, direction=direction, columns=columns)
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.expand_grouped_block", "transform.rename"],
    )


@mcp.tool(name="transform.densify")
def densify_dataframe_tool(
    data: List[Dict[str, Any]],
    dimension_cols: Optional[List[Any]] = None,
    drop_empty_value_rows: bool = True,
) -> Dict[str, Any]:
    """Forward-fill sparse dimension columns; optionally drop continuation rows with no metric values."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.densify_dataframe(
        df,
        dimension_cols=dimension_cols,
        drop_empty_value_rows=drop_empty_value_rows,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.fill_merged", "transform.rename", "transform.filter_empty"],
    )


@mcp.tool(name="transform.expand_grouped_block")
def expand_grouped_block_tool(
    data: List[Dict[str, Any]],
    dimension_columns: List[str],
    block_start_columns: Optional[List[str]] = None,
    allocations: Optional[List[Dict[str, Any]]] = None,
    auto_detect_block_metrics: bool = True,
    child_numeric_sparse_threshold: float = 0.25,
    parent_numeric_rate_threshold: float = 0.55,
    row_filters: Optional[List[Dict[str, Any]]] = None,
    merged_metric_ranges: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Forward-fill sparse dimensions within each block, then split block-level metrics to child rows.
    Prefer over separate fill_merged + allocate_block_metric on grouped influencer layouts.
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.expand_grouped_block(
        df,
        dimension_columns=dimension_columns,
        block_start_columns=block_start_columns,
        allocations=allocations,
        auto_detect_block_metrics=auto_detect_block_metrics,
        child_numeric_sparse_threshold=child_numeric_sparse_threshold,
        parent_numeric_rate_threshold=parent_numeric_rate_threshold,
        row_filters=row_filters,
        merged_metric_ranges=merged_metric_ranges,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.format", "transform.aggregate_weekly", "verify.schema"],
    )


@mcp.tool(name="transform.allocate_block_metric")
def allocate_block_metric_tool(
    data: List[Dict[str, Any]],
    metric_col: str,
    block_start_columns: List[str],
    method: str = "equal",
    weight_col: Optional[str] = None,
    row_filters: Optional[List[Dict[str, Any]]] = None,
    merged_metric_ranges: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Expand grouped rows with metric semantics.

    Explicit rules: **equal** — row_metric = block_total / rows_in_block.
    **by_weight** — row_metric = block_total * (row_weight / sum_weights_in_block).
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.allocate_block_metric(
        df,
        metric_col=metric_col,
        block_start_columns=block_start_columns,
        method=method,
        weight_col=weight_col,
        row_filters=row_filters,
        merged_metric_ranges=merged_metric_ranges,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.format", "transform.aggregate_weekly", "verify.schema"],
    )


@mcp.tool(name="transform.classify_metric_level")
def classify_metric_level_tool(
    data: List[Dict[str, Any]],
    block_start_columns: Optional[List[str]] = None,
    metric_columns: Optional[List[str]] = None,
    child_numeric_sparse_threshold: float = 0.25,
    parent_numeric_rate_threshold: float = 0.55,
    row_numeric_dense_threshold: float = 0.42,
    min_numeric_global_rate: float = 0.04,
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Label metric columns by block-header vs post-row grain; dataframe unchanged."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.classify_metric_level(
        df,
        block_start_columns=block_start_columns,
        metric_columns=metric_columns,
        child_numeric_sparse_threshold=child_numeric_sparse_threshold,
        parent_numeric_rate_threshold=parent_numeric_rate_threshold,
        row_numeric_dense_threshold=row_numeric_dense_threshold,
        min_numeric_global_rate=min_numeric_global_rate,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.allocate_block_metric", "transform.aggregate_weekly", "verify.schema"],
    )


@mcp.tool(name="transform.infer_block_boundary_columns")
def infer_block_boundary_columns_tool(
    data: List[Dict[str, Any]],
    exclude_columns: Optional[List[Any]] = None,
    metric_columns_hint: Optional[List[Any]] = None,
    min_multi_row_blocks: int = 2,
    min_nonempty_row_rate: float = 0.01,
    max_nonempty_row_rate: float = 0.92,
    min_mean_segment_len: float = 1.25,
    max_pair_search: int = 6,
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Heuristic rankings for block_start_columns (grouped-row delimiters); leaves data unchanged."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.infer_block_boundary_columns(
        df,
        exclude_columns=exclude_columns,
        metric_columns_hint=metric_columns_hint,
        min_multi_row_blocks=min_multi_row_blocks,
        min_nonempty_row_rate=min_nonempty_row_rate,
        max_nonempty_row_rate=max_nonempty_row_rate,
        min_mean_segment_len=min_mean_segment_len,
        max_pair_search=max_pair_search,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.classify_metric_level", "transform.allocate_block_metric", "transform.rename"],
    )

@mcp.tool(name="transform.unpivot")
def unpivot_columns(
    data: List[Dict[str, Any]],
    id_cols: List[str],
    value_cols: Optional[List[str]] = None,
    var_name: str = "Variable",
    value_name: str = "Value"
) -> Dict[str, Any]:
    """
    Unpivot a wide Table (List of Dicts) into a long format (Melt).
    Use when multiple columns represent the same dimension (e.g., 'Jan', 'Feb', 'Mar' columns representing 'Date').
    
    Args:
        data: The current Table (List of Dicts).
        id_cols: List of column NAMES to keep as identifiers (dimensions that are ALREADY flat).
        value_cols: List of column NAMES to unpivot into rows. If omitted, unpivots all non-id columns.
        var_name: Name for the new dimension column created (e.g., 'Month').
        value_name: Name for the new metric column created (e.g., 'Sales Amount').
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.unpivot_columns(df, id_cols=id_cols, value_cols=value_cols, var_name=var_name, value_name=value_name)
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["transform.type_cast", "transform.filter_empty"])


@mcp.tool(name="transform.skip_rows")
def skip_rows(
    data: List[Dict[str, Any]],
    rows_to_skip: List[int]
) -> Dict[str, Any]:
    """Remove specified rows by index."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.skip_rows(df, rows_to_skip=rows_to_skip)
    return _result_to_dict(result, start_time, rows_in=rows_in)


@mcp.tool(name="transform.filter_summaries")
def filter_summary_rows(
    data: List[Dict[str, Any]],
    keywords: Optional[List[str]] = None,
    use_structural_detection: bool = True,
    numeric_threshold: float = 0.7
) -> Dict[str, Any]:
    """Remove total and summary rows from the dataset."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.filter_summary_rows(df, keywords=keywords, use_structural_detection=use_structural_detection, numeric_threshold=numeric_threshold)
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["transform.filter_empty", "transform.rename"])


@mcp.tool(name="transform.filter_empty")
def filter_empty_rows(
    data: List[Dict[str, Any]],
    check_columns: Optional[List[str]] = None,
    metric_columns: Optional[List[str]] = None,
    all_must_be_empty: bool = True,
    require_metrics: bool = False,
    min_populated_columns: int = 2
) -> Dict[str, Any]:
    """Remove rows where specified columns are empty or lack metric values."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.filter_empty_rows(
        df, 
        check_columns=check_columns, 
        metric_columns=metric_columns,
        all_must_be_empty=all_must_be_empty,
        require_metrics=require_metrics,
        min_populated_columns=min_populated_columns
    )
    return _result_to_dict(result, start_time, rows_in=rows_in)


@mcp.tool(name="layout.filter_header_repeats")
def filter_header_rows(
    data: List[Dict[str, Any]],
    threshold: float = 0.5
) -> Dict[str, Any]:
    """Remove rows that are repetitions of the column headers."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.filter_header_rows(df, threshold=threshold)
    return _result_to_dict(result, start_time, rows_in=rows_in)


@mcp.tool(name="layout.merge_headers")
def merge_header_rows(
    data: List[Dict[str, Any]],
    header_rows: List[int],
    separator: str = " - "
) -> Dict[str, Any]:
    """Merge multiple rows into a single header row."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.merge_header_rows(df, header_rows=header_rows, separator=separator)
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["transform.fill_merged"])


@mcp.tool(name="transform.rename")
def rename_columns(
    data: List[Dict[str, Any]],
    mapping: Dict[str, str]
) -> Dict[str, Any]:
    """Rename columns."""
    from sia.agent.target_template_utils import sanitize_rename_mapping

    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.rename_columns(df, sanitize_rename_mapping(mapping))
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["transform.type_cast"])


@mcp.tool(name="transform.align_schema")
def align_schema(
    data: List[Dict[str, Any]],
    target_columns: List[str],
    aliases: Optional[Dict[str, Any]] = None,
    fill_value: Any = None,
) -> Dict[str, Any]:
    """Reorder/rename columns to a fixed target list for predictable pd.concat (optional aliases, fill missing)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.align_schema(
        df, target_columns=target_columns, aliases=aliases, fill_value=fill_value
    )
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["transform.type_cast", "verify.schema"])


@mcp.tool(name="transform.drop_columns")
def drop_columns(
    data: List[Dict[str, Any]],
    columns: List[str]
) -> Dict[str, Any]:
    """Drop specified columns by name."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.drop_columns(df, columns=columns)
    return _result_to_dict(result, start_time, rows_in=rows_in)


@mcp.tool(name="transform.drop_blank_columns")
def drop_blank_columns(
    data: List[Dict[str, Any]],
    threshold: float = 1.0
) -> Dict[str, Any]:
    """Automatically remove columns that are entirely or mostly blank."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.drop_blank_columns(df, threshold=threshold)
    return _result_to_dict(result, start_time, rows_in=rows_in)


@mcp.tool(name="transform.type_cast")
def type_cast_columns(
    data: List[Dict[str, Any]],
    type_map: Dict[str, Literal["numeric", "date", "string", "integer"]]
) -> Dict[str, Any]:
    """
    Cast columns in a Table (List of Dicts) to standardized data types.
    Cleans messy currency ($, %), commas, and date formats automatically.
    
    Args:
        data: The current Table (List of Dicts).
        type_map: Mapping of {column_name: type}. Valid types: 'numeric' (float), 'date', 'string', 'integer'.
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.type_cast_columns(df, type_map=type_map)
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["transform.deduplicate"])


@mcp.tool(name="layout.stack")
def stack_tables(
    data: List[List[Any]],
    header_row: int,
    blocks: List[Dict[str, int]]
) -> Dict[str, Any]:
    """Stack multiple data blocks (tables) into one using explicit coordinates."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.stack_tables(df, header_row=header_row, blocks=blocks)
    return _result_to_dict(result, start_time, rows_in=rows_in, 
                         next_hints=["transform.fill_merged", "transform.filter_summaries", "transform.filter_empty"])


@mcp.tool(name="layout.unpivot_matrix")
def crosstab_unpivot(
    data: List[List[Any]],
    row_header_col: int = 0,
    col_header_row: int = 0,
    data_start_row: int = 1,
    data_start_col: int = 1,
    row_dim_name: str = "Row",
    col_dim_name: str = "Column",
    value_name: str = "Value"
) -> Dict[str, Any]:
    """Unpivot a crosstab/matrix where both row and column headers represent dimensions."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.crosstab_unpivot(df, row_header_col, col_header_row, data_start_row, data_start_col, row_dim_name, col_dim_name, value_name)
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["transform.type_cast"])


@mcp.tool(name="transform.transpose")
def transpose_data(
    data: List[Dict[str, Any]],
    header_col: Optional[int] = None,
    new_index_name: str = "Column"
) -> Dict[str, Any]:
    """Transpose the DataFrame (swap rows and columns)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.transpose_data(df, header_col=header_col, new_index_name=new_index_name)
    return _result_to_dict(result, start_time, rows_in=rows_in)


@mcp.tool(name="transform.deduplicate")
def deduplicate_rows(
    data: List[Dict[str, Any]],
    subset: Optional[List[str]] = None,
    keep: str = "first"
) -> Dict[str, Any]:
    """Remove duplicate rows based on specified columns."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.deduplicate_rows(df, subset=subset, keep=keep)
    return _result_to_dict(result, start_time, rows_in=rows_in)


@mcp.tool(name="transform.union_resolve")
def union_resolve(
    data: List[Dict[str, Any]],
    key_columns: List[Any],
    policy: str = "first",
    metric_columns: Optional[List[Any]] = None,
    source_tag_column: Optional[str] = None,
    prefer_source_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Keyed dedupe / conflict resolution after stacking multiple sources (first|sum|max|min|prefer_source)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.union_resolve(
        df,
        key_columns=key_columns,
        policy=policy,
        metric_columns=metric_columns,
        source_tag_column=source_tag_column,
        prefer_source_id=prefer_source_id,
    )
    return _result_to_dict(result, start_time, rows_in=rows_in, next_hints=["verify.schema"])


# ===== LAST-MILE TOOLS =====

@mcp.tool(name="transform.add_column")
def add_column_tool(
    data: List[Dict[str, Any]],
    target_column: str,
    value: Any,
    when: Literal["missing", "null_or_blank", "always"] = "missing",
) -> Dict[str, Any]:
    """
    Add or fill a column with a literal value (e.g. UID hierarchy default ``publisher='total'``).

    Use when the column does not exist in source data — not ``transform.calculate`` or ``map_values``.
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.add_column(df, target_column=target_column, value=value, when=when)
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.type_cast", "transform.aggregate_weekly", "verify.schema"],
    )


@mcp.tool(name="transform.apply_column_rules")
def apply_column_rules_tool(
    data: List[Dict[str, Any]],
    column_rules: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Apply target-template ``business_logic.column_rules`` (set_value when column missing or null).

    Pass the ``column_rules`` array from the target template, or rely on runtime injection from job template.
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.apply_column_rules(df, column_rules=column_rules)
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.type_cast", "transform.aggregate_weekly", "verify.schema"],
    )


@mcp.tool(name="transform.map_values")
def map_values(
    data: List[Dict[str, Any]],
    column: str,
    mapping: Dict[str, str],
    case_insensitive: bool = True,
    default: Optional[str] = None
) -> Dict[str, Any]:
    """Map messy source values to standardized target values (e.g., 'FB' -> 'social')."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.map_values(df, column=column, mapping=mapping,
                                            case_insensitive=case_insensitive, default=default)
    return _result_to_dict(result, start_time, rows_in=rows_in,
                          next_hints=["verify.schema"])


@mcp.tool(name="transform.calculate")
def calculate_column(
    data: List[Dict[str, Any]],
    target_column: str,
    expression: str,
    source_columns: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Create or overwrite a column using arithmetic (e.g., 'Spend * 0.85', 'Impressions * 1000')."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.calculate_column(df, target_column=target_column,
                                                  expression=expression,
                                                  source_columns=source_columns)
    return _result_to_dict(result, start_time, rows_in=rows_in,
                          next_hints=["transform.format", "transform.rename"])


@mcp.tool(name="transform.scale_values")
def scale_values(
    data: List[Dict[str, Any]],
    scales: Dict[str, float],
) -> Dict[str, Any]:
    """Multiply columns by denomination factors (e.g. thousands in header → ×1000)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.scale_columns(df, scales=scales)
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.format", "transform.aggregate_weekly", "verify.schema"],
    )


@mcp.tool(name="transform.date_range_to_weekly")
def date_range_to_weekly(
    data: List[Dict[str, Any]],
    start_date_col: str,
    end_date_col: str,
    value_cols: List[str],
    id_cols: Optional[List[str]] = None,
    granularity: Literal["weekly", "daily"] = "weekly",
    week_start_col: str = "week_start",
    week_end_col: Optional[str] = None,
    days_in_period_col: str = "days_in_week",
    date_column: str = "calendar_date",
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Split each row's [start_date, end_date] range into equal daily amounts, then sum into ISO weeks
    (Monday start, Sunday end) or return one row per calendar day (granularity=daily).
    Optional row_filters (e.g. campaign_status eq active) apply before allocation.
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.date_range_to_weekly(
        df,
        start_date_col=start_date_col,
        end_date_col=end_date_col,
        value_cols=value_cols,
        id_cols=id_cols,
        granularity=granularity,
        week_start_col=week_start_col,
        week_end_col=week_end_col,
        days_in_period_col=days_in_period_col,
        date_column=date_column,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.format", "transform.rename", "verify.checksum"],
    )


@mcp.tool(name="transform.expand_date_range_to_daily")
def expand_date_range_to_daily(
    data: List[Dict[str, Any]],
    start_date_col: str,
    end_date_col: str,
    value_cols: List[str],
    id_cols: Optional[List[str]] = None,
    week_start_col: str = "week_start",
    week_end_col: Optional[str] = None,
    days_in_period_col: str = "days_in_week",
    date_column: str = "calendar_date",
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Inclusive start/end columns → daily rows with prorated metrics (then aggregate_weekly if target is weekly)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.expand_date_range_to_daily(
        df,
        start_date_col=start_date_col,
        end_date_col=end_date_col,
        value_cols=value_cols,
        id_cols=id_cols,
        week_start_col=week_start_col,
        week_end_col=week_end_col,
        days_in_period_col=days_in_period_col,
        date_column=date_column,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.aggregate_weekly", "transform.format", "verify.checksum"],
    )


@mcp.tool(name="transform.expand_date_range_to_weekly")
def expand_date_range_to_weekly(
    data: List[Dict[str, Any]],
    start_date_col: str,
    end_date_col: str,
    value_cols: List[str],
    id_cols: Optional[List[str]] = None,
    week_start_col: str = "week_start",
    week_end_col: Optional[str] = None,
    days_in_period_col: str = "days_in_week",
    date_column: str = "calendar_date",
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Inclusive start/end columns → Monday–Sunday weekly buckets with per-day proration."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.expand_date_range_to_weekly(
        df,
        start_date_col=start_date_col,
        end_date_col=end_date_col,
        value_cols=value_cols,
        id_cols=id_cols,
        week_start_col=week_start_col,
        week_end_col=week_end_col,
        days_in_period_col=days_in_period_col,
        date_column=date_column,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.format", "transform.rename", "verify.schema"],
    )


@mcp.tool(name="transform.aggregate_weekly")
def aggregate_weekly(
    data: List[Dict[str, Any]],
    date_col: str,
    group_by_cols: Optional[List[str]] = None,
    metric_rules: Optional[Dict[str, str]] = None,
    value_cols: Optional[List[str]] = None,
    week_start_col: Optional[str] = None,
    week_end_col: Optional[str] = None,
    drop_original_date: bool = True,
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Aggregate a dataframe with an existing daily/date column into Monday–Sunday
    ISO weekly buckets. Use this when rows already have a single date column
    (not a start/end range). metric_rules maps metric column -> aggregation rule
    (sum, mean, min, max, first, last, count). If metric_rules is omitted,
    value_cols default to sum. When week_start_col is omitted (or the legacy
    default ``"week_start"`` is passed while ``drop_original_date`` is true and
    ``date_col`` has another name), the weekly Monday date is written under the
    input date column name so you do not gain an extra ``week_start`` column.
    """
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.aggregate_weekly(
        df,
        date_col=date_col,
        group_by_cols=group_by_cols,
        metric_rules=metric_rules,
        value_cols=value_cols,
        week_start_col=week_start_col,
        week_end_col=week_end_col,
        drop_original_date=drop_original_date,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.format", "transform.rename", "verify.schema"],
    )


@mcp.tool(name="transform.build_date_from_parts")
def build_date_from_parts(
    data: List[Dict[str, Any]],
    year_col: str,
    month_col: Optional[str] = None,
    day_col: Optional[str] = None,
    quarter_col: Optional[str] = None,
    target_date_col: str = "calendar_date",
    default_day: int = 1,
    drop_source_columns: bool = False,
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a real date column from separate year/month/day columns, or from year + quarter."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.build_date_from_parts(
        df,
        year_col=year_col,
        month_col=month_col,
        day_col=day_col,
        quarter_col=quarter_col,
        target_date_col=target_date_col,
        default_day=default_day,
        drop_source_columns=drop_source_columns,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.type_cast", "transform.format", "transform.aggregate_weekly"],
    )


@mcp.tool(name="transform.expand_period_to_daily")
def expand_period_to_daily(
    data: List[Dict[str, Any]],
    date_col: str,
    value_cols: List[str],
    input_granularity: Literal["monthly", "quarterly"],
    id_cols: Optional[List[str]] = None,
    date_column: str = "calendar_date",
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Expand monthly or quarterly period rows into daily rows by equal daily proration."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.expand_period_to_daily(
        df,
        date_col=date_col,
        value_cols=value_cols,
        input_granularity=input_granularity,
        id_cols=id_cols,
        date_column=date_column,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.aggregate_weekly", "transform.format", "verify.schema"],
    )


@mcp.tool(name="transform.infer_granularity_expand_to_daily")
def infer_granularity_expand_to_daily(
    data: List[Dict[str, Any]],
    date_col: str,
    value_cols: List[str],
    id_cols: Optional[List[str]] = None,
    date_column: str = "calendar_date",
    min_confidence: float = 0.35,
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Infer date cadence from values, then expand monthly/quarterly/weekly rows to daily (daily = no-op)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.infer_granularity_expand_to_daily(
        df,
        date_col=date_col,
        value_cols=value_cols,
        id_cols=id_cols,
        date_column=date_column,
        min_confidence=min_confidence,
        row_filters=row_filters,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.aggregate_weekly", "transform.format", "verify.schema"],
    )


@mcp.tool(name="transform.format")
def format_columns(
    data: List[Dict[str, Any]],
    format_map: Dict[str, str]
) -> Dict[str, Any]:
    """Apply formatting to columns (date:YYYY-MM-DD, numeric:2, integer, lowercase, uppercase)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.format_columns(df, format_map=format_map)
    return _result_to_dict(result, start_time, rows_in=rows_in,
                          next_hints=["transform.reorder_columns", "transform.sort_rows", "verify.schema"])


@mcp.tool(name="transform.reorder_columns")
def reorder_columns_tool(
    data: List[Dict[str, Any]],
    column_order: List[str],
) -> Dict[str, Any]:
    """Reorder columns to template order; unlisted columns trail at the end."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.reorder_columns_layout(df, column_order=column_order)
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.sort_rows", "verify.schema"],
    )


@mcp.tool(name="transform.sort_rows")
def sort_rows_tool(
    data: List[Dict[str, Any]],
    sort_columns: List[str],
    ascending: bool = True,
) -> Dict[str, Any]:
    """Sort rows by uid hierarchy columns (stable; dates coerced when possible)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.sort_rows(df, sort_columns=sort_columns, ascending=ascending)
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["verify.schema"],
    )


@mcp.tool(name="transform.fuzzy_standardize")
def fuzzy_standardize_tool(
    data: List[Dict[str, Any]],
    column: Any,
    threshold: float = 0.85,
    case_sensitive: bool = False,
) -> Dict[str, Any]:
    """Normalize inconsistent labels within a column using fuzzy grouping (difflib)."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.fuzzy_standardize_column(
        df,
        column=column,
        threshold=threshold,
        case_sensitive=case_sensitive,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.map_values", "transform.format", "verify.schema"],
    )


@mcp.tool(name="transform.split_column")
def split_column_tool(
    data: List[Dict[str, Any]],
    column: Any,
    new_names: List[str],
    separator: str = " - ",
    remove_original: bool = True,
) -> Dict[str, Any]:
    """Split one text column into multiple columns using a separator."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.split_column(
        df,
        column=column,
        new_names=new_names,
        separator=separator,
        remove_original=remove_original,
    )
    return _result_to_dict(
        result,
        start_time,
        rows_in=rows_in,
        next_hints=["transform.rename", "transform.type_cast", "verify.schema"],
    )


@mcp.tool(name="verify.schema")
def validate_against_schema(
    data: List[Dict[str, Any]],
    schema: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate a DataFrame against a JSON Schema. Checks mandatory columns, types, enums, and min/max."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.validate_against_schema(df, schema=schema)
    return _result_to_dict(result, start_time, rows_in=rows_in)


@mcp.tool(name="validate.cross_source_union")
def validate_cross_source_union(
    data: List[Dict[str, Any]],
    column_sets_by_source: Dict[str, List[str]],
    key_hints: Optional[List[str]] = None,
    dtype_map_by_source: Optional[Dict[str, Dict[str, str]]] = None,
    date_granularity_by_source: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Read-only union readiness (column intersection, optional dtypes, date grain); current table unchanged."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.validate_cross_source_union_tool(
        df,
        column_sets_by_source=column_sets_by_source,
        key_hints=key_hints,
        dtype_map_by_source=dtype_map_by_source,
        date_granularity_by_source=date_granularity_by_source,
    )
    return _result_to_dict(result, start_time, rows_in=rows_in)


# ===== ATOMIC SKILLS =====

@mcp.tool(name="discovery.inventory")
def get_file_inventory(
    file_path: str
) -> Dict[str, Any]:
    """List all sheets, named ranges, hidden tabs, and dimensions of an Excel workbook."""
    start_time = time.time()
    result = TransformationTools.get_file_inventory(file_path)
    duration_ms = int((time.time() - start_time) * 1000)
    return {
        "ok": result.success,
        "success": result.success,
        "data": result.data,
        "context": {
            "summary": result.message,
            "metrics": {"elapsed_ms": duration_ms},
            "next_hints": ["layout.extract", "transform.fill_merged"]
        },
        "errors": [] if result.success else [{"code": "TOOL_ERROR", "message": result.message}]
    }


@mcp.tool(name="discovery.inspect")
def inspect_sheet_structure(
    file_path: str,
    sheet_name: Optional[str] = None
) -> Dict[str, Any]:
    """Deterministic structural scan of an Excel sheet: merged cells, spacers, headers, data regions, noise score."""
    start_time = time.time()
    result = TransformationTools.inspect_sheet_structure(file_path, sheet_name=sheet_name)
    duration_ms = int((time.time() - start_time) * 1000)
    return {
        "ok": result.success,
        "success": result.success,
        "data": result.data,
        "context": {
            "summary": result.message,
            "metrics": {"elapsed_ms": duration_ms},
            "next_hints": ["layout.extract", "transform.fill_merged"]
        },
        "errors": [] if result.success else [{"code": "TOOL_ERROR", "message": result.message}]
    }






@mcp.tool(name="transform.align_columns")
def fuzzy_column_align(
    source_columns: List[str],
    target_columns: List[str]
) -> Dict[str, Any]:
    """Align column names between two schemas using fuzzy matching and synonyms."""
    start_time = time.time()
    result = TransformationTools.fuzzy_column_align(source_columns, target_columns)
    duration_ms = int((time.time() - start_time) * 1000)
    return {
        "ok": result.success,
        "success": result.success,
        "data": result.data,
        "context": {
            "summary": result.message,
            "metrics": {"elapsed_ms": duration_ms},
            "next_hints": ["transform.rename"]
        },
        "errors": [] if result.success else [{"code": "TOOL_ERROR", "message": result.message}]
    }


@mcp.tool(name="verify.checksum")
def verify_checksum(
    data: List[Dict[str, Any]],
    checksum_column: str,
    expected_total: float,
    tolerance: float = 0.01
) -> Dict[str, Any]:
    """Verify data integrity by comparing column sum against expected total."""
    start_time = time.time()
    rows_in = len(data)
    df = pd.DataFrame(data)
    result = TransformationTools.verify_checksum(df, checksum_column=checksum_column,
                                                  expected_total=expected_total,
                                                  tolerance=tolerance)
    duration_ms = int((time.time() - start_time) * 1000)
    return {
        "ok": result.success,
        "success": result.success,
        "data": result.data,
        "context": {
            "summary": result.message,
            "metrics": {"elapsed_ms": duration_ms},
            "next_hints": []
        },
        "errors": [] if result.success else [{"code": "TOOL_ERROR", "message": result.message}]
    }


if __name__ == "__main__":
    mcp.run()

