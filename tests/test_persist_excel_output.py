"""Tests for the canonical Excel persistence helper used by the web server."""
from pathlib import Path

import pandas as pd
import pytest

import web_server


@pytest.fixture
def tmp_output_dir(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(web_server, "OUTPUT_FOLDER", output_dir, raising=True)
    return output_dir


def test_persist_excel_output_writes_file_and_sets_paths(tmp_output_dir):
    df = pd.DataFrame(
        [
            {"date": "2026-04-13", "channel": "search", "spend": 100.0},
            {"date": "2026-04-14", "channel": "search", "spend": 150.0},
        ]
    )
    job = {"trace": {}, "output_files": {"schema": "/tmp/schema.json"}}

    path = web_server._persist_excel_output("job-abc", job, df)

    assert path is not None
    assert Path(path).exists()
    assert Path(path).suffix == ".xlsx"
    assert job["full_excel_path"] == path
    assert job["output_file"] == path
    assert job["output_files"]["excel"] == path
    assert job["output_files"]["schema"] == "/tmp/schema.json"
    assert job["trace"]["output_file"] == path

    loaded = pd.read_excel(path)
    assert list(loaded.columns) == ["date", "channel", "spend"]
    assert len(loaded) == 2


def test_persist_excel_output_skips_empty_df(tmp_output_dir):
    df = pd.DataFrame()
    job = {}
    path = web_server._persist_excel_output("job-empty", job, df)
    assert path is None
    assert "full_excel_path" not in job
    assert "output_file" not in job


def test_persist_excel_output_skips_none(tmp_output_dir):
    job = {}
    path = web_server._persist_excel_output("job-none", job, None)
    assert path is None
    assert job == {}
