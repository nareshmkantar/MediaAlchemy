"""Snapshot panel structure: source block, ordered sections, list fields."""

import pandas as pd

from sia.debug.state_snapshot import (
    build_node_output_drilldown,
    build_state_snapshot,
    compact_memory,
)


def test_source_block_is_canonical_and_ordered():
    snapshot = build_state_snapshot(
        label="state.generate_plan",
        phase="node_after",
        state={
            "source_id": "job:file_001:Data",
            "sheet_name": "Data",
            "scoped_source": {"sheet_name": "Data"},
            "context_packet": {
                "source_metadata": {
                    "source_id": "job:file_001:Data",
                    "sheet_name": "Data",
                    "date_granularity": "daily",
                    "uid": ["Ref", "Name"],
                },
                "defer_weekly_rollups_to_post_union": True,
                "planning_summary": {
                    "source_summary": {"date_granularity": "daily"},
                    "template_summary": {"target_date_granularity": "weekly"},
                    "layout_summary": {"scope_type": "main_data", "main_blocks_count": 1},
                },
            },
            "suggested_tools": [{"tool": "transform.rename"}, {"tool": "verify.schema"}],
        },
        decision={"result_keys": ["suggested_tools"]},
    )

    keys = list(snapshot.keys())
    assert keys.index("source") < keys.index("agent_state")
    assert keys.index("agent_state") < keys.index("context")
    assert keys.index("context") < keys.index("decision")

    assert snapshot["source"]["source_id"] == "job:file_001:Data"
    assert snapshot["source"]["uid_columns"] == ["Ref", "Name"]
    assert "source_id" not in snapshot["agent_state"]
    assert snapshot["agent_state"]["plan"]["suggested_tools"] == [
        "transform.rename",
        "verify.schema",
    ]
    assert snapshot["context"]["deferrals"]["weekly_rollups_to_post_union"] is True
    assert snapshot["decision"]["node"]["result_keys"] == ["suggested_tools"]


def test_memory_empty_for_single_source():
    mem = compact_memory(
        {"processing_route": "single", "source_registry": [{"source_id": "a"}]},
        {"multi_source_active_batch": False},
    )
    assert mem == {}


def test_load_file_scope_in_agent_state():
    snapshot = build_state_snapshot(
        label="state.load_file",
        phase="node_after",
        state={
            "source_id": "job:file:Data",
            "sheet_name": "Data",
            "file_path": "/uploads/book.xlsx",
            "scoped_source": {
                "scope_type": "main_data",
                "requires_extraction": True,
                "main_blocks": [
                    {
                        "id": "block_1",
                        "category": "Main Data",
                        "coordinates": {
                            "start_row": 5,
                            "end_row": 85,
                            "start_col": 0,
                            "end_col": 4,
                            "header_row": 4,
                        },
                    }
                ],
            },
            "source_frame": {"rows": 81, "cols": 5},
            "file_inventory": {"total_sheets": 2, "visible_sheets": 2, "sheet_names": ["Data", "Extra"]},
        },
    )
    assert snapshot["agent_state"]["frames"]["source"] == {"rows": 81, "cols": 5}
    assert snapshot["agent_state"]["scope"]["main_blocks_count"] == 1
    assert snapshot["agent_state"]["scope"]["main_blocks"][0]["end_row"] == 85
    assert snapshot["agent_state"]["workbook"]["sheet_names"] == ["Data", "Extra"]


def test_decision_tool_step_lists_params_keys():
    snapshot = build_state_snapshot(
        label="state.execute_tools.tool",
        phase="tool_before",
        state={"source_id": "a"},
        decision={
            "tool": "transform.filter_empty",
            "params_keys": ["check_columns", "require_metrics"],
            "rows_before": 79,
        },
    )
    assert snapshot["decision"]["tool_step"]["tool"] == "transform.filter_empty"
    assert snapshot["decision"]["tool_step"]["params_keys"] == [
        "check_columns",
        "require_metrics",
    ]


def test_node_output_drilldown_context_packet_and_mapping():
    state = {
        "context_packet": {
            "approved_mappings": [
                {
                    "source_column": "Spend",
                    "target_column": "spends",
                    "decision": "keep",
                    "target_match_confidence": 0.95,
                }
            ],
            "mapping_supplement": {
                "merged_metric_ranges": [
                    {
                        "column_name": "Spend",
                        "start_row": 0,
                        "end_row": 15,
                        "top_left_value": 10614.78,
                    }
                ],
            },
            "planning_summary": {"source_summary": {"date_granularity": "daily"}},
            "unresolved_items": [],
        },
        "mapping_summary": {
            "average_confidence": 0.95,
            "mapped_count": 1,
            "unresolved_items": [],
        },
    }
    outputs = build_node_output_drilldown(
        state,
        ["context_packet", "mapping_summary", "approved_mappings"],
        node_name="resolve_mapping",
    )
    assert "context_packet" in outputs
    assert "merged_metric" in outputs["context_packet"]["summary"] or "1 merged" in outputs[
        "context_packet"
    ]["summary"]
    detail = outputs["context_packet"]["detail"]
    assert "planning_summary" in detail
    assert detail["mapping_supplement"]["merged_metric_ranges"][0]["top_left_value"] == 10614.78
    assert "mapping_summary" in outputs
    assert outputs["approved_mappings"]["detail"][0]["target_column"] == "spends"


def test_build_state_snapshot_preserves_output_drilldown():
    snapshot = build_state_snapshot(
        label="state.resolve_mapping",
        phase="node_after",
        state={"context_packet": {"approved_mappings": []}},
        decision={
            "result_keys": ["context_packet"],
            "outputs": {
                "context_packet": {"summary": "test", "detail": {"approved_mappings": []}},
            },
        },
    )
    assert snapshot["decision"]["outputs"]["context_packet"]["summary"] == "test"
