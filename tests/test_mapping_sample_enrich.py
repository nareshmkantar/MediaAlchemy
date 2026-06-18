"""Re-hydrate unique_values when mapping is loaded from registry (no samples persisted)."""

import pandas as pd

from sia.agent.schema_mapper import (
    enrich_mapping_rows_from_dataframe,
    mapping_rows_need_sample_enrichment,
)


def test_mapping_rows_need_sample_enrichment():
    assert mapping_rows_need_sample_enrichment([{"column_name": "a", "unique_values": []}])
    assert mapping_rows_need_sample_enrichment([{"column_name": "a"}])
    assert not mapping_rows_need_sample_enrichment(
        [{"column_name": "a", "unique_values": ["x"]}],
    )


def test_enrich_mapping_rows_from_dataframe():
    df = pd.DataFrame(
        {
            "date_paid_media": ["2024-01-01", "2024-01-02"],
            "publisher_name_paid_media": ["TikTok", "Pinterest"],
        }
    )
    rows = [
        {
            "column_name": "date_paid_media",
            "source_column": "date_paid_media",
            "target_column": "calendar_date",
            "unique_values": [],
        },
        {
            "column_name": "publisher_name_paid_media",
            "source_column": "publisher_name_paid_media",
            "target_column": "publisher",
        },
    ]
    out = enrich_mapping_rows_from_dataframe(rows, df)
    assert out[0]["unique_values"]
    assert "2024-01-01" in out[0]["unique_values"][0]
    assert out[1]["unique_values"]
    assert "TikTok" in out[1]["unique_values"][0]
