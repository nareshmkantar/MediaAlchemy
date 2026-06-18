"""Tests for Console macro-phase banner heuristics (UI only)."""

from sia.agent.transformation_phase_banner import (
    build_transformation_phase_banner,
    compact_structure_hints,
    needs_structure_normalization_phase,
)


def test_compact_structure_hints_merged_and_noise():
    hints = compact_structure_hints(
        {
            "merged_cells": [{"range": "A1:B5", "top_left_value": "x", "rows_spanned": 5, "cols_spanned": 2}],
            "noise_score": 0.6,
            "header_candidates": [1, 2],
            "sheet_name": "Data",
        },
        {"overall_structure": "hierarchical", "visual_patterns": {"merged_cells": 2}},
    )
    assert hints["merged_cell_signals"] == 2
    assert hints["merged_regions_total"] == 1
    assert len(hints["merged_regions_preview"]) == 1
    assert hints["merged_regions_preview"][0].get("range") == "A1:B5"
    assert hints["noise_above_hitl_threshold"] is True
    assert hints["ambiguous_headers"] is True


def test_hide_structure_phase_when_clean_and_flat_plan():
    hints = compact_structure_hints(
        {"merged_cells": [], "noise_score": 0.1, "header_candidates": [1]},
        {"overall_structure": "flat"},
    )
    assert needs_structure_normalization_phase(hints, ["transform.rename", "verify.schema"]) is False


def test_show_structure_phase_when_layout_tools():
    hints = compact_structure_hints({}, {})
    assert needs_structure_normalization_phase(hints, ["layout.extract", "transform.rename"]) is True


def test_banner_completed_marks_all_done():
    banner = build_transformation_phase_banner(
        hints=compact_structure_hints({}, {}),
        plan_tool_names=["transform.rename"],
        tool_executions=[],
        job_status="completed",
    )
    assert banner["show_structure_phase"] is False
    assert len(banner["phases"]) == 2
    assert all(p["status"] == "done" for p in banner["phases"])
