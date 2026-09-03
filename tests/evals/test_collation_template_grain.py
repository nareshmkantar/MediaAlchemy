"""Template-aware union false-duplicate detection."""

from __future__ import annotations

import pandas as pd

from sia.evals.collation_sanity import (
    evaluate_union_false_duplicates,
    resolve_collation_fingerprint_columns,
)


_SALES_TEMPLATE = {
    "x_scope": {
        "uid_hierarchy": ["date", "channel", "market"],
        "supporting_columns": ["product", "category"],
        "metrics": ["revenue"],
    },
    "properties": {
        "date": {"type": "string", "format": "date"},
        "channel": {"type": "string"},
        "market": {"type": "string"},
        "product": {"type": "string"},
        "category": {"type": "string"},
        "revenue": {"type": "number"},
    },
}


def test_resolve_collation_fingerprint_columns_from_template():
    keys, measures = resolve_collation_fingerprint_columns(_SALES_TEMPLATE)
    assert keys == ["date", "channel", "market", "product", "category"]
    assert measures == ["revenue"]


def test_blank_dates_do_not_false_duplicate_when_product_differs():
    """Regression: PMI default keys flagged Jan/Feb bleed on blank dates alone."""
    jan = pd.DataFrame(
        [
            {"date": "", "channel": "retail", "market": "US", "product": "A", "category": "x", "revenue": 10},
            {"date": "", "channel": "retail", "market": "US", "product": "B", "category": "y", "revenue": 20},
        ]
    )
    feb = pd.DataFrame(
        [
            {"date": "", "channel": "retail", "market": "US", "product": "C", "category": "z", "revenue": 30},
        ]
    )
    with_template = evaluate_union_false_duplicates(
        {"jan": jan, "feb": feb},
        target_template=_SALES_TEMPLATE,
    )
    assert with_template["metrics"]["union_false_duplicates"] == 0

    without_template = evaluate_union_false_duplicates({"jan": jan, "feb": feb})
    assert without_template["metrics"]["union_false_duplicates"] >= 1


def test_true_false_duplicate_when_full_grain_and_metrics_match():
    jan = pd.DataFrame(
        [
            {"date": "", "channel": "retail", "market": "US", "product": "A", "category": "x", "revenue": 10},
        ]
    )
    feb = pd.DataFrame(
        [
            {"date": "", "channel": "retail", "market": "US", "product": "A", "category": "x", "revenue": 10},
        ]
    )
    out = evaluate_union_false_duplicates(
        {"jan": jan, "feb": feb},
        target_template=_SALES_TEMPLATE,
    )
    assert out["metrics"]["union_false_duplicates"] >= 1
