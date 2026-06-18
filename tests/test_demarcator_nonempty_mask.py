"""Parity tests for StructureDemarcator._build_non_empty_mask (vectorized vs legacy semantics)."""

import numpy as np
import pandas as pd
import pytest

from sia.agent.demarcator import StructureDemarcator


def _legacy_nonempty_mask(grid_df: pd.DataFrame) -> list:
    if grid_df.empty:
        return []
    mask = []
    for row in grid_df.itertuples(index=False, name=None):
        mask_row = []
        for value in row:
            if value is None:
                mask_row.append(False)
            elif isinstance(value, str):
                mask_row.append(bool(value.strip()))
            else:
                mask_row.append(bool(pd.notna(value)))
        mask.append(mask_row)
    return mask


@pytest.fixture
def dem() -> StructureDemarcator:
    return StructureDemarcator(llm_client=None)


def test_nonempty_mask_parity_mixed_object_frame(dem: StructureDemarcator):
    df = pd.DataFrame(
        [
            ["  ", None, 1, np.nan],
            [None, "ok", pd.NA, 0.0],
            ["", pd.NaT, "  x  ", False],
        ]
    )
    assert dem._build_non_empty_mask(df) == _legacy_nonempty_mask(df)


def test_nonempty_mask_parity_numeric_and_datetime(dem: StructureDemarcator):
    df = pd.DataFrame(
        {
            "f": [1.0, np.nan, 0.0],
            "i": pd.array([1, pd.NA, 3], dtype="Int64"),
            "d": pd.to_datetime(["2020-01-01", pd.NaT, "2020-01-03"]),
        }
    )
    assert dem._build_non_empty_mask(df) == _legacy_nonempty_mask(df)


def test_nonempty_mask_parity_string_dtype(dem: StructureDemarcator):
    df = pd.DataFrame({"s": pd.array([" a ", None, "", "x"], dtype="string")})
    assert dem._build_non_empty_mask(df) == _legacy_nonempty_mask(df)


def test_nonempty_mask_parity_bool_column(dem: StructureDemarcator):
    df = pd.DataFrame({"b": [True, False, np.nan]})
    assert dem._build_non_empty_mask(df) == _legacy_nonempty_mask(df)


def test_nonempty_mask_parity_categorical(dem: StructureDemarcator):
    df = pd.DataFrame({"c": pd.Categorical(["a", None, "  ", "b"])})
    assert dem._build_non_empty_mask(df) == _legacy_nonempty_mask(df)


def test_nonempty_mask_parity_wider_random_object(dem: StructureDemarcator):
    rng = np.random.default_rng(42)
    rows, cols = 120, 35
    data = []
    for _ in range(rows):
        row = []
        for _ in range(cols):
            k = rng.integers(0, 6)
            if k == 0:
                row.append(None)
            elif k == 1:
                row.append("")
            elif k == 2:
                row.append("   ")
            elif k == 3:
                row.append(rng.choice([" hi ", "x", ""]))
            elif k == 4:
                row.append(float(rng.integers(1, 1000)))
            else:
                row.append(np.nan)
        data.append(row)
    df = pd.DataFrame(data)
    assert dem._build_non_empty_mask(df) == _legacy_nonempty_mask(df)
