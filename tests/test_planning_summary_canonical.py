"""Phase 1.1: canonical planning_summary must retain interpreted context."""

from __future__ import annotations

from sia.agent.context_packet import build_canonical_planning_view
from sia.agent.nodes import _ensure_canonical_planning_summary


def test_canonical_view_includes_interpreted_context():
    packet = {
        "source_metadata": {"sheet_name": "Radio_DE", "source_id": "job:file:Radio_DE"},
        "interpreted_context": {
            "fields": {"market": "Germany", "channel": "radio"},
            "assumptions": ["Use booking date"],
            "evidence": [{"field": "market", "value": "Germany", "block_label": "Top meta"}],
        },
        "context_block_snippets": [
            {"block_label": "Top meta", "text_preview": ["Market: Germany", "Channel: radio"]},
        ],
        "approved_layout": {"main_blocks": [], "context_blocks": [{"block_id": "meta"}]},
        "approved_mappings": [],
        "business_rules": [],
        "target_template": {},
    }
    view = build_canonical_planning_view(packet)
    assert view["interpreted_context"]["fields"]["market"] == "Germany"
    assert view["layout_summary"]["context_blocks_count"] == 1


def test_ensure_canonical_planning_summary_preserves_interpreted_context():
    packet = {
        "source_metadata": {"sheet_name": "Digital_UK"},
        "interpreted_context": {"fields": {"market": "UK", "channel": "digital"}},
        "approved_mappings": [{"source_column": "Spend", "target_column": "spends", "decision": "Keep"}],
        "business_rules": [],
        "target_template": {},
        "approved_layout": {},
    }
    summary = _ensure_canonical_planning_summary(packet)
    assert summary["interpreted_context"]["fields"]["market"] == "UK"
    assert summary["mapping_summary"]["mapped_count"] >= 1
