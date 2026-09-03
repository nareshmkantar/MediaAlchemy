"""Schema template upload validates target template shape before accepting."""

import io
import json
from pathlib import Path

import web_server as ws
from sia.agent.job_manager import job_manager


def _valid_template():
    return {
        "x_scope": {
            "uid_hierarchy": ["date", "channel", "publisher"],
            "metrics": ["spends", "impressions"],
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {"metric_rules": {"spends": "sum", "impressions": "sum"}},
        "properties": {
            "date": {"type": "string", "format": "date"},
            "channel": {"type": "string"},
            "publisher": {"type": "string"},
            "spends": {"type": "number"},
            "impressions": {"type": "integer"},
        },
    }


def _invalid_template_missing_property():
    tpl = _valid_template()
    tpl["x_scope"]["uid_hierarchy"] = ["date", "channel", "market", "publisher"]
    return tpl


def test_schema_upload_rejects_invalid_template(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "UPLOAD_FOLDER", tmp_path)
    job_id = "schema01"
    job_manager.create_job(job_id, "data.xlsx", str(tmp_path / "data.xlsx"), sheets=["Sheet1"])

    payload = json.dumps(_invalid_template_missing_property()).encode("utf-8")
    client = ws.app.test_client()
    res = client.post(
        "/api/upload",
        data={
            "file": (io.BytesIO(payload), "context_test.json"),
            "type": "schema",
            "job_id": job_id,
        },
        content_type="multipart/form-data",
    )

    assert res.status_code == 400
    body = res.get_json()
    assert "Invalid target template" in body["error"]
    assert "market" in body["template_validation_error"]
    job = job_manager.get_job(job_id)
    assert not job.get("template_path")


def test_schema_upload_accepts_valid_template(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "UPLOAD_FOLDER", tmp_path)
    job_id = "schema02"
    job_manager.create_job(job_id, "data.xlsx", str(tmp_path / "data.xlsx"), sheets=["Sheet1"])

    payload = json.dumps(_valid_template()).encode("utf-8")
    client = ws.app.test_client()
    res = client.post(
        "/api/upload",
        data={
            "file": (io.BytesIO(payload), "template.json"),
            "type": "schema",
            "job_id": job_id,
        },
        content_type="multipart/form-data",
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["success"] is True
    assert body["target_columns"] == [
        "date",
        "channel",
        "publisher",
        "spends",
        "impressions",
    ]
    job = job_manager.get_job(job_id)
    assert job.get("template_path")
    assert Path(job["template_path"]).exists()
