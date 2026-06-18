"""Removing sources / data files from in-memory jobs."""

from sia.agent.job_manager import JobManager


def test_remove_one_sheet_updates_registry_and_sheets(tmp_path) -> None:
    p = tmp_path / "t.xlsx"
    p.write_bytes(b"x")
    m = JobManager()
    m.create_job("j1", "t.xlsx", str(p), sheets=["A", "B"])
    job = m.get_job("j1")
    assert len(job["source_registry"]) == 2
    sid0 = job["source_registry"][0]["source_id"]
    assert m.remove_source("j1", sid0)
    job = m.get_job("j1")
    assert len(job["source_registry"]) == 1
    assert len(job["data_files"]) == 1
    assert job["data_files"][0]["sheets"] == ["B"]


def test_remove_data_file_drops_all_sheets(tmp_path) -> None:
    p1 = tmp_path / "a.xlsx"
    p1.write_bytes(b"a")
    p2 = tmp_path / "b.xlsx"
    p2.write_bytes(b"b")
    m = JobManager()
    m.create_job("j2", "a.xlsx", str(p1), sheets=["S1"])
    m.add_data_file("j2", "b.xlsx", str(p2), sheets=["T1"])
    job = m.get_job("j2")
    assert len(job["source_registry"]) == 2
    fid2 = job["data_files"][1]["file_id"]
    assert m.remove_data_file("j2", fid2)
    job = m.get_job("j2")
    assert len(job["data_files"]) == 1
    assert len(job["source_registry"]) == 1
    assert job["source_registry"][0]["sheet_name"] == "S1"
