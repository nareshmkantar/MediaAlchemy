from pathlib import Path

import pandas as pd

from sia.agent.artifact_exports import (
    build_pre_transform_dataframe,
    persist_processing_artifacts,
)


def test_build_pre_transform_dataframe_keeps_mapped_and_supporting_columns():
    df = pd.DataFrame(
        {
            "Posting date": ["2024-01-01"],
            "Post": ["IG Reel 1"],
            "Saves": [100],
            "Market": ["ID"],
            "Ignore Me": ["x"],
        }
    )
    target_template = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel"],
            "metrics": ["impressions"],
            "supporting_columns": [],
        },
        "properties": {
            "date": {"type": "string", "format": "date"},
            "channel": {"type": "string"},
            "impressions": {"type": "integer"},
        },
    }
    approved_mappings = [
        {"source_column": "Posting date", "target_column": "date", "decision": "keep", "role": "primary"},
        {"source_column": "Saves", "target_column": "impressions", "decision": "keep", "role": "primary"},
        {"source_column": "Post", "target_column": "No match", "decision": "keep", "role": "supporting"},
        {"source_column": "Ignore Me", "target_column": "No match", "decision": "discard", "role": "exclude"},
    ]

    out = build_pre_transform_dataframe(df, target_template, approved_mappings)

    assert list(out.columns) == ["date", "impressions", "Post"]
    assert out.iloc[0].to_dict() == {
        "date": "2024-01-01",
        "impressions": 100,
        "Post": "IG Reel 1",
    }


def test_persist_processing_artifacts_writes_excel_and_zip_outputs(tmp_path: Path):
    pre_frames = {
        "source_a__Sheet1": pd.DataFrame({"date": ["2024-01-01"], "impressions": [10]}),
        "source_b__Sheet2": pd.DataFrame({"date": ["2024-01-08"], "impressions": [20]}),
    }
    post_df = pd.DataFrame({"date": ["2024-01-01"], "channel": ["instagram"], "impressions": [30]})

    artifacts = persist_processing_artifacts(tmp_path, "job123", pre_frames, post_df)

    assert len(artifacts["pre_transform_files"]) == 2
    assert artifacts["post_transform_file"]
    assert artifacts["pre_transform_bundle"]
    assert artifacts["artifacts_bundle"]

    for key in ("post_transform_file", "pre_transform_bundle", "artifacts_bundle"):
        assert Path(artifacts[key]).exists()
    for file_path in artifacts["pre_transform_files"]:
        assert Path(file_path).exists()
