"""Append-only per-job processing logs under ``runtime/logs/<job_id>/``.

These are the durable copy of the Processing page timeline (and orchestration
debug events). They are always written — unlike per-tool CSV snapshots, which
follow ``SIA_DEBUG_ENABLED``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SAFE_JOB_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def logs_root() -> Path:
    raw = (os.environ.get("SCHEMA_AGENT_LOGS_DIR") or "").strip()
    root = Path(raw) if raw else _repo_root() / "runtime" / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_job_id(job_id: str) -> str:
    text = _SAFE_JOB_ID.sub("_", str(job_id or "").strip())
    return text[:80] or "unknown"


def job_log_dir(job_id: str) -> Path:
    path = logs_root() / safe_job_id(job_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def processing_log_path(job_id: str) -> Path:
    return job_log_dir(job_id) / "processing.log"


def processing_jsonl_path(job_id: str) -> Path:
    return job_log_dir(job_id) / "processing.jsonl"


def events_jsonl_path(job_id: str) -> Path:
    return job_log_dir(job_id) / "events.jsonl"


def format_processing_line(step: Dict[str, Any]) -> str:
    ts = str(step.get("timestamp") or "").strip()
    name = str(step.get("step") or "").strip()
    message = str(step.get("message") or "").replace("\n", " ").strip()
    return f"{ts} | {name} | {message}"


def _append_text(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")
        handle.flush()


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        handle.flush()


def ensure_job_log(job_id: str, *, filename: str = "") -> Path:
    """Create the job log folder and a header if the text log does not exist yet."""
    directory = job_log_dir(job_id)
    text_path = processing_log_path(job_id)
    if not text_path.exists():
        header = f"# processing log  job={safe_job_id(job_id)}"
        if filename:
            header += f"  file={filename}"
        _append_text(text_path, header)
    return directory


def append_processing_step(job_id: str, step: Dict[str, Any]) -> None:
    """Append one Processing-log step to ``processing.log`` and ``processing.jsonl``."""
    if not job_id or not isinstance(step, dict):
        return
    try:
        ensure_job_log(job_id)
        record = {
            "timestamp": step.get("timestamp"),
            "step": step.get("step"),
            "message": step.get("message"),
        }
        extra = {
            key: value
            for key, value in step.items()
            if key not in record and value is not None
        }
        if extra:
            record["extra"] = extra
        _append_jsonl(processing_jsonl_path(job_id), record)
        _append_text(processing_log_path(job_id), format_processing_line(step))
    except Exception as exc:
        logger.warning("Failed to write processing log for job %s: %s", job_id, exc)


def append_debug_event(job_id: str, event: Dict[str, Any]) -> None:
    """Append one ``job_debug_events`` row to ``events.jsonl``."""
    if not job_id or not isinstance(event, dict):
        return
    try:
        ensure_job_log(job_id)
        slim = {
            "timestamp": event.get("timestamp"),
            "phase": event.get("phase"),
            "status": event.get("status"),
            "label": event.get("label"),
            "source_id": event.get("source_id"),
            "sheet_name": event.get("sheet_name"),
            "summary": event.get("summary")
            or event.get("output_summary")
            or event.get("input_summary")
            or "",
        }
        _append_jsonl(events_jsonl_path(job_id), slim)
    except Exception as exc:
        logger.warning("Failed to write debug event log for job %s: %s", job_id, exc)


def delete_job_logs(job_id: str) -> bool:
    """Remove ``runtime/logs/<job_id>/``. Returns True if a folder was deleted."""
    if not job_id:
        return False
    path = logs_root() / safe_job_id(job_id)
    if not path.exists():
        return False
    try:
        shutil.rmtree(path)
        return True
    except OSError as exc:
        logger.warning("Failed to delete processing logs for job %s: %s", job_id, exc)
        return False


def read_processing_log_text(job_id: str) -> Optional[str]:
    path = logs_root() / safe_job_id(job_id) / "processing.log"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
