"""User demarcation decisions must override the AI-proposed block category.

Regression for CP-12: clicking "Use as Metadata" / "Ignore" only flips a block's
``decision``; the original ``category`` stays "Main Data". Classification must
honour the explicit decision, not the stale AI category.
"""

from sia.agent.job_manager import _layout_row_is_kept_main_data
from sia.agent.scoped_source import _classify_blocks, build_scoped_source


def _block(category, decision):
    return {
        "id": "block_1",
        "category": category,
        "decision": decision,
        "coordinates": {
            "start_row": 0,
            "end_row": 2,
            "start_col": 0,
            "end_col": 3,
            "header_row": 0,
        },
    }


def test_use_as_metadata_overrides_main_data_category():
    """Decision='Context' on an AI 'Main Data' block -> context, not main."""
    main, context = _classify_blocks([_block("Main Data", "Context")])
    assert main == []
    assert len(context) == 1


def test_ignore_excludes_main_data_block():
    """Decision='Discard' on an AI 'Main Data' block -> dropped from both lists."""
    main, context = _classify_blocks([_block("Main Data", "Discard")])
    assert main == []
    assert context == []


def test_keep_decision_stays_main_data():
    main, context = _classify_blocks([_block("Main Data", "Keep")])
    assert len(main) == 1
    assert context == []


def test_category_used_only_without_decision():
    """No explicit decision -> fall back to the AI category."""
    main, context = _classify_blocks([_block("Main Data", "")])
    assert len(main) == 1
    main2, context2 = _classify_blocks([_block("Metadata", "")])
    assert main2 == []
    assert len(context2) == 1


def test_metadata_override_removes_extraction_scope():
    """A metadata-only sheet must not be treated as an extractable data table."""
    scoped = build_scoped_source("Validation_Rules", blocks=[_block("Main Data", "Context")])
    assert scoped["requires_extraction"] is False
    assert scoped["scope_type"] == "full_sheet"
    assert scoped["main_blocks"] == []
    assert len(scoped["context_blocks"]) == 1


def _reg_row(block_category, decision):
    return {"block_category": block_category, "decision": decision}


def test_gating_promotes_context_block_to_main_data():
    """User 'Treat as Data' on an AI Context/Noise block must count as main data
    so the pipeline routes to column mapping instead of skipping it."""
    assert _layout_row_is_kept_main_data(_reg_row("Context", "keep")) is True
    assert _layout_row_is_kept_main_data(_reg_row("Noise", "keep")) is True


def test_gating_excludes_metadata_and_ignore():
    assert _layout_row_is_kept_main_data(_reg_row("Main Data", "context")) is False
    assert _layout_row_is_kept_main_data(_reg_row("Main Data", "discard")) is False


def test_gating_category_fallback_without_decision():
    assert _layout_row_is_kept_main_data(_reg_row("Main Data", "")) is True
    assert _layout_row_is_kept_main_data(_reg_row("Context", "")) is False
