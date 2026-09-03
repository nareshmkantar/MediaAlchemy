"""Curated plan_generator user prompt (build_planner_prompt_context)."""

from __future__ import annotations

from sia.context.planner_prompt import build_planner_prompt_context


def _radio_de_packet() -> dict:
    return {
        "lineage": {"source_id": "job:f:Radio_DE", "sheet_name": "Radio_DE"},
        "planning_summary": {
            "source_summary": {
                "file_name": "media.xlsx",
                "sheet_name": "Radio_DE",
                "market": "DE",
                "channel": "radio",
                "date_granularity": "monthly",
            },
            "mapping_summary": {
                "mapped_columns": ["date -> date", "spends -> spends"],
                "excluded_columns": [],
            },
            "layout_summary": {
                "scope_type": "sheet",
                "header_row": 2,
                "main_blocks_count": 1,
                "context_blocks_count": 1,
                "context_block_labels": ["Header"],
            },
            "rules_summary": [],
            "user_notes": [],
        },
        "context_block_snippets": [
            {
                "block_label": "Header",
                "summary": "Market: Germany",
                "text_preview": ["Market: Germany | Channel: Radio"],
                "non_empty_cells": [{"row": 0, "col": 0, "value": "Market: Germany"}],
            }
        ],
        "interpreted_context": {
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "Germany",
                    "scope": "block",
                    "source_id": "job:f:Radio_DE",
                    "block_label": "Header",
                    "confidence": 1.0,
                },
                "channel": {
                    "name": "channel",
                    "value": "radio",
                    "scope": "sheet",
                    "source_id": "job:f:Radio_DE",
                    "confidence": 0.7,
                },
            },
            "fields": {"market": "Germany", "channel": "radio"},
            "evidence": [{"field": "market", "source_id": "job:f:Radio_DE", "line": "Market: Germany"}],
        },
        "file_relationships": [],
        "available_source_summaries": [
            {"source_id": "job:f:Radio_DE", "sheet_name": "Radio_DE"},
            {"source_id": "job:f:Digital_UK", "sheet_name": "Digital_UK", "market": "UK"},
        ],
    }


def test_local_context_appears_before_structure():
    prompt = build_planner_prompt_context(
        {"tables": [], "column_analysis": [], "overall_structure": "flat"},
        _radio_de_packet(),
        None,
    )
    local_idx = prompt.index("## Local context (authoritative for this sheet)")
    structure_idx = prompt.index("## Structure Analysis (Summary)")
    assert local_idx < structure_idx


def test_no_duplicate_inferred_fields_json():
    prompt = build_planner_prompt_context(
        {"tables": [], "column_analysis": [], "overall_structure": "flat"},
        _radio_de_packet(),
        None,
    )
    assert "Inferred fields:" not in prompt
    assert "market='Germany'" in prompt or "market=" in prompt


def test_scoped_dimensions_omitted_from_source_identity():
    prompt = build_planner_prompt_context(
        {"tables": [], "column_analysis": [], "overall_structure": "flat"},
        _radio_de_packet(),
        None,
    )
    identity_start = prompt.index("## Source identity")
    identity_end = prompt.index("## Structure Analysis", identity_start)
    identity_block = prompt[identity_start:identity_end]
    assert "market: DE" not in identity_block
    assert "channel: radio" not in identity_block.lower() or "channel" not in identity_block


def test_mappings_before_structure():
    prompt = build_planner_prompt_context(
        {"tables": [], "column_analysis": [], "overall_structure": "flat"},
        _radio_de_packet(),
        None,
    )
    assert prompt.index("## Approved Column Mappings") < prompt.index("## Structure Analysis")


def test_peer_market_not_in_prompt_body():
    prompt = build_planner_prompt_context(
        {"tables": [], "column_analysis": [], "overall_structure": "flat"},
        _radio_de_packet(),
        None,
    )
    assert "Digital_UK" not in prompt or "UK" not in prompt.split("Digital_UK")[1][:80]


def test_budget_trims_low_priority_sections():
    huge_notes = ["note " * 200] * 50
    packet = _radio_de_packet()
    packet["planning_summary"]["user_notes"] = huge_notes
    prompt = build_planner_prompt_context(
        {"tables": [], "column_analysis": [], "overall_structure": "flat"},
        packet,
        None,
        max_chars=4000,
    )
    assert "## Local context" in prompt
    assert "## Approved Column Mappings" in prompt
    assert len(prompt) <= 4500
