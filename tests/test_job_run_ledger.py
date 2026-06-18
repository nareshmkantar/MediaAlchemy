"""Tests for job_run_ledger helpers (router, bootstrap, ledger)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sia.agent.job_run_ledger import (
    bootstrap_job_sources,
    build_job_run_ledger_summary,
    clear_multi_source_ledger,
    record_source_run_ledger,
    route_processing_mode,
)


def _minimal_job(**kwargs):
    base = {
        "id": "job-test",
        "source_registry": [],
        "approved_file_relationships": [],
        "relationship_proposals": [],
        "data_files": [],
        "source_scope_registry": {},
    }
    base.update(kwargs)
    return base


def test_route_single_main():
    job = _minimal_job(
        source_registry=[
            {"source_id": "a:1:Sheet1", "contains_main_data": True, "contains_reference_data": False},
        ]
    )
    assert route_processing_mode(job) == "single_main"


def test_route_needs_hitl_two_mains():
    job = _minimal_job(
        source_registry=[
            {"source_id": "a", "contains_main_data": True, "contains_reference_data": False},
            {"source_id": "b", "contains_main_data": True, "contains_reference_data": False},
        ],
        approved_file_relationships=[],
    )
    assert route_processing_mode(job) == "needs_relationship_hitl"


def test_route_multi_union():
    job = _minimal_job(
        source_registry=[
            {"source_id": "a", "contains_main_data": True, "contains_reference_data": False},
            {"source_id": "b", "contains_main_data": True, "contains_reference_data": False},
        ],
        approved_file_relationships=[
            {"relationship_kind": "union", "source_ids": ["a", "b"]},
        ],
    )
    assert route_processing_mode(job) == "multi_union"


def test_route_multi_join():
    job = _minimal_job(
        source_registry=[
            {"source_id": "a", "contains_main_data": True, "contains_reference_data": False},
            {"source_id": "b", "contains_main_data": True, "contains_reference_data": False},
        ],
        approved_file_relationships=[
            {"kind": "lookup", "source_ids": ["a", "b"]},
        ],
    )
    assert route_processing_mode(job) == "multi_join"


def test_clear_and_record_ledger():
    job = _minimal_job(
        source_execution_registry=[{"source_id": "old"}],
        _deferred_post_collate_by_source={"x": [{"tool": "t"}]},
    )
    clear_multi_source_ledger(job)
    assert job["source_execution_registry"] == []
    assert job["_deferred_post_collate_by_source"] == {}

    record_source_run_ledger(
        job,
        "src1",
        {"sheet_name": "S1", "processing_sheet": "S1x"},
        [{"tool": "transform.union_resolve"}],
    )
    assert len(job["source_execution_registry"]) == 1
    assert job["source_execution_registry"][0]["deferred_post_collate_tools_count"] == 1
    assert "src1" in (job.get("_deferred_post_collate_by_source") or {})

    summary = build_job_run_ledger_summary(job)
    assert summary["registry_row_count"] == 1
    assert summary["total_deferred_post_collate_tool_calls"] == 1
    assert "src1" in summary["sources_with_deferred_tools"]


def test_bootstrap_job_sources_mocked():
    fake_path = r"C:\fake\workbook.xlsx"
    job = _minimal_job(
        source_registry=[
            {
                "source_id": "job:file_001:Sheet1",
                "file_path": fake_path,
                "sheet_name": "Sheet1",
                "contains_main_data": True,
            },
        ],
    )

    inv = MagicMock()
    inv.success = True
    inv.data = {
        "sheets": [{"name": "Sheet1", "rows": 5, "cols": 3, "state": "visible", "has_data": True}],
        "named_ranges": [],
        "total_sheets": 1,
        "visible_sheets": 1,
        "hidden_sheets": 0,
        "file_metadata": {"file_name": "workbook.xlsx", "file_size_kb": 1.0},
    }
    insp = MagicMock()
    insp.success = True
    insp.data = {
        "sheet_name": "Sheet1",
        "dimensions": {"rows": 5, "cols": 3},
        "data_region": {"rows": 3, "cols": 2},
        "header_candidates": [{"row": 0}],
        "noise_score": 0.1,
        "density": 0.5,
        "summary": {},
    }

    with patch(
        "sia.agent.job_run_ledger.TransformationTools.get_file_inventory",
        return_value=inv,
    ), patch(
        "sia.agent.job_run_ledger.TransformationTools.inspect_sheet_structure",
        return_value=insp,
    ):
        out = bootstrap_job_sources(job, job_id="job-test", source_ids=["job:file_001:Sheet1"])

    assert job.get("source_bootstrap_complete") is True
    assert "by_source_id" in out
    row = out["by_source_id"]["job:file_001:Sheet1"]
    assert row["inspect"].get("has_data_region") is True
    assert row["main_block_hint"]["estimated_main_blocks"] == 1
