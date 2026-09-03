"""Tests for planner prompt briefing payload."""

from __future__ import annotations

from sia.context.planner_prompt import build_planner_prompt_result
from sia.context.prompt_briefing import BUCKET_ORDER, BUCKET_LABELS


def _minimal_structure():
    return {
        "tables": [{"name": "Sheet1", "rows": 10, "cols": 5}],
        "column_analysis": [{"name": "Spend", "col": 0}],
        "overall_structure": "flat",
    }


def _minimal_context_packet():
    return {
        "planning_summary": {
            "mapping_summary": {
                "mapped_columns": ["Spend → spends"],
                "excluded_columns": [],
            },
            "source_summary": {
                "file_name": "test.xlsx",
                "sheet_name": "Data",
                "date_granularity": "weekly",
            },
            "layout_summary": {"scope_type": "full_sheet", "main_blocks_count": 1},
            "rules_summary": ["Default channel to Direct"],
            "user_notes": ["Use Q1 only"],
        },
        "context_block_snippets": [{"label": "Market", "text": "Germany"}],
        "interpreted_context": {
            "scoped_fields": {"market": {"value": "Germany", "scope": "workbook"}},
            "evidence": [{"field": "market", "source": "snippet"}],
        },
        "file_relationships": [],
    }


def test_briefing_has_bucket_structure():
    result = build_planner_prompt_result(
        _minimal_structure(),
        _minimal_context_packet(),
        target_template={"properties": {"spends": {"type": "number"}}},
    )
    briefing = result["briefing"]
    assert result["user_prompt"]
    assert briefing["schema_version"] == 1
    assert briefing["curated_user_prompt_chars"] == len(result["user_prompt"])
    buckets = briefing["buckets"]
    for key in ("prompt", "docs", "memory", "tools"):
        assert key in buckets, f"missing bucket {key}"
    assert buckets["prompt"]["label"] == BUCKET_LABELS["prompt"]
    assert buckets["tools"]["sections"][0]["id"] == "pipeline_catalog"
    assert buckets["tools"]["sections"][0]["delivery"] == "system_prompt"


def test_briefing_marks_included_sections():
    result = build_planner_prompt_result(_minimal_structure(), _minimal_context_packet())
    briefing = result["briefing"]
    included = set(briefing["included_section_ids"])
    assert "task" in included
    for row in briefing["sections"]:
        if row["id"] == "pipeline_catalog":
            assert row["in_curated_prompt"] is True
            assert row["delivery"] == "system_prompt"
            continue
        if row["id"] in included:
            assert row["in_curated_prompt"] is True
        else:
            assert row["in_curated_prompt"] is False


def test_archive_differs_from_curated():
    cp = _minimal_context_packet()
    result = build_planner_prompt_result(_minimal_structure(), cp)
    archive = result["briefing"]["archive"]
    assert archive["buckets"]
    assert "context_block_snippets_full" in {i["id"] for i in archive["buckets"]["docs"]["items"]}
    assert "Germany" in result["user_prompt"] or "market" in result["user_prompt"].lower()


def test_budget_drops_low_priority_sections():
    full = build_planner_prompt_result(
        _minimal_structure(),
        _minimal_context_packet(),
        max_chars=48_000,
    )
    cut = build_planner_prompt_result(
        _minimal_structure(),
        _minimal_context_packet(),
        max_chars=1200,
    )
    briefing = cut["briefing"]
    assert briefing["dropped_section_ids"]
    assert len(cut["user_prompt"]) < len(full["user_prompt"])


def test_bucket_order_stable():
    assert "prompt" in BUCKET_ORDER
    assert BUCKET_ORDER.index("tools") < BUCKET_ORDER.index("memory")
