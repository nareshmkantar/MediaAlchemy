"""Phase 3.2: scoped field propagation."""

from __future__ import annotations

from sia.context.boundary_policy import ContextBoundaryPolicy
from sia.context.propagation import propagate_field
from sia.context.scoped_field import ScopedField


def test_cross_sheet_without_relationship_rejected():
    target: dict = {}
    candidate = ScopedField(
        name="market",
        value="UK",
        scope="cross_sheet",
        source_id="peer",
        sheet_name="Digital_UK",
        hop=0,
    )
    result = propagate_field(
        target,
        candidate,
        policy=ContextBoundaryPolicy(),
        active_source_id="s-radio",
        relationship=False,
    )
    assert result.accepted is False
    assert "market" not in target


def test_cross_sheet_with_relationship_accepted_increments_hop():
    target: dict = {}
    candidate = ScopedField(
        name="campaign_group",
        value="Brand A",
        scope="cross_sheet",
        source_id="peer",
        evidence_line="join:Digital_UK",
        hop=0,
    )
    result = propagate_field(
        target,
        candidate,
        policy=ContextBoundaryPolicy(max_cross_sheet_hops=1),
        active_source_id="s-radio",
        relationship=True,
    )
    assert result.accepted is True
    assert target["campaign_group"].hop == 1


def test_foreign_block_scope_rejected():
    target: dict = {}
    candidate = ScopedField(
        name="market",
        value="UK",
        scope="block",
        source_id="digital-uk",
        sheet_name="Digital_UK",
    )
    result = propagate_field(target, candidate, active_source_id="radio-de")
    assert result.accepted is False
