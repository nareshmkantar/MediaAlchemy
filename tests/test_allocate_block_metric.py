"""Block-level metric allocation before weekly SUM rollups."""
import numpy as np
import pandas as pd

from sia.tools.transformation_tools import TransformationTools


def test_allocate_equal_split_single_block():
    df = pd.DataFrame(
        {
            "Name": ["Alice", np.nan, np.nan, np.nan],
            "spends": [400.0, np.nan, np.nan, np.nan],
            "impressions": [100.0, 200.0, 300.0, 400.0],
        }
    )
    r = TransformationTools.allocate_block_metric(
        df, metric_col="spends", block_start_columns=["Name"], method="equal"
    )
    assert r.success, r.message
    assert abs(float(r.data["spends"].sum()) - 400.0) < 1e-9
    assert all(abs(v - 100.0) < 1e-9 for v in r.data["spends"])


def test_allocate_two_blocks_and_orphan_skipped_totals():
    df = pd.DataFrame(
        {
            "Ref": ["R1", np.nan, "R2", np.nan],
            "spends": [10.0, np.nan, 30.0, np.nan],
        }
    )
    r = TransformationTools.allocate_block_metric(df, "spends", block_start_columns=["Ref"])
    assert r.success, r.message
    assert len(r.data) == 4
    assert abs(float(r.data.loc[0, "spends"]) - 5.0) < 1e-9
    assert abs(float(r.data.loc[1, "spends"]) - 5.0) < 1e-9
    assert abs(float(r.data.loc[2, "spends"]) - 15.0) < 1e-9
    assert abs(float(r.data.loc[3, "spends"]) - 15.0) < 1e-9


def test_allocate_by_weight_matches_impressions_share():
    df = pd.DataFrame(
        {
            "Name": ["X", np.nan, np.nan],
            "spends": [100.0, np.nan, np.nan],
            "impressions": [10.0, 30.0, 60.0],
        }
    )
    r = TransformationTools.allocate_block_metric(
        df,
        metric_col="spends",
        block_start_columns=["Name"],
        method="by_weight",
        weight_col="impressions",
    )
    assert r.success, r.message
    vals = list(r.data["spends"])
    assert abs(vals[0] - 10.0) < 1e-9  # 10% of 100
    assert abs(vals[1] - 30.0) < 1e-9
    assert abs(vals[2] - 60.0) < 1e-9
