"""Phase 7–8: confidence, stamp policy, propagation hop limits."""

from __future__ import annotations

from sia.context.boundary_policy import ContextBoundaryPolicy
from sia.context.confidence import CONFIDENCE_BLOCK_PARSE, MIN_CONFIDENCE_TO_STAMP
from sia.context.merge import merge_scoped_fields
from sia.context.propagation import propagate_field
from sia.context.scoped_field import ScopedField
from sia.context.stamp_policy import (
    evaluate_low_confidence_context,
    stampable_local_fields,
)
from sia.evals.context_propagation import evaluate_context_hop_events
from sia.integrity.context_isolation import build_scoped_fields


def test_merge_scoped_fields_germany_beats_uk_planner():
    base = {
        "market": ScopedField(
            name="market",
            value="UK",
            scope="interpreted",
            confidence=0.5,
        ),
    }
    merged = merge_scoped_fields(
        base,
        [
            ScopedField(
                name="market",
                value="Germany",
                scope="block",
                confidence=CONFIDENCE_BLOCK_PARSE,
            ),
        ],
    )
    assert merged["market"].value == "Germany"
    assert merged["market"].scope == "block"


def test_tab_inference_does_not_override_block_market():
    scoped = build_scoped_fields(
        {"fields": {}, "evidence": []},
        [{"block_label": "Meta", "text_preview": ["Market: Germany"]}],
        "Radio_DE",
        source_id="s-radio",
    )
    assert scoped["market"].value == "Germany"
    assert scoped["market"].confidence >= MIN_CONFIDENCE_TO_STAMP
    assert scoped["market"].scope == "block"


def test_stampable_local_fields_skips_low_confidence_tab():
    cp = {
        "interpreted_context": {
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "DE",
                    "scope": "sheet",
                    "confidence": 0.7,
                    "hop": 1,
                    "evidence_line": "tab:Radio_DE",
                },
            },
        },
    }
    job = {
        "source_registry": [
            {"source_id": "a", "sheet_name": "Radio_DE"},
            {"source_id": "b", "sheet_name": "Digital_UK"},
        ],
    }
    assert stampable_local_fields(cp) == {}
    violations = evaluate_low_confidence_context(cp, job)
    assert any(v["type"] == "low_confidence_context" for v in violations)


def test_cross_sheet_market_blocked_even_with_relationship():
    target: dict = {}
    candidate = ScopedField(
        name="market",
        value="UK",
        scope="cross_sheet",
        source_id="peer",
        sheet_name="Digital_UK",
        evidence_line="join:Digital_UK",
        hop=0,
    )
    result = propagate_field(
        target,
        candidate,
        policy=ContextBoundaryPolicy(max_cross_sheet_hops=1),
        active_source_id="s-radio",
        relationship=True,
        job={},
    )
    assert result.accepted is False
    assert "market" not in target


def test_context_hop_exceeded_from_diff_log():
    job = {
        "context_diff_log": [
            {
                "source_id": "s1",
                "field": "market",
                "hop": 2,
                "reason": "hop 2 exceeds cross-sheet limit 1",
                "meta": {"rejected": True},
            }
        ],
    }
    result = evaluate_context_hop_events(job, source_id="s1")
    assert result["pass"] is False
    assert any(v["type"] == "context_hop_exceeded" for v in result["violations"])
