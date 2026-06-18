"""Cross-source derived fields for SchemaAgent.

This module defines how a new column on one source can be derived from
normalized data on another source. It introduces:

- :class:`DerivedFieldPlan` - the serializable plan an analyst approves.
- :func:`validate_derived_field_plan` - checks that every referenced
  ``source_id.column`` exists and that there is an approved join path.
- :func:`apply_derived_field_plan` - materializes the new column against
  the target dataframe by joining upstream artifacts using the approved
  join keys.

The expression grammar is intentionally minimal for v1 so the LLM cannot
invent unsafe computations:

- ``<source_id>.<column_name>`` - pull ``column_name`` from source ``source_id``
  via the approved join path, optionally falling back to ``fallback`` when
  no row matches.
- ``COALESCE(<expr>, <expr>, ...)`` - try each expression left to right.
- ``literal:"<string>"`` or ``literal:<number>`` - constant fallback value.

Arithmetic and case expressions are out of scope for v1. They can be
added later behind the same validator/executor contract without changing
callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from .source_graph import JoinPath, SourceGraph


DERIVED_TOKEN_PATTERN = re.compile(r"^([A-Za-z0-9_:\-]+)\.([A-Za-z0-9_]+)$")
COALESCE_PATTERN = re.compile(r"^\s*COALESCE\s*\((.*)\)\s*$", re.IGNORECASE)
LITERAL_PATTERN = re.compile(r"^\s*literal\s*:\s*(.*)\s*$", re.IGNORECASE)


@dataclass
class DerivedFieldPlan:
    """Analyst-approved plan for a single cross-source derived column."""

    derived_field_id: str
    target_source_id: str
    target_column: str
    expression: str
    join_context: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[Dict[str, str]] = field(default_factory=list)  # [{"source_id": ..., "column": ...}]
    fallback: Optional[str] = None
    status: str = "proposed"
    approved_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "derived_field_id": self.derived_field_id,
            "target_source_id": self.target_source_id,
            "target_column": self.target_column,
            "expression": self.expression,
            "join_context": dict(self.join_context),
            "dependencies": [dict(dep) for dep in self.dependencies],
            "fallback": self.fallback,
            "status": self.status,
            "approved_by": self.approved_by,
        }


@dataclass
class ValidationIssue:
    code: str
    message: str


@dataclass
class ValidationResult:
    ok: bool
    issues: List[ValidationIssue] = field(default_factory=list)
    resolved_dependencies: List[Dict[str, str]] = field(default_factory=list)
    join_paths: Dict[str, List[str]] = field(default_factory=dict)

    def error_messages(self) -> List[str]:
        return [issue.message for issue in self.issues]


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
def _parse_literal(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _split_top_level_args(inner: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current = []
    in_quote: Optional[str] = None
    for char in inner:
        if in_quote:
            current.append(char)
            if char == in_quote:
                in_quote = None
            continue
        if char in {'"', "'"}:
            in_quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
            current.append(char)
            continue
        if char == ")":
            depth -= 1
            current.append(char)
            continue
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def parse_expression(expression: str) -> Dict[str, Any]:
    """Parse a derived-field expression into a small AST.

    Node shapes:
    - ``{"kind": "ref", "source_id": str, "column": str}``
    - ``{"kind": "literal", "value": Any}``
    - ``{"kind": "coalesce", "args": [Node, ...]}``
    """
    expression = (expression or "").strip()
    if not expression:
        raise ValueError("expression is empty")

    coalesce = COALESCE_PATTERN.match(expression)
    if coalesce:
        inner = coalesce.group(1)
        args = _split_top_level_args(inner)
        if not args:
            raise ValueError("COALESCE requires at least one argument")
        return {"kind": "coalesce", "args": [parse_expression(arg) for arg in args]}

    literal = LITERAL_PATTERN.match(expression)
    if literal:
        return {"kind": "literal", "value": _parse_literal(literal.group(1))}

    token = DERIVED_TOKEN_PATTERN.match(expression)
    if token:
        return {"kind": "ref", "source_id": token.group(1), "column": token.group(2)}

    raise ValueError(
        f"Unsupported expression fragment: {expression!r}. "
        "Supported: '<source_id>.<column>', 'COALESCE(...)', 'literal:<value>'."
    )


def _collect_refs(node: Mapping[str, Any]) -> List[Tuple[str, str]]:
    kind = node.get("kind")
    if kind == "ref":
        return [(node["source_id"], node["column"])]
    if kind == "coalesce":
        refs: List[Tuple[str, str]] = []
        for arg in node.get("args", []) or []:
            refs.extend(_collect_refs(arg))
        return refs
    return []


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def validate_derived_field_plan(
    plan: DerivedFieldPlan,
    graph: SourceGraph,
) -> ValidationResult:
    """Validate ``plan`` against a :class:`SourceGraph`.

    Returns :class:`ValidationResult` with either ``ok=True`` or a list of
    human-readable issues. All referenced ``source_id.column`` tokens must
    exist in the graph, and for every upstream source there must be an
    approved join path from ``target_source_id``.
    """
    issues: List[ValidationIssue] = []
    resolved: List[Dict[str, str]] = []
    join_paths: Dict[str, List[str]] = {}

    if not plan.target_source_id:
        issues.append(ValidationIssue("missing_target", "target_source_id is required"))
    if not plan.target_column:
        issues.append(ValidationIssue("missing_column", "target_column is required"))

    if graph.get_node(plan.target_source_id) is None:
        issues.append(
            ValidationIssue(
                "unknown_target_source",
                f"Target source {plan.target_source_id!r} is not registered in the source graph",
            )
        )

    try:
        ast = parse_expression(plan.expression)
    except ValueError as exc:
        issues.append(ValidationIssue("parse_error", str(exc)))
        return ValidationResult(ok=False, issues=issues)

    refs = _collect_refs(ast)
    if not refs:
        if not (plan.fallback or ast.get("kind") == "literal"):
            issues.append(
                ValidationIssue(
                    "no_refs",
                    "Expression does not reference any source column and has no literal fallback",
                )
            )

    for source_id, column in refs:
        node = graph.get_node(source_id)
        if node is None:
            issues.append(
                ValidationIssue(
                    "unknown_source",
                    f"Referenced source {source_id!r} is not in the source graph",
                )
            )
            continue
        if column not in node.columns:
            issues.append(
                ValidationIssue(
                    "unknown_column",
                    f"Referenced column {source_id}.{column!r} is not available in the source graph",
                )
            )
            continue
        if source_id == plan.target_source_id:
            resolved.append({"source_id": source_id, "column": column})
            continue
        path = graph.join_path(plan.target_source_id, source_id)
        if path.is_empty:
            issues.append(
                ValidationIssue(
                    "no_join_path",
                    f"No approved join path from {plan.target_source_id!r} to {source_id!r}",
                )
            )
            continue
        resolved.append({"source_id": source_id, "column": column})
        join_paths[source_id] = [edge.relationship_id for edge in path.steps]

    return ValidationResult(
        ok=not issues,
        issues=issues,
        resolved_dependencies=resolved,
        join_paths=join_paths,
    )


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------
def apply_derived_field_plan(
    plan: DerivedFieldPlan,
    *,
    graph: SourceGraph,
    target_frame: pd.DataFrame,
    frames_by_source: Mapping[str, pd.DataFrame],
) -> pd.DataFrame:
    """Materialize the derived field on ``target_frame``.

    Raises :class:`ValueError` if validation fails or if any referenced
    frame is missing from ``frames_by_source``. The returned frame is a
    copy of ``target_frame`` with ``plan.target_column`` assigned.
    """
    validation = validate_derived_field_plan(plan, graph)
    if not validation.ok:
        raise ValueError(
            f"Derived field plan {plan.derived_field_id!r} is invalid: "
            + "; ".join(validation.error_messages())
        )

    ast = parse_expression(plan.expression)
    result = target_frame.copy()
    result[plan.target_column] = _evaluate(
        ast,
        plan=plan,
        graph=graph,
        target_frame=result,
        frames_by_source=frames_by_source,
    )
    if plan.fallback is not None:
        fallback_value = _parse_literal(plan.fallback) if isinstance(plan.fallback, str) else plan.fallback
        result[plan.target_column] = result[plan.target_column].where(
            result[plan.target_column].notna(),
            fallback_value,
        )
    return result


def _evaluate(
    node: Mapping[str, Any],
    *,
    plan: DerivedFieldPlan,
    graph: SourceGraph,
    target_frame: pd.DataFrame,
    frames_by_source: Mapping[str, pd.DataFrame],
) -> pd.Series:
    kind = node.get("kind")
    if kind == "literal":
        return pd.Series([node.get("value")] * len(target_frame), index=target_frame.index)
    if kind == "ref":
        return _resolve_ref(
            node["source_id"],
            node["column"],
            plan=plan,
            graph=graph,
            target_frame=target_frame,
            frames_by_source=frames_by_source,
        )
    if kind == "coalesce":
        series: Optional[pd.Series] = None
        for arg in node.get("args", []) or []:
            candidate = _evaluate(
                arg,
                plan=plan,
                graph=graph,
                target_frame=target_frame,
                frames_by_source=frames_by_source,
            )
            if series is None:
                series = candidate
            else:
                series = series.where(series.notna(), candidate)
        return series if series is not None else pd.Series([None] * len(target_frame))
    raise ValueError(f"Unsupported AST node: {node}")


def _resolve_ref(
    source_id: str,
    column: str,
    *,
    plan: DerivedFieldPlan,
    graph: SourceGraph,
    target_frame: pd.DataFrame,
    frames_by_source: Mapping[str, pd.DataFrame],
) -> pd.Series:
    if source_id == plan.target_source_id:
        if column not in target_frame.columns:
            raise ValueError(f"Column {column!r} missing on target frame")
        return target_frame[column]
    if source_id not in frames_by_source:
        raise ValueError(f"Upstream frame for source {source_id!r} not provided")
    upstream = frames_by_source[source_id]
    path = graph.join_path(plan.target_source_id, source_id)
    if path.is_empty:
        raise ValueError(f"No approved join path for upstream source {source_id!r}")

    join_keys = _collapse_join_keys(path, target_source_id=plan.target_source_id, upstream_id=source_id)
    if not join_keys:
        raise ValueError(f"Join path for {source_id!r} has no executable join keys")

    left_on = [pair["from"] for pair in join_keys]
    right_on = [pair["to"] for pair in join_keys]
    missing_left = [col for col in left_on if col not in target_frame.columns]
    missing_right = [col for col in right_on if col not in upstream.columns]
    if missing_left or missing_right:
        raise ValueError(
            f"Join keys missing (left={missing_left}, right={missing_right}) for derived field "
            f"{plan.derived_field_id!r}"
        )
    if column not in upstream.columns:
        raise ValueError(f"Upstream column {source_id}.{column!r} missing")

    lookup = upstream[right_on + [column]].drop_duplicates(subset=right_on)
    merged = target_frame[left_on].merge(
        lookup,
        how="left",
        left_on=left_on,
        right_on=right_on,
    )
    return merged[column].reset_index(drop=True)


def _collapse_join_keys(
    path: JoinPath,
    *,
    target_source_id: str,
    upstream_id: str,
) -> List[Dict[str, str]]:
    """Collapse a multi-hop join path into ``(target_column, upstream_column)`` pairs.

    For v1 we only support single-hop joins and concatenate compatible keys
    for multi-hop cases where each hop shares the same key names.
    """
    if not path.steps:
        return []
    if len(path.steps) == 1:
        edge = path.steps[0]
        if edge.from_source_id == target_source_id and edge.to_source_id == upstream_id:
            return [dict(entry) for entry in edge.join_keys]
        return [
            {"from": entry.get("to", ""), "to": entry.get("from", "")}
            for entry in edge.join_keys
            if entry.get("from") and entry.get("to")
        ]
    # For longer paths, require that every hop uses the same join key names.
    first_keys = path.steps[0].join_keys
    for step in path.steps[1:]:
        if [pair.get("from") for pair in step.join_keys] != [pair.get("from") for pair in first_keys]:
            return []
    return [dict(entry) for entry in first_keys]
