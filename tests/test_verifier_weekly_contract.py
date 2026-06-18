"""Tests for deterministic weekly rollup contract in output verifier."""

import pandas as pd

from sia.agent.verifier import deterministic_weekly_contract_issues


def test_weekly_contract_when_rollup_required_but_no_tools():
    align = {
        "normalized_target": "weekly",
        "requires_weekly_rollup_step": True,
    }
    issues = deterministic_weekly_contract_issues(
        align,
        [{"tool": "transform.rename", "success": True}],
    )
    assert len(issues) == 1
    assert issues[0]["issue_type"] == "granularity_mismatch"


def test_weekly_contract_passes_when_aggregate_weekly_ran():
    align = {
        "normalized_target": "weekly",
        "requires_weekly_rollup_step": True,
    }
    assert (
        deterministic_weekly_contract_issues(
            align,
            [{"tool": "layout.extract"}, {"tool": "transform.aggregate_weekly"}],
        )
        == []
    )


def test_weekly_contract_passes_when_infer_granularity_expand_ran():
    align = {
        "normalized_target": "weekly",
        "requires_weekly_rollup_step": True,
    }
    assert (
        deterministic_weekly_contract_issues(
            align,
            [{"tool": "transform.infer_granularity_expand_to_daily"}],
        )
        == []
    )


def test_weekly_contract_passes_when_output_df_is_weekly_cadence():
    """No rollup tools in history, but output dates are ~weekly — do not flag."""
    align = {
        "normalized_target": "weekly",
        "requires_weekly_rollup_step": True,
    }
    idx = pd.date_range("2024-01-01", periods=12, freq="7D")
    df = pd.DataFrame({"calendar_date": idx, "metric": 1.0})
    assert deterministic_weekly_contract_issues(align, [], output_df=df) == []


def test_weekly_contract_ignored_when_target_not_weekly():
    align = {
        "normalized_target": "daily",
        "requires_weekly_rollup_step": True,
    }
    assert deterministic_weekly_contract_issues(align, []) == []


def test_merge_weekly_contract_skipped_when_deferred_flag():
    """Per-source multi-union runs skip deterministic weekly extras when flag is set."""
    from unittest.mock import MagicMock

    from sia.agent.base import VerificationResult
    from sia.agent.verifier import OutputVerifier

    v = OutputVerifier(MagicMock())
    base = VerificationResult(is_flat=True, confidence=0.9, issues=[], summary="ok")
    align = {"normalized_target": "weekly", "requires_weekly_rollup_step": True}
    merged = v._merge_weekly_contract(
        base,
        align,
        [{"tool": "transform.rename"}],
        None,
        skip_weekly_grain_contract=True,
    )
    assert merged.is_flat is True
    assert merged.issues == []
