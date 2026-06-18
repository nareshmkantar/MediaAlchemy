"""Tests for job-level orchestration debug timeline helpers."""

from sia.debug.job_debug_events import (
    append_job_debug_event,
    data_files_summary,
    get_job_debug_timeline,
    job_setup_summary,
    make_job_debug_event,
    rebuild_job_debug_timeline,
)


def test_upload_inventory_event_from_data_files():
    job = {
        "data_files": [
            {"file_id": "f1", "file_name": "a.xlsx", "sheets": ["S1", "S2"]},
            {"file_id": "f2", "file_name": "b.xlsx", "sheets": ["T1"]},
        ],
        "job_debug_events": [],
    }
    timeline = rebuild_job_debug_timeline(job)
    upload = next(e for e in timeline if e.get("phase") == "upload")
    assert upload["metadata"]["file_count"] == 2
    assert upload["metadata"]["total_sheets"] == 3
    assert "2 workbook" in (upload.get("input_summary") or "")


def test_demarcation_and_mapping_seed_from_registries():
    job = {
        "source_registry": [
            {"source_id": "src-a", "sheet_name": "SheetA", "file_id": "f1"},
            {"source_id": "src-b", "sheet_name": "SheetB", "file_id": "f1"},
        ],
        "layout_registry": [
            {"source_id": "src-a", "layout_id": "l1"},
            {"source_id": "src-a", "layout_id": "l2"},
            {"source_id": "src-b", "layout_id": "l3"},
        ],
        "mapping_registry": [
            {"source_id": "src-a", "target_column": "date", "column_name": "Date"},
            {"source_id": "src-b", "target_column": "No match", "column_name": "X"},
        ],
        "job_debug_events": [],
    }
    timeline = rebuild_job_debug_timeline(job)
    phases = [e.get("phase") for e in timeline]
    assert "inventory" in phases
    assert "demarcation" in phases
    assert "mapping" in phases
    mapping = next(e for e in timeline if e.get("phase") == "mapping")
    assert mapping["metadata"]["unresolved_by_source"].get("src-b") == 1


def test_live_events_merge_and_parent_child_order():
    job = {"data_files": [], "job_debug_events": []}
    root = make_job_debug_event(
        label="Processing started",
        phase="process",
    )
    append_job_debug_event(job, root)
    child = make_job_debug_event(
        label="Agent run: src-1",
        phase="agent_run",
        parent_event_id=root["event_id"],
        source_id="src-1",
        sheet_name="Data",
    )
    append_job_debug_event(job, child)

    timeline = get_job_debug_timeline(job)
    assert len(timeline) >= 2
    agent = next(e for e in timeline if e.get("phase") == "agent_run")
    assert agent["parent_event_id"] == root["event_id"]
    assert agent["source_id"] == "src-1"


def test_source_registry_sorts_before_agent_runs_with_late_live_timestamps():
    """Source registry is synthetic at job created_at; agent runs use wall clock — must still sort early."""
    job = {
        "created_at": "2026-05-22T07:30:00",
        "data_files": [{"file_id": "f1", "file_name": "a.xlsx", "sheets": ["S1", "S2"]}],
        "source_registry": [
            {"source_id": "src-a", "sheet_name": "Data", "file_id": "f1"},
            {"source_id": "src-b", "sheet_name": "Extra", "file_id": "f1"},
        ],
        "job_debug_events": [
            make_job_debug_event(
                label="Data file uploaded",
                phase="upload",
                timestamp="2026-05-22T15:00:00+00:00",
            ),
            make_job_debug_event(
                label="Agent run: src-a",
                phase="agent_run",
                timestamp="2026-05-22T15:10:00+00:00",
            ),
        ],
    }
    timeline = get_job_debug_timeline(job)
    labels = [e.get("label") for e in timeline]
    assert labels.index("Data file uploaded") < labels.index("Source registry")
    assert labels.index("Source registry") < labels.index("Agent run: src-a")


