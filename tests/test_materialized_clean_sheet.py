import pandas as pd

from sia.agent.materialized_clean_sheet import (
    drop_discarded_mapping_columns,
    resolve_processing_workbook,
    scoped_source_for_materialized_workbook,
)


def test_drop_discarded_mapping_columns():
    df = pd.DataFrame({"a": [1], "b": [2], "c": [3]})
    mappings = [
        {"source_column": "b", "decision": "Discard", "target_column": "No match"},
        {"source_column": "a", "decision": "Keep", "target_column": "a"},
    ]
    out = drop_discarded_mapping_columns(df, mappings)
    assert list(out.columns) == ["a", "c"]


def test_resolve_processing_workbook_fallback():
    job = {"materialized_clean_templates": {}}
    scoped = {"sheet_name": "Raw", "main_blocks": []}
    path, sheet, sc, used = resolve_processing_workbook(
        job,
        "src1",
        "/tmp/original.xlsx",
        "Raw",
        scoped,
    )
    assert path == "/tmp/original.xlsx"
    assert sheet == "Raw"
    assert sc == scoped
    assert used is False


def test_scoped_source_for_materialized_workbook_full_sheet():
    sc = scoped_source_for_materialized_workbook("Clean", 10, 4)
    assert sc.get("sheet_name") == "Clean"
    assert sc.get("requires_extraction") is False
