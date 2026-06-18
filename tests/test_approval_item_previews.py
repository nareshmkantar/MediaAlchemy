"""Planner review: approval_items enriched with data_preview column samples."""

import web_server as ws


def test_enrich_approval_items_uses_target_column_when_present():
    job = {
        "data_preview": [{"date": "2024-01-01", "spend": 100}],
        "mapping_registry": [],
        "mapping_source_id": None,
    }
    out = ws._enrich_approval_items_with_previews(
        job,
        [{"target_column": "date", "summary": "Check"}],
    )
    assert out[0]["preview_values"] == ["2024-01-01"]
    assert out[0]["preview_column_label"] == "date"


def test_enrich_approval_items_falls_back_to_mapped_source_column():
    job = {
        "data_preview": [{"Posting date": "2024-01-02", "X": 1}],
        "mapping_registry": [
            {
                "source_id": "src1",
                "source_column": "Posting date",
                "target_column": "date",
                "decision": "Keep",
            }
        ],
        "mapping_source_id": "src1",
    }
    out = ws._enrich_approval_items_with_previews(
        job,
        [{"target_column": "date", "summary": "Check date"}],
    )
    assert out[0]["preview_values"] == ["2024-01-02"]
    assert "←" in out[0]["preview_column_label"]
