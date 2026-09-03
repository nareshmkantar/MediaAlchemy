"""Join-key inference for multi-source union (metadata / add_column grain)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from sia.agent.parent_collation_graph import diagnose_stacked_union_duplicates
from sia.agent.relationships import (
    _collate_frames_baseline,
    propose_file_relationships,
    refresh_union_join_keys_in_proposals,
)
from sia.agent.target_template_utils import normalize_target_template

CP07_TEMPLATE = normalize_target_template(
    {
        "x_scope": {
            "uid_hierarchy": ["date", "channel", "market", "publisher"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": [],
        },
        "properties": {
            "date": {"type": "date"},
            "channel": {"type": "string"},
            "market": {"type": "string"},
            "publisher": {"type": "string"},
            "spends": {"type": "number"},
            "impressions": {"type": "number"},
        },
    }
)

SHEET_META = {
    "Digital_UK": ("digital", "UK"),
    "TV_FR": ("tv", "FR"),
    "Radio_DE": ("radio", "DE"),
    "Print_US": ("print", "US"),
}


def _cp07_post_exec_frames(job_id: str = "602f49b2") -> dict[str, pd.DataFrame]:
    base = Path(f"runtime/cleaned_templates/{job_id}")
    frames: dict[str, pd.DataFrame] = {}
    for sheet, (channel, market) in SHEET_META.items():
        raw = pd.read_excel(base / f"{job_id}_file_001_{sheet}_clean.xlsx")
        frames[f"src_{sheet}"] = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["Date"]).dt.strftime("%Y-%m-%d"),
                "channel": channel,
                "market": market,
                "publisher": raw["Publisher"].astype(str),
                "spends": pd.to_numeric(raw["Spend"], errors="coerce"),
                "impressions": pd.to_numeric(raw["Impressions"], errors="coerce"),
            }
        )
    return frames


def _cp07_summaries_mapped_only() -> list[dict]:
    return [
        {
            "source_id": f"src_{sheet}",
            "file_name": "CP-07.xlsx",
            "sheet_name": sheet,
            "contains_main_data": True,
            "contains_reference_data": False,
            "mapped_targets": ["date", "publisher", "spends", "impressions"],
            "uid": [],
        }
        for sheet in SHEET_META
    ]


def test_join_keys_include_metadata_columns_when_present_in_output():
    frames = _cp07_post_exec_frames()
    proposals = propose_file_relationships(
        _cp07_summaries_mapped_only(),
        target_template=CP07_TEMPLATE,
        frames_by_source=frames,
    )
    union = next(p for p in proposals if p.get("relationship_kind") == "union")
    assert union["join_keys"] == ["date", "channel", "market", "publisher"]


def test_join_keys_without_output_columns_stays_mapped_only():
    proposals = propose_file_relationships(
        _cp07_summaries_mapped_only(),
        target_template=CP07_TEMPLATE,
    )
    union = next(p for p in proposals if p.get("relationship_kind") == "union")
    assert union["join_keys"] == ["date", "publisher"]


def test_refresh_union_join_keys_updates_stale_checkpoint_proposals():
    frames = _cp07_post_exec_frames()
    stale = [
        {
            "relationship_id": "relationship_union_1",
            "relationship_kind": "union",
            "source_ids": list(frames.keys()),
            "join_keys": ["date", "publisher"],
        }
    ]
    refreshed = refresh_union_join_keys_in_proposals(
        stale,
        _cp07_summaries_mapped_only(),
        target_template=CP07_TEMPLATE,
        frames_by_source=frames,
    )
    assert refreshed[0]["join_keys"] == ["date", "channel", "market", "publisher"]


def test_full_grain_join_keys_avoid_cp07_cross_sheet_false_duplicates():
    frames = _cp07_post_exec_frames()
    proposals = propose_file_relationships(
        _cp07_summaries_mapped_only(),
        target_template=CP07_TEMPLATE,
        frames_by_source=frames,
    )
    stacked = _collate_frames_baseline(frames, proposals)
    diag = diagnose_stacked_union_duplicates(stacked, proposals, [])
    assert diag["subset_for_dedupe"] == ["date", "channel", "market", "publisher"]
    assert int(diag.get("duplicate_rows_on_keys") or 0) == 0
