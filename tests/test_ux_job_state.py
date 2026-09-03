"""UX stage fields on jobs (stage 0–4 stepper support)."""
import pytest

from sia.agent.job_manager import JobManager


@pytest.fixture()
def manager():
    m = JobManager()
    m.create_job("ux_job", "a.xlsx", "/tmp/a.xlsx", sheets=["S1"])
    # Hierarchy registration is part of stage0; mark complete for layout/mapping tests.
    sid = m.get_source_id("ux_job", "S1")
    m.save_hierarchy_registry(
        "ux_job",
        [{
            "source_id": sid,
            "publisher_id": "dv360",
            "publisher_name": "DV360",
            "grain_level_id": "campaign",
            "grain_level": 2,
            "grain_level_name": "Campaign",
            "registered": True,
        }],
        mark_complete=True,
    )
    return m


def test_create_job_has_ux_defaults():
    m = JobManager()
    m.create_job("ux_defaults", "a.xlsx", "/tmp/a.xlsx", sheets=["S1"])
    job = m.get_job("ux_defaults")
    assert job["current_ux_stage"] == 0
    assert job["ux_source_progress"] == {}
    assert job["relationships_gate_complete"] is True
    assert job.get("hierarchy_register_complete") is False


def test_mark_layout_and_mapping(manager):
    sid = manager.get_source_id("ux_job", "S1")
    manager.mark_ux_source_layout("ux_job", sid, True)
    assert not manager.build_ux_stepper_summary(manager.get_job("ux_job"))["stage1_complete"]
    manager.mark_ux_source_mapping("ux_job", sid, True)
    assert manager.build_ux_stepper_summary(manager.get_job("ux_job"))["stage1_complete"]


def test_multi_file_clears_relationship_gate(manager):
    manager.add_data_file("ux_job", "b.xlsx", "/tmp/b.xlsx", sheets=["T1"])
    job = manager.get_job("ux_job")
    assert job["relationships_gate_complete"] is False
    assert job["hierarchy_register_complete"] is False


def test_save_file_relationships_sets_gate(manager):
    manager.add_data_file("ux_job", "b.xlsx", "/tmp/b.xlsx", sheets=["T1"])
    # Re-complete hierarchy after second file invalidated it
    sids = [s["source_id"] for s in manager.get_job("ux_job")["source_registry"]]
    manager.save_hierarchy_registry(
        "ux_job",
        [{
            "source_id": sid,
            "publisher_id": "dv360",
            "grain_level_id": "campaign",
            "grain_level": 2,
            "grain_level_name": "Campaign",
            "registered": True,
        } for sid in sids],
        mixed_grain_acknowledged=True,
        mark_complete=True,
    )
    manager.save_file_relationships(
        "ux_job",
        [{"relationship_id": "r1", "relationship_kind": "independent", "source_ids": ["a", "b"]}],
    )
    assert manager.get_job("ux_job")["relationships_gate_complete"] is True


def test_patch_ux_state(manager):
    manager.patch_ux_state("ux_job", {"current_ux_stage": 3})
    assert manager.get_job("ux_job")["current_ux_stage"] == 3


def test_derived_stage1_from_registries_when_ux_empty(manager):
    sid = manager.get_source_id("ux_job", "S1")
    job = manager.get_job("ux_job")
    # Fixture already wrote hierarchy progress; clear only layout/mapping flags
    job["ux_source_progress"] = {}
    job["layout_registry"].append(
        {"source_id": sid, "block_category": "Main Data", "decision": "keep", "start_row": 0},
    )
    job["mapping_registry"].append({"source_id": sid, "raw_column": "a", "target_field": "b"})
    uxs = manager.build_ux_stepper_summary(job)
    assert uxs["ux_source_progress"][sid]["layout_complete"] is True
    assert uxs["ux_source_progress"][sid]["mapping_complete"] is True
    assert uxs["stage1_complete"] is True


def test_derived_mapping_complete_when_no_main_data(manager):
    sid = manager.get_source_id("ux_job", "S1")
    job = manager.get_job("ux_job")
    # Context/noise (not kept main data) ⇒ mapping N/A for this sheet
    job["layout_registry"].append(
        {"source_id": sid, "block_category": "Metadata", "decision": "context"},
    )
    uxs = manager.build_ux_stepper_summary(job)
    assert uxs["ux_source_progress"][sid]["layout_complete"] is True
    assert uxs["ux_source_progress"][sid]["mapping_complete"] is True
    assert uxs["stage1_complete"] is True


def test_derived_mapping_incomplete_when_main_without_mapping(manager):
    sid = manager.get_source_id("ux_job", "S1")
    job = manager.get_job("ux_job")
    job["layout_registry"].append(
        {"source_id": sid, "block_category": "Main Data", "decision": "keep"},
    )
    uxs = manager.build_ux_stepper_summary(job)
    assert uxs["ux_source_progress"][sid]["layout_complete"] is True
    assert uxs["ux_source_progress"][sid]["mapping_complete"] is False
    assert uxs["stage1_complete"] is False
