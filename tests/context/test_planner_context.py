"""Phase 3.3: planner context sanitization."""

from __future__ import annotations

from sia.context.planner_context import (
    filter_interpreted_context_for_planner,
    prepare_planner_context_sections,
    sanitize_peer_source_summary,
    sanitize_source_graph_view,
)


def test_filter_strips_foreign_scoped_fields():
    ic = {
        "scoped_fields": {
            "market": {
                "name": "market",
                "value": "DE",
                "scope": "block",
                "source_id": "radio-de",
            },
            "channel": {
                "name": "channel",
                "value": "digital",
                "scope": "block",
                "source_id": "digital-uk",
            },
        },
        "fields": {"market": "DE", "channel": "digital"},
        "evidence": [
            {"field": "market", "source_id": "radio-de"},
            {"field": "channel", "source_id": "digital-uk"},
        ],
    }
    filtered = filter_interpreted_context_for_planner(ic, "radio-de")
    assert filtered["fields"] == {"market": "DE"}
    assert "channel" not in filtered["scoped_fields"]
    assert all(e.get("source_id") == "radio-de" for e in filtered["evidence"])


def test_peer_summary_strips_dimension_literals():
    peer = {
        "source_id": "digital-uk",
        "sheet_name": "Digital_UK",
        "market": "UK",
        "channel": "digital",
        "mapped_targets": ["spends"],
    }
    stripped = sanitize_peer_source_summary(peer, is_active=False, allows_enrichment=False)
    assert "market" not in stripped
    assert "channel" not in stripped
    assert stripped["sheet_name"] == "Digital_UK"
    assert stripped["mapped_targets"] == ["spends"]


def test_source_graph_strips_peer_dimensions():
    graph = {
        "nodes": [
            {"source_id": "radio-de", "market": "DE", "sheet_name": "Radio_DE"},
            {"source_id": "digital-uk", "market": "UK", "channel": "digital"},
        ],
        "edges": [],
    }
    out = sanitize_source_graph_view(graph, "radio-de", allows_enrichment=False)
    peers = [n for n in out["nodes"] if n.get("source_id") == "digital-uk"]
    assert peers[0].get("market") is None or "market" not in peers[0]


def test_prepare_planner_context_sections_for_radio_de():
    packet = {
        "lineage": {"source_id": "radio-de", "sheet_name": "Radio_DE"},
        "interpreted_context": {
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "DE",
                    "scope": "block",
                    "source_id": "radio-de",
                },
            },
            "fields": {"market": "DE"},
        },
        "available_source_summaries": [
            {"source_id": "radio-de", "sheet_name": "Radio_DE", "market": "DE"},
            {"source_id": "digital-uk", "sheet_name": "Digital_UK", "market": "UK"},
        ],
        "file_relationships": [],
        "source_graph_view": {
            "nodes": [
                {"source_id": "radio-de", "market": "DE"},
                {"source_id": "digital-uk", "market": "UK"},
            ],
        },
    }
    sections = prepare_planner_context_sections(packet)
    assert sections["interpreted_context"]["fields"]["market"] == "DE"
    peer = next(
        s for s in sections["available_source_summaries"] if s.get("source_id") == "digital-uk"
    )
    assert "market" not in peer
