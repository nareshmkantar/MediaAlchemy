"""Shared fixtures for context handoff integration tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

from sia.agent.context_packet import build_canonical_planning_view
from sia.agent.job_manager import JobManager

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "context_flow"


@pytest.fixture
def paid_media_target_template() -> Dict[str, Any]:
    with open(FIXTURES_DIR / "target_template.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def context_flow_workbook(tmp_path: Path) -> Path:
    """Workbook with a metadata block (rows 1–3) and main data (rows 5+)."""
    file_path = tmp_path / "media_with_metadata.xlsx"
    pd.DataFrame(
        [
            ["Publisher", "Instagram", "", ""],
            ["Market", "UK", "", ""],
            ["Modeling Period", "2025-01-01 to 2025-03-31", "", ""],
            ["", "", "", ""],
            ["Event Date", "Spend", "Impressions", "Clicks"],
            ["2025-01-15", 120.5, 4000, 22],
            ["2025-01-16", 95.0, 3100, 18],
        ]
    ).to_excel(file_path, index=False, header=False)
    return file_path


def ui_demarcation_blocks() -> List[Dict[str, Any]]:
    """Blocks as the Setup UI would submit after 'Use as Metadata' + 'Treat as Data'."""
    return [
        {
            "id": "meta_top",
            "label": "Top metadata",
            "category": "metadata",
            "decision": "Context",
            "coordinates": {
                "start_row": 0,
                "end_row": 2,
                "start_col": 0,
                "end_col": 1,
            },
        },
        {
            "id": "main_table",
            "label": "Daily metrics",
            "category": "main_data",
            "decision": "Keep",
            "coordinates": {
                "start_row": 4,
                "end_row": 6,
                "start_col": 0,
                "end_col": 3,
                "header_row": 4,
            },
        },
    ]


def demarcation_blocks_to_layout_registry(
    source_id: str,
    sheet_name: str,
    blocks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Persist demarcation blocks the way ``web_server.blocks_to_layout_registry`` does, without global state."""
    rows: List[Dict[str, Any]] = []
    for idx, block in enumerate(blocks or []):
        coords = block.get("coordinates", block) if isinstance(block, dict) else {}
        hr = int(coords.get("header_row", coords.get("start_row", 0)))
        rows.append(
            {
                "layout_id": f"{source_id}:layout:{idx}",
                "source_id": source_id,
                "sheet_name": sheet_name,
                "block_id": str(block.get("id") or f"block_{idx}"),
                "block_label": block.get("label", f"Block {idx + 1}"),
                "block_category": block.get("category", "main_data"),
                "start_row": int(coords.get("start_row", 0)),
                "end_row": int(coords.get("end_row", 0)),
                "start_col": int(coords.get("start_col", 0)),
                "end_col": int(coords.get("end_col", 0)),
                "header_row": hr,
                "decision": block.get("decision", "Keep"),
                "confidence": float(block.get("confidence", 1.0) or 1.0),
                "approved_by": "user",
                "layout_version": 1,
                "coordinates": {
                    "start_row": int(coords.get("start_row", 0)),
                    "end_row": int(coords.get("end_row", 0)),
                    "start_col": int(coords.get("start_col", 0)),
                    "end_col": int(coords.get("end_col", 0)),
                    "header_row": hr,
                },
            }
        )
    return rows


@pytest.fixture
def context_flow_job_manager() -> JobManager:
    return JobManager()


@pytest.fixture
def context_flow_job(
    context_flow_workbook: Path,
    context_flow_job_manager: JobManager,
    paid_media_target_template: Dict[str, Any],
) -> Dict[str, Any]:
    """Job after upload + demarcation submit + mapping + business rules (no LLM)."""
    manager = context_flow_job_manager
    file_path = str(context_flow_workbook)
    job = manager.create_job(
        "job_ctx_flow",
        "media_with_metadata.xlsx",
        file_path,
        sheets=["Sheet1"],
    )
    job_id = job["id"]
    source_id = manager.get_source_id(job_id, "Sheet1")

    job = manager.get_job(job_id)
    job["scoped_source"] = {
        "sheet_name": "Sheet1",
        "header_row": 4,
        "scope_type": "demarcated_table",
        "analysis_bounds": {"start_row": 4, "end_row": 6, "start_col": 0, "end_col": 3},
    }

    layout_rows = demarcation_blocks_to_layout_registry(source_id, "Sheet1", ui_demarcation_blocks())
    manager.save_layout_registry(job_id, layout_rows, source_id=source_id)

    manager.save_mapping_registry(
        job_id,
        [
            {
                "source_id": source_id,
                "source_column": "Event Date",
                "target_column": "date_paid_media",
                "decision": "Keep",
            },
            {
                "source_id": source_id,
                "source_column": "Spend",
                "target_column": "total_cost_paid_media",
                "decision": "Keep",
            },
            {
                "source_id": source_id,
                "source_column": "Impressions",
                "target_column": "impressions_paid_media",
                "decision": "Keep",
            },
            {
                "source_id": source_id,
                "source_column": "Clicks",
                "target_column": "clicks_paid_media",
                "decision": "Keep",
            },
        ],
        sheet_name="Sheet1",
    )

    manager.save_business_rules(
        job_id,
        [
            {
                "applies_to_source_id": source_id,
                "target_column": "date_paid_media",
                "rule_type": "format",
                "rule_expression": "yyyy-mm-dd",
                "priority": 10,
            },
            {
                "applies_to_source_id": source_id,
                "target_column": "clicks_paid_media",
                "rule_type": "fill_blank",
                "rule_expression": "0",
                "priority": 20,
            },
            {
                "applies_to_source_id": source_id,
                "target_column": "publisher_paid_media",
                "rule_type": "default_value",
                "rule_expression": "Instagram",
                "priority": 30,
            },
        ],
        sheet_name="Sheet1",
    )

    manager.update_source_metadata(
        job_id,
        "Sheet1",
        {
            "user_notes": ["Use booking date, not invoice date"],
            "aggregation_logic": "impressions: sum",
        },
    )

    job = manager.get_job(job_id)
    job["_target_template_fixture"] = paid_media_target_template
    return job


def enrich_packet_for_planner(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror graph nodes: planner reads ``planning_summary`` on the context packet."""
    out = dict(packet)
    out["planning_summary"] = build_canonical_planning_view(out)
    return out
