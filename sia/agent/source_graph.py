"""Executable source graph for SchemaAgent.

This module turns approved relationship records into a runtime graph that
the planner, executor, and collation stage can query. It is intentionally
decoupled from :mod:`sia.agent.relationships` so the heuristic proposal
layer can keep producing suggestions while the runtime only trusts
approved edges with explicit ``join_keys``.

Core capabilities:
- Build a :class:`SourceGraph` from persisted source summaries and
  approved :class:`sia.storage.RelationshipRecord` entries.
- Query available fields per source.
- Look up executable join paths between two sources.
- Collate per-source normalized frames into a single dataset using the
  approved join keys rather than heuristic column intersections.

The graph treats each source as a node with a role (``fact``, ``lookup``,
``reference``, ``dimension``, ``parallel_main``) and each relationship as
a directed or bidirectional edge with explicit join keys. Unapproved
relationships are ignored at runtime.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd


APPROVED_STATUSES = {"approved", "active"}
JOIN_KINDS = {"join", "lookup", "lookup/reference", "reference"}
UNION_KINDS = {"union"}
NON_OUTPUT_ROLES = {"lookup", "reference", "dimension"}


@dataclass
class SourceNode:
    """A node in the source graph."""

    source_id: str
    role: str = "unknown"
    file_name: str = ""
    sheet_name: Optional[str] = None
    columns: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RelationshipEdge:
    """An approved, executable relationship edge."""

    relationship_id: str
    from_source_id: str
    to_source_id: str
    relationship_kind: str
    direction: str = "bidirectional"
    join_keys: List[Dict[str, str]] = field(default_factory=list)
    cardinality: str = "unknown"
    confidence: float = 0.0

    @property
    def is_join_like(self) -> bool:
        return self.relationship_kind.lower() in JOIN_KINDS

    @property
    def is_union(self) -> bool:
        return self.relationship_kind.lower() in UNION_KINDS

    def allows_traversal(self, from_id: str, to_id: str) -> bool:
        """Does this edge allow traversal from ``from_id`` to ``to_id``?"""
        if self.direction == "bidirectional":
            return {from_id, to_id} == {self.from_source_id, self.to_source_id}
        if self.direction == "from_to":
            return from_id == self.from_source_id and to_id == self.to_source_id
        if self.direction == "to_from":
            return from_id == self.to_source_id and to_id == self.from_source_id
        return False


@dataclass
class JoinPath:
    """A validated join path between two sources."""

    steps: List[RelationshipEdge] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.steps


class SourceGraph:
    """Queryable graph of sources and approved relationships."""

    def __init__(self, nodes: Sequence[SourceNode], edges: Sequence[RelationshipEdge]):
        self._nodes: Dict[str, SourceNode] = {node.source_id: node for node in nodes}
        self._edges: List[RelationshipEdge] = list(edges)
        self._adj: Dict[str, List[Tuple[str, RelationshipEdge]]] = defaultdict(list)
        for edge in self._edges:
            if edge.direction in {"bidirectional", "from_to"}:
                self._adj[edge.from_source_id].append((edge.to_source_id, edge))
            if edge.direction in {"bidirectional", "to_from"}:
                self._adj[edge.to_source_id].append((edge.from_source_id, edge))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @property
    def nodes(self) -> List[SourceNode]:
        return list(self._nodes.values())

    @property
    def edges(self) -> List[RelationshipEdge]:
        return list(self._edges)

    def get_node(self, source_id: str) -> Optional[SourceNode]:
        return self._nodes.get(source_id)

    def available_fields(self, source_id: str) -> List[str]:
        """Return the known columns for ``source_id`` (empty if unknown)."""
        node = self._nodes.get(source_id)
        return list(node.columns) if node else []

    def reachable_sources(self, source_id: str) -> List[str]:
        """Return every source reachable from ``source_id`` via approved edges."""
        if source_id not in self._nodes:
            return []
        seen = {source_id}
        order: List[str] = []
        queue = deque([source_id])
        while queue:
            current = queue.popleft()
            for neighbor, _edge in self._adj.get(current, []):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                order.append(neighbor)
                queue.append(neighbor)
        return order

    def join_path(self, from_id: str, to_id: str) -> JoinPath:
        """Return the shortest approved, join-like path between two sources."""
        if from_id == to_id or from_id not in self._nodes or to_id not in self._nodes:
            return JoinPath()
        visited = {from_id: None}  # type: Dict[str, Optional[Tuple[str, RelationshipEdge]]]
        queue = deque([from_id])
        while queue:
            current = queue.popleft()
            if current == to_id:
                break
            for neighbor, edge in self._adj.get(current, []):
                if not edge.is_join_like:
                    continue
                if neighbor in visited:
                    continue
                visited[neighbor] = (current, edge)
                queue.append(neighbor)
        if to_id not in visited:
            return JoinPath()
        steps: List[RelationshipEdge] = []
        cursor: Optional[str] = to_id
        while cursor is not None and visited.get(cursor) is not None:
            prev_id, edge = visited[cursor]  # type: ignore[misc]
            steps.append(edge)
            cursor = prev_id
        steps.reverse()
        return JoinPath(steps=steps)

    def describe_for_planner(self) -> Dict[str, Any]:
        """Return a compact summary of the graph for planner prompts."""
        return {
            "nodes": [
                {
                    "source_id": node.source_id,
                    "role": node.role,
                    "file_name": node.file_name,
                    "sheet_name": node.sheet_name,
                    "columns": list(node.columns),
                }
                for node in self._nodes.values()
            ],
            "edges": [
                {
                    "relationship_id": edge.relationship_id,
                    "from": edge.from_source_id,
                    "to": edge.to_source_id,
                    "kind": edge.relationship_kind,
                    "direction": edge.direction,
                    "join_keys": list(edge.join_keys),
                    "cardinality": edge.cardinality,
                    "confidence": edge.confidence,
                }
                for edge in self._edges
            ],
        }

    # ------------------------------------------------------------------
    # Collation
    # ------------------------------------------------------------------
    def collate(
        self,
        frames_by_source: Mapping[str, pd.DataFrame],
        *,
        on_missing_key: str = "warn",
    ) -> pd.DataFrame:
        """Collate ``frames_by_source`` using the approved edges.

        The algorithm is deterministic:
        1. Sources connected by ``union`` edges are stacked together.
        2. Each ``join``/``lookup`` edge is applied in order using the
           explicit ``join_keys`` from the edge record.
        3. Sources not referenced by any approved edge are left alone and
           concatenated at the end.

        ``on_missing_key`` controls behavior when an edge references a
        column that is not present in the provided frame:
        ``"warn"`` (default) silently skips that step, ``"raise"`` raises
        a :class:`ValueError` so problems surface loudly in tests.
        """
        if not frames_by_source:
            return pd.DataFrame()

        frames_by_source = dict(frames_by_source)
        non_output_sources = {
            sid
            for sid in frames_by_source
            if (self._nodes.get(sid) and str(self._nodes[sid].role).lower() in NON_OUTPUT_ROLES)
        }
        frames_by_source = {
            sid: frame for sid, frame in frames_by_source.items() if sid not in non_output_sources
        }
        consumed: set = set()

        # 1. Union groups - preserve the original insertion order of frames_by_source
        ordered_ids = list(frames_by_source.keys())
        union_groups = self._group_union_members(set(ordered_ids))
        union_frames: List[pd.DataFrame] = []
        for group in union_groups:
            ordered_group = [sid for sid in ordered_ids if sid in group]
            group_frames = [frames_by_source[sid] for sid in ordered_group if sid in frames_by_source]
            if group_frames:
                union_frames.append(_concat(group_frames))
                consumed.update(ordered_group)

        # 2. Join/lookup edges applied against available frames
        join_frames: List[pd.DataFrame] = []
        for edge in self._edges:
            if not edge.is_join_like:
                continue
            left_id = edge.from_source_id
            right_id = edge.to_source_id
            if left_id not in frames_by_source or right_id not in frames_by_source:
                continue
            left = frames_by_source[left_id]
            right = frames_by_source[right_id]
            merged = _merge_on_join_keys(left, right, edge.join_keys, on_missing_key=on_missing_key)
            if merged is not None:
                join_frames.append(merged)
                consumed.update({left_id, right_id})

        # 3. Independent sources
        independent = [
            frames_by_source[sid]
            for sid in frames_by_source
            if sid not in consumed
        ]

        pieces = union_frames + join_frames + independent
        if not pieces:
            return pd.DataFrame()
        if len(pieces) == 1:
            return pieces[0]
        return _concat(pieces)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _group_union_members(self, known_sources: set) -> List[List[str]]:
        parents: Dict[str, str] = {sid: sid for sid in known_sources}

        def find(x: str) -> str:
            while parents[x] != x:
                parents[x] = parents[parents[x]]
                x = parents[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parents[ra] = rb

        for edge in self._edges:
            if not edge.is_union:
                continue
            if edge.from_source_id in known_sources and edge.to_source_id in known_sources:
                union(edge.from_source_id, edge.to_source_id)

        groups_map: Dict[str, List[str]] = defaultdict(list)
        for sid in known_sources:
            groups_map[find(sid)].append(sid)
        return [group for group in groups_map.values() if len(group) > 1]


def build_source_graph(
    source_summaries: Sequence[Dict[str, Any]],
    relationship_records: Sequence[Any],
) -> SourceGraph:
    """Build a :class:`SourceGraph` from summaries and approved relationships.

    ``source_summaries`` may be either the canonical summaries produced by
    :func:`sia.agent.context_packet._build_source_summary` or raw rows from
    :attr:`JobManager.source_registry`. ``relationship_records`` may be a
    list of :class:`sia.storage.RelationshipRecord` dataclasses or the
    dict-shaped approved relationship records stored on the job.
    """
    nodes: List[SourceNode] = []
    for summary in source_summaries or []:
        if not isinstance(summary, dict):
            continue
        source_id = str(summary.get("source_id") or "")
        if not source_id:
            continue
        nodes.append(
            SourceNode(
                source_id=source_id,
                role=str(summary.get("role") or summary.get("source_type") or "unknown"),
                file_name=str(summary.get("file_name") or ""),
                sheet_name=summary.get("sheet_name"),
                columns=list(summary.get("columns") or summary.get("available_columns") or []),
                metadata={k: v for k, v in summary.items() if k not in {"columns", "available_columns"}},
            )
        )

    edges: List[RelationshipEdge] = []
    for record in relationship_records or []:
        edge = _record_to_edge(record)
        if edge is None:
            continue
        edges.append(edge)
    return SourceGraph(nodes=nodes, edges=edges)


def _record_to_edge(record: Any) -> Optional[RelationshipEdge]:
    """Normalize either a dict relationship record or a ``RelationshipRecord`` dataclass."""
    if record is None:
        return None
    if hasattr(record, "relationship_id") and hasattr(record, "from_source_id"):
        status = str(getattr(record, "status", "") or "").lower()
        if status and status not in APPROVED_STATUSES:
            return None
        return RelationshipEdge(
            relationship_id=str(getattr(record, "relationship_id", "")),
            from_source_id=str(getattr(record, "from_source_id", "")),
            to_source_id=str(getattr(record, "to_source_id", "")),
            relationship_kind=str(getattr(record, "relationship_kind", "")),
            direction=str(getattr(record, "direction", "bidirectional") or "bidirectional"),
            join_keys=[dict(entry) for entry in (getattr(record, "join_keys", []) or [])],
            cardinality=str(getattr(record, "cardinality", "unknown") or "unknown"),
            confidence=float(getattr(record, "confidence", 0.0) or 0.0),
        )
    if isinstance(record, dict):
        status = str(record.get("status", "") or "").lower()
        if status and status not in APPROVED_STATUSES:
            return None
        source_ids = record.get("source_ids") or []
        from_id = str(record.get("from_source_id") or (source_ids[0] if source_ids else ""))
        to_id = str(record.get("to_source_id") or (source_ids[1] if len(source_ids) > 1 else ""))
        if not from_id or not to_id:
            return None
        join_keys_raw = record.get("join_keys") or []
        return RelationshipEdge(
            relationship_id=str(record.get("relationship_id") or f"rel_{from_id}_{to_id}"),
            from_source_id=from_id,
            to_source_id=to_id,
            relationship_kind=str(record.get("relationship_kind") or ""),
            direction=str(record.get("direction") or "bidirectional"),
            join_keys=_normalize_join_keys(join_keys_raw, from_id, to_id),
            cardinality=str(record.get("cardinality") or "unknown"),
            confidence=float(record.get("confidence") or 0.0),
        )
    return None


def _normalize_join_keys(raw: Iterable[Any], from_id: str, to_id: str) -> List[Dict[str, str]]:
    """Accept join_keys as strings, tuples, or explicit mappings."""
    normalized: List[Dict[str, str]] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            from_col = entry.get("from") or entry.get("from_column") or entry.get("left")
            to_col = entry.get("to") or entry.get("to_column") or entry.get("right")
            if from_col and to_col:
                normalized.append({"from": str(from_col), "to": str(to_col)})
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            normalized.append({"from": str(entry[0]), "to": str(entry[1])})
        elif isinstance(entry, str):
            normalized.append({"from": entry, "to": entry})
    return normalized


def _concat(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame()
    return pd.concat(valid, ignore_index=True, sort=False)


def _merge_on_join_keys(
    left: pd.DataFrame,
    right: pd.DataFrame,
    join_keys: Sequence[Mapping[str, str]],
    *,
    on_missing_key: str = "warn",
) -> Optional[pd.DataFrame]:
    if not join_keys:
        return None
    left_on: List[str] = []
    right_on: List[str] = []
    for pair in join_keys:
        from_col = pair.get("from")
        to_col = pair.get("to")
        if not from_col or not to_col:
            continue
        if from_col not in left.columns or to_col not in right.columns:
            if on_missing_key == "raise":
                raise ValueError(
                    f"Join key missing: {from_col!r} in left or {to_col!r} in right"
                )
            continue
        left_on.append(from_col)
        right_on.append(to_col)
    if not left_on:
        return None
    return left.merge(right, how="left", left_on=left_on, right_on=right_on)
