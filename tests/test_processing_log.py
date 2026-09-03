from pathlib import Path

from sia.agent.job_manager import JobManager
from sia.context.diff_log import build_debug_export_payload
from sia.debug.job_debug_events import append_job_debug_event, make_job_debug_event
from sia.debug.processing_log import (
    append_processing_step,
    delete_job_logs,
    processing_jsonl_path,
    processing_log_path,
    read_processing_log_text,
)


def test_append_processing_step_writes_text_and_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEMA_AGENT_LOGS_DIR", str(tmp_path))
    step = {
        "timestamp": "2026-08-16T10:00:00",
        "step": "layout.extract",
        "message": "ok — rows 14→14",
    }
    append_processing_step("job_abc", step)

    text = read_processing_log_text("job_abc")
    assert text is not None
    assert "layout.extract" in text
    assert "ok — rows 14→14" in text
    jsonl = processing_jsonl_path("job_abc").read_text(encoding="utf-8")
    assert "layout.extract" in jsonl


def test_job_manager_status_writes_and_delete_removes_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEMA_AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.delenv("SCHEMA_AGENT_PERSIST_JOBS", raising=False)
    monkeypatch.delenv("SCHEMA_AGENT_METADATA_DB", raising=False)
    manager = JobManager()
    manager.create_job("job_log1", "amazon.csv", str(tmp_path / "amazon.csv"))
    manager.update_job_status("job_log1", "processing", "Extracted 14 rows", "layout.extract")

    log_file = processing_log_path("job_log1")
    assert log_file.is_file()
    assert "Extracted 14 rows" in log_file.read_text(encoding="utf-8")

    manager.remove_job("job_log1")
    assert not log_file.exists()
    assert delete_job_logs("job_log1") is False


def test_debug_event_and_export_include_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEMA_AGENT_LOGS_DIR", str(tmp_path))
    job = {"id": "job_exp", "filename": "cm360.csv", "status": "completed", "steps": []}
    event = make_job_debug_event(
        label="Data file uploaded",
        phase="upload",
        status="success",
        summary="Added CM 360",
    )
    append_job_debug_event(job, event)
    job["steps"] = [
        {"timestamp": "2026-08-16T10:00:00", "step": "Starting", "message": "Initializing agent..."}
    ]
    append_processing_step("job_exp", job["steps"][0])

    payload = build_debug_export_payload(job)
    assert payload["steps"][0]["step"] == "Starting"
    assert payload["job_debug_events"][0]["label"] == "Data file uploaded"
    assert payload["processing_log"] is not None
    assert "Starting" in payload["processing_log"]
    assert Path(tmp_path, "job_exp", "events.jsonl").is_file()
