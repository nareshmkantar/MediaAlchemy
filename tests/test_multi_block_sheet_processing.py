"""Multi-block sheet: per-block pipeline + union collation (like multi-source)."""

from __future__ import annotations

from sia.agent.base import ExtractionPlan
from sia.agent.multi_block_sheet import (
    block_virtual_source_id,
    blocks_are_column_disjoint,
    expand_multi_block_source_ids,
    ensure_block_union_relationship,
    group_mapping_rows_by_block,
    parse_block_virtual_source_id,
    should_process_main_blocks_separately,
)
from sia.agent.planner import PlanGenerator


def _two_horizontal_blocks():
    return [
        {
            "id": "b1",
            "category": "Main Data",
            "decision": "Keep",
            "coordinates": {
                "start_row": 0,
                "end_row": 10,
                "start_col": 0,
                "end_col": 5,
                "header_row": 0,
            },
        },
        {
            "id": "b2",
            "category": "Main Data",
            "decision": "Keep",
            "coordinates": {
                "start_row": 0,
                "end_row": 10,
                "start_col": 7,
                "end_col": 12,
                "header_row": 0,
            },
        },
    ]


def test_expand_multi_block_source_ids():
    job = {
        "id": "job1",
        "source_scope_registry": {
            "src_a": {
                "main_blocks": _two_horizontal_blocks(),
                "sheet_frame": {"rows": 20, "cols": 15},
            }
        },
    }
    assert should_process_main_blocks_separately(job, "src_a")
    expanded, flag = expand_multi_block_source_ids(job, ["src_a"])
    assert flag is True
    assert len(expanded) == 2
    assert all("::block::" in sid for sid in expanded)
    ensure_block_union_relationship(job, expanded)
    rels = job.get("approved_file_relationships") or []
    assert any(r.get("relationship_kind") == "union" for r in rels)


def test_planner_strips_stack_for_per_block_run():
    gen = PlanGenerator(llm_client=None)
    block = _two_horizontal_blocks()[0]
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.stack", "params": {"header_row": 0, "blocks": []}},
            {"tool": "transform.filter_summaries", "params": {}},
        ],
        reasoning="",
    )
    cp = {
        "process_blocks_separately": True,
        "scoped_source": {"main_blocks": [block], "header_row": 0},
        "approved_layout": {"main_blocks": [block]},
    }
    out = gen._ensure_layout_stack_in_plan(plan, cp, {})
    names = [t.get("tool") for t in (out.tool_calls or [])]
    assert "layout.stack" not in names
    assert names[0] == "layout.extract"
    assert out.tool_calls[0]["params"]["start_col"] == 0
    assert out.tool_calls[0]["params"]["end_col"] == 5


def test_block_virtual_id_roundtrip():
    bid = block_virtual_source_id("parent1", {"id": "blk_x"})
    assert parse_block_virtual_source_id(bid) == ("parent1", "blk_x")


def test_disjoint_blocks_detection():
    assert blocks_are_column_disjoint(_two_horizontal_blocks()) is True
    overlap = [
        _two_horizontal_blocks()[0],
        {
            "id": "b2",
            "coordinates": {
                "start_row": 0,
                "end_row": 5,
                "start_col": 3,
                "end_col": 8,
                "header_row": 0,
            },
        },
    ]
    assert blocks_are_column_disjoint(overlap) is False


def test_group_mapping_rows_sets_column_name_from_source_column():
    blocks = _two_horizontal_blocks()
    registry = [
        {
            "block_id": "b1",
            "source_column": "Date",
            "target_column": "date",
        },
        {
            "block_id": "b2",
            "source_column": "Spend",
            "target_column": "spends",
        },
    ]
    sections = group_mapping_rows_by_block(registry, blocks)
    assert sections[0]["mapping"][0]["column_name"] == "Date"
    assert sections[1]["mapping"][0]["column_name"] == "Spend"