def test_synthetic_inventory_events_sort_before_agent_runs():
    job = {
        "created_at": "2026-05-22T07:30:00",
        "data_files": [{"file_id": "f1", "file_name": "a.xlsx", "sheets": ["S1"]}],
        "source_registry": [{"source_id": "src-a", "sheet_name": "S1", "file_id": "f1"}],
        "job_debug_events": [],
    }
    append_job_debug_event(
        job,
        make_job_debug_event(
            label="Agent run: src-a",
            phase="agent_run",
            timestamp="2026-05-22T07:35:00+00:00",
        ),
    )

    timeline = rebuild_job_debug_timeline(job)
    labels = [e.get("label") for e in timeline]
    assert labels.index("Upload inventory") < labels.index("Agent run: src-a")
    assert labels.index("Source registry") < labels.index("Agent run: src-a")


def test_collapse_repeated_mapping_saves_per_source():
    job = {
        "mapping_registry": [
            {"source_id": "src-a", "column_name": "A", "target_column": "date"},
        ],
        "job_debug_events": [],
    }
    for i in range(3):
        append_job_debug_event(
            job,
            make_job_debug_event(
                label="Schema mapping saved",
                phase="mapping",
                source_id="src-a",
                sheet_name="Data",
                operation="submit",
                summary=f"Save {i}",
                timestamp=f"2026-05-22T10:0{i}:00+00:00",
            ),
        )
    timeline = get_job_debug_timeline(job)
    mapping_saves = [e for e in timeline if e.get("label") == "Schema mapping saved"]
    assert len(mapping_saves) == 1
    assert mapping_saves[0]["metadata"].get("save_count") == 3
    assert len(mapping_saves[0]["metadata"].get("save_history") or []) == 2
    assert "Data" in (mapping_saves[0].get("display_label") or "")
    assert mapping_saves[0]["metadata"].get("mapping_table")


def test_hide_registry_and_inventory_when_live_upload_and_saves_exist():
    job = {
        "data_files": [{"file_id": "f1", "file_name": "a.xlsx", "sheets": ["S1"]}],
        "layout_registry": [{"source_id": "src-a", "start_row": 1, "end_row": 10}],
        "mapping_registry": [{"source_id": "src-a", "column_name": "X", "target_column": "date"}],
        "job_debug_events": [
            make_job_debug_event(
                label="Data file uploaded",
                phase="upload",
                operation="data_file",
                summary="Added a.xlsx",
            ),
            make_job_debug_event(
                label="Demarcation saved",
                phase="demarcation",
                source_id="src-a",
                operation="submit",
            ),
            make_job_debug_event(
                label="Schema mapping saved",
                phase="mapping",
                source_id="src-a",
                operation="submit",
            ),
        ],
    }
    labels = [e.get("label") for e in get_job_debug_timeline(job)]
    assert "Upload inventory" not in labels
    assert "Layout registry" not in labels
    assert "Mapping registry" not in labels
    assert labels.count("Schema mapping saved") == 1


def test_mapping_save_warning_when_unresolved():
    job = {
        "mapping_registry": [
            {"source_id": "src-a", "column_name": "Orphan", "target_column": "No match"},
        ],
        "job_debug_events": [
            make_job_debug_event(
                label="Schema mapping saved",
                phase="mapping",
                source_id="src-a",
                sheet_name="Sheet1",
                operation="submit",
                status="success",
            ),
        ],
    }
    ev = next(e for e in get_job_debug_timeline(job) if e.get("label") == "Schema mapping saved")
    assert ev.get("status") == "warning"
    assert ev["metadata"].get("unresolved_targets") == 1


def test_data_files_and_setup_summaries():
    job = {
        "data_files": [{"file_id": "f1", "file_name": "w.xlsx", "sheets": ["A"]}],
        "source_registry": [{"source_id": "s1", "sheet_name": "A", "file_id": "f1"}],
        "layout_registry": [{"source_id": "s1"}],
        "mapping_registry": [{"source_id": "s1"}],
        "ux_source_progress": {"s1": {"layout_complete": True, "mapping_complete": False}},
    }
    dfs = data_files_summary(job)
    assert dfs["file_count"] == 1
    assert dfs["total_sheets"] == 1
    setup = job_setup_summary(job)
    assert setup["source_count"] == 1
    assert setup["per_source"][0]["layout_complete"] is True
    assert setup["per_source"][0]["mapping_complete"] is False
