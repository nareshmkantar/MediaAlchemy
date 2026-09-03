"""Fixtures for debug/checkpoint API tests."""

from __future__ import annotations

import pytest

from tests.debug.test_checkpoint_export import _sample_job


@pytest.fixture
def flask_app_job():
    import web_server as ws

    job = _sample_job()
    job_id = str(job["id"])
    source_id = job["source_registry"][0]["source_id"]
    ws.job_manager.jobs[job_id] = job
    try:
        yield {
            "client": ws.app.test_client(),
            "job_id": job_id,
            "source_id": source_id,
            "job": job,
        }
    finally:
        ws.job_manager.jobs.pop(job_id, None)
