"""Shared CP-07 multisheet frames for regression tests."""

from __future__ import annotations

import pandas as pd


def radio_de_bleed_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2025-03-01"],
            "publisher": ["SiteC"],
            "channel": ["digital"],
            "market": ["UK"],
            "spends": [258],
            "impressions": [3030],
        }
    )


def radio_de_correct_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2025-03-01"],
            "publisher": ["SiteC"],
            "channel": ["radio"],
            "market": ["DE"],
            "spends": [230],
            "impressions": [2270],
        }
    )


def digital_uk_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2025-03-01"],
            "publisher": ["SiteC"],
            "channel": ["digital"],
            "market": ["UK"],
            "spends": [258],
            "impressions": [3030],
        }
    )


def cp07_multisheet_job(*, job_id: str = "cp07-multisheet") -> dict:
    """Minimal job dict for Digital_UK + Radio_DE."""
    radio_sid = f"{job_id}:file_001:Radio_DE"
    digital_sid = f"{job_id}:file_001:Digital_UK"
    return {
        "id": job_id,
        "filename": "CP-07.xlsx",
        "source_registry": [
            {
                "source_id": digital_sid,
                "sheet_name": "Digital_UK",
                "file_name": "CP-07.xlsx",
            },
            {
                "source_id": radio_sid,
                "sheet_name": "Radio_DE",
                "file_name": "CP-07.xlsx",
            },
        ],
        "pipeline_evals": {},
    }


def radio_de_context_packet(*, source_id: str = "job:file:Radio_DE") -> dict:
    return {
        "lineage": {"sheet_name": "Radio_DE", "source_id": source_id},
        "interpreted_context": {
            "fields": {"market": "DE", "channel": "radio"},
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "DE",
                    "scope": "block",
                    "confidence": 1.0,
                    "source_id": source_id,
                    "evidence_line": "Market: Germany",
                },
                "channel": {
                    "name": "channel",
                    "value": "radio",
                    "scope": "block",
                    "confidence": 1.0,
                    "source_id": source_id,
                },
            },
        },
    }
