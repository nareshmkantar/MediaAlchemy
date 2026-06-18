"""Tests for the executable SourceGraph."""

from __future__ import annotations

import pandas as pd

from sia.agent.source_graph import (
    RelationshipEdge,
    SourceGraph,
    SourceNode,
    build_source_graph,
)


def _summary(source_id: str, columns, role="fact", file_name="f.xlsx", sheet="S"):
    return {
        "source_id": source_id,
        "role": role,
        "file_name": file_name,
        "sheet_name": sheet,
        "columns": list(columns),
    }


def test_build_source_graph_filters_unapproved_dict_relationships():
    summaries = [_summary("a", ["date", "spend"]), _summary("b", ["date", "impressions"])]
    rels = [
        {
            "relationship_id": "r1",
            "relationship_kind": "join",
            "source_ids": ["a", "b"],
            "join_keys": [{"from": "date", "to": "date"}],
            "status": "approved",
        },
        {
            "relationship_id": "r2",
            "relationship_kind": "join",
            "source_ids": ["a", "b"],
            "join_keys": [{"from": "campaign", "to": "campaign"}],
            "status": "proposed",
        },
    ]
    graph = build_source_graph(summaries, rels)
    assert len(graph.edges) == 1
    assert graph.edges[0].relationship_id == "r1"


def test_source_graph_join_path_and_available_fields():
    graph = SourceGraph(
        nodes=[
            SourceNode(source_id="a", columns=["date", "spend"]),
            SourceNode(source_id="b", columns=["date", "impressions"]),
            SourceNode(source_id="c", columns=["date", "clicks"]),
        ],
        edges=[
            RelationshipEdge(
                relationship_id="ab",
                from_source_id="a",
                to_source_id="b",
                relationship_kind="join",
                join_keys=[{"from": "date", "to": "date"}],
            ),
            RelationshipEdge(
                relationship_id="bc",
                from_source_id="b",
                to_source_id="c",
                relationship_kind="join",
                join_keys=[{"from": "date", "to": "date"}],
            ),
        ],
    )
    assert graph.available_fields("a") == ["date", "spend"]
    assert graph.reachable_sources("a") == ["b", "c"]
    path = graph.join_path("a", "c")
    assert not path.is_empty
    assert [edge.relationship_id for edge in path.steps] == ["ab", "bc"]

    disconnected = graph.join_path("a", "missing")
    assert disconnected.is_empty


def test_source_graph_collate_uses_approved_join_keys():
    left = pd.DataFrame({"date": ["2025-01-01", "2025-01-02"], "spend": [100.0, 150.0]})
    right = pd.DataFrame({"date": ["2025-01-01", "2025-01-02"], "impressions": [1000, 2000]})
    graph = SourceGraph(
        nodes=[
            SourceNode(source_id="a", columns=["date", "spend"]),
            SourceNode(source_id="b", columns=["date", "impressions"]),
        ],
        edges=[
            RelationshipEdge(
                relationship_id="ab",
                from_source_id="a",
                to_source_id="b",
                relationship_kind="join",
                join_keys=[{"from": "date", "to": "date"}],
            )
        ],
    )
    merged = graph.collate({"a": left, "b": right})
    assert set(merged.columns) >= {"date", "spend", "impressions"}
    assert len(merged) == 2
    assert merged.loc[merged["date"] == "2025-01-01", "impressions"].iloc[0] == 1000


def test_source_graph_collate_unions_parallel_sources():
    meta = pd.DataFrame({"date": ["2025-01-01"], "spend": [100.0]})
    youtube = pd.DataFrame({"date": ["2025-01-02"], "spend": [200.0]})
    graph = SourceGraph(
        nodes=[
            SourceNode(source_id="meta", columns=["date", "spend"]),
            SourceNode(source_id="yt", columns=["date", "spend"]),
        ],
        edges=[
            RelationshipEdge(
                relationship_id="u",
                from_source_id="meta",
                to_source_id="yt",
                relationship_kind="union",
            )
        ],
    )
    result = graph.collate({"meta": meta, "yt": youtube})
    assert list(result["date"]) == ["2025-01-01", "2025-01-02"]
    assert list(result["spend"]) == [100.0, 200.0]


def test_source_graph_collate_missing_join_key_warns_not_raises():
    left = pd.DataFrame({"date": ["2025-01-01"], "spend": [100]})
    right = pd.DataFrame({"date_other": ["2025-01-01"], "impressions": [1000]})
    graph = SourceGraph(
        nodes=[
            SourceNode(source_id="a", columns=["date", "spend"]),
            SourceNode(source_id="b", columns=["date_other", "impressions"]),
        ],
        edges=[
            RelationshipEdge(
                relationship_id="ab",
                from_source_id="a",
                to_source_id="b",
                relationship_kind="join",
                join_keys=[{"from": "date", "to": "date"}],
            )
        ],
    )
    merged = graph.collate({"a": left, "b": right})
    assert not merged.empty
    assert "spend" in merged.columns


def test_describe_for_planner_exposes_columns_and_join_keys():
    graph = SourceGraph(
        nodes=[
            SourceNode(source_id="a", role="fact", columns=["date", "spend"], file_name="a.xlsx"),
            SourceNode(source_id="b", role="lookup", columns=["campaign_id", "group"], file_name="b.xlsx"),
        ],
        edges=[
            RelationshipEdge(
                relationship_id="ab",
                from_source_id="a",
                to_source_id="b",
                relationship_kind="lookup",
                join_keys=[{"from": "campaign_id", "to": "campaign_id"}],
            )
        ],
    )
    desc = graph.describe_for_planner()
    assert len(desc["nodes"]) == 2
    assert desc["edges"][0]["join_keys"] == [{"from": "campaign_id", "to": "campaign_id"}]
