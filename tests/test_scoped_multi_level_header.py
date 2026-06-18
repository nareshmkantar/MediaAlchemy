import pandas as pd

from sia.agent.scoped_source import (
    _is_probable_subheader_row,
    load_scoped_dataframe,
)


def test_probable_subheader_rejects_row_with_any_numeric_cell():
    width = 6
    text_only = pd.Series(["a", "b", "c", "d", "e", "f"])
    assert _is_probable_subheader_row(text_only, width) is True
    mixed = pd.Series(["Meta", "Social", "Video", "AU", "18.57", "1073"])
    assert _is_probable_subheader_row(mixed, width) is False


def test_load_scoped_merges_two_row_headers_with_underscore():
    raw = pd.DataFrame(
        [
            ["ParentA", "", "ParentB"],
            ["ChildA", "ChildB", "ChildC"],
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
        ]
    )
    scoped = {
        "main_blocks": [
            {
                "coordinates": {
                    "start_row": 0,
                    "end_row": 3,
                    "start_col": 0,
                    "end_col": 2,
                    "header_row": 0,
                },
                "decision": "Keep",
                "category": "main_data",
            }
        ],
        "header_row": 0,
        "sheet_name": "Sheet1",
    }
    df, scope = load_scoped_dataframe("unused.xlsx", "Sheet1", scoped, raw_df=raw)

    # Row 0 forward-filled: ParentA, ParentA, ParentB — then merged with row 1
    assert list(df.columns) == ["ParentA_ChildA", "ParentA_ChildB", "ParentB_ChildC"]
    assert len(df) == 2
    assert scope["header_derivation"]["derived"] is True
    assert scope["header_derivation"]["separator"] == "_"
    dmap = scope["header_derivation"].get("derived_by_column") or {}
    assert all(dmap.get(c) for c in df.columns)
    assert df.iloc[0].tolist() == [1.0, 2.0, 3.0]


def test_two_row_header_blank_sub_row_not_derived():
    """When the sub-header cell is blank, only the parent label is used — not marked derived."""
    raw = pd.DataFrame(
        [
            ["X", "Y", "Z"],
            ["a", "", "c"],
            [1.0, 2.0, 3.0],
        ]
    )
    scoped = {
        "main_blocks": [
            {
                "coordinates": {
                    "start_row": 0,
                    "end_row": 2,
                    "start_col": 0,
                    "end_col": 2,
                    "header_row": 0,
                    "header_row_end": 1,
                    "header_mode": "multi",
                },
                "decision": "Keep",
                "category": "main_data",
            }
        ],
    }
    df, scope = load_scoped_dataframe("unused.xlsx", "Sheet1", scoped, raw_df=raw)
    assert list(df.columns) == ["X_a", "Y", "Z_c"]
    dmap = scope["header_derivation"].get("derived_by_column") or {}
    assert dmap.get("X_a") is True
    assert dmap.get("Y") is False
    assert dmap.get("Z_c") is True
    assert scope["header_derivation"]["derived"] is True


def test_load_scoped_single_row_when_subrow_is_numeric():
    raw = pd.DataFrame(
        [
            ["A", "B", "C"],
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
        ]
    )
    scoped = {
        "main_blocks": [
            {
                "coordinates": {
                    "start_row": 0,
                    "end_row": 2,
                    "start_col": 0,
                    "end_col": 2,
                    "header_row": 0,
                },
                "decision": "Keep",
                "category": "main_data",
            }
        ],
        "header_row": 0,
    }
    df, scope = load_scoped_dataframe("unused.xlsx", "Sheet1", scoped, raw_df=raw)

    assert list(df.columns) == ["A", "B", "C"]
    assert scope["header_derivation"]["derived"] is False
    assert len(df) == 2


def test_load_scoped_user_multi_header_overrides_heuristic():
    """Explicit multi-header range must merge even when row 2 looks numeric (heuristic would skip)."""
    raw = pd.DataFrame(
        [
            ["A", "B"],
            ["C", "D"],
            [1.0, 2.0],
            [3.0, 4.0],
        ]
    )
    scoped = {
        "main_blocks": [
            {
                "coordinates": {
                    "start_row": 0,
                    "end_row": 3,
                    "start_col": 0,
                    "end_col": 1,
                    "header_row": 0,
                    "header_row_end": 1,
                    "header_mode": "multi",
                },
                "decision": "Keep",
                "category": "main_data",
            }
        ],
    }
    df, scope = load_scoped_dataframe("unused.xlsx", "Sheet1", scoped, raw_df=raw)

    assert list(df.columns) == ["A_C", "B_D"]
    assert len(df) == 2
    assert scope["header_derivation"]["derived"] is True
    assert scope["header_derivation"]["format"] == "user_multi_row"
    assert scope["header_derivation"].get("user_selected") is True
    dmap = scope["header_derivation"].get("derived_by_column") or {}
    assert all(dmap.get(c) for c in df.columns)
