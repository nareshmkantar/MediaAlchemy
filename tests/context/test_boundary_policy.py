"""Phase 3.1: ContextBoundaryPolicy unit tests."""

from __future__ import annotations

import pytest

from sia.context.boundary_policy import BoundaryVerdict, ContextBoundaryPolicy


@pytest.fixture
def policy() -> ContextBoundaryPolicy:
    return ContextBoundaryPolicy(max_cross_sheet_hops=1)


@pytest.mark.parametrize(
    "from_scope,to_scope,relationship,evidence,expected",
    [
        ("workbook", "sheet", False, False, BoundaryVerdict.ALLOW),
        ("sheet", "block", False, False, BoundaryVerdict.ALLOW),
        ("block", "interpreted", False, False, BoundaryVerdict.WARN),
        ("block", "interpreted", False, True, BoundaryVerdict.ALLOW),
        ("interpreted", "sheet", False, False, BoundaryVerdict.REJECT),
        ("block", "workbook", False, False, BoundaryVerdict.REJECT),
        ("cross_sheet", "sheet", False, False, BoundaryVerdict.REJECT),
        ("cross_sheet", "sheet", True, False, BoundaryVerdict.WARN),
        ("cross_sheet", "sheet", True, True, BoundaryVerdict.ALLOW),
    ],
)
def test_may_propagate_matrix(policy, from_scope, to_scope, relationship, evidence, expected):
    decision = policy.may_propagate(
        from_scope,
        to_scope,
        relationship=relationship,
        evidence=evidence,
    )
    assert decision.verdict == expected


def test_allows_hop_cross_sheet_limit(policy):
    ok = policy.allows_hop(1, cross_sheet=True)
    assert ok.verdict == BoundaryVerdict.ALLOW
    blocked = policy.allows_hop(2, cross_sheet=True)
    assert blocked.verdict == BoundaryVerdict.REJECT
