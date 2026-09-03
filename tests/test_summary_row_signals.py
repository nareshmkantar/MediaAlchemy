"""Summary-row signal detection for conditional filter_summaries."""
from sia.agent.summary_row_signals import sheet_likely_has_summary_rows


def test_sheet_likely_has_summary_rows_from_structure_label():
    assert sheet_likely_has_summary_rows(
        {"tables": [{"label": "Includes Subtotal row under TV block"}]},
        {},
        try_dataframe_preview=False,
    )


def test_sheet_likely_has_summary_rows_false_for_clean_flat_table():
    assert not sheet_likely_has_summary_rows(
        {
            "tables": [{"label": "Digital performance block", "table_shape": "flat"}],
            "reasoning": "Flat table with campaign rows only.",
        },
        {},
        try_dataframe_preview=False,
    )
