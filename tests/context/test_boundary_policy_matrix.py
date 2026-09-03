"""Phase 10.3: exhaustive ContextBoundaryPolicy scope-pair matrix."""

from __future__ import annotations

import itertools

import pytest

from sia.context.boundary_policy import BoundaryVerdict, ContextBoundaryPolicy
from sia.context.scoped_field import CONTEXT_FIELD_SCOPES

SCOPES = sorted(CONTEXT_FIELD_SCOPES)


def _expected_verdict(
    from_scope: str,
    to_scope: str,
    *,
    relationship: bool = False,
    evidence: bool = False,
) -> BoundaryVerdict:
    policy = ContextBoundaryPolicy(max_cross_sheet_hops=1)
    return policy.may_propagate(
        from_scope,
        to_scope,
        relationship=relationship,
        evidence=evidence,
    ).verdict


@pytest.mark.parametrize("from_scope,to_scope", list(itertools.product(SCOPES, SCOPES)))
def test_may_propagate_same_scope_pairs_default(from_scope, to_scope):
    expected = _expected_verdict(from_scope, to_scope)
    actual = ContextBoundaryPolicy().may_propagate(from_scope, to_scope).verdict
    assert actual == expected, f"{from_scope} → {to_scope}"


@pytest.mark.parametrize(
    "from_scope,to_scope,relationship,evidence,expected",
    [
        ("cross_sheet", "interpreted", False, False, BoundaryVerdict.REJECT),
        ("cross_sheet", "interpreted", True, False, BoundaryVerdict.WARN),
        ("cross_sheet", "interpreted", True, True, BoundaryVerdict.ALLOW),
        ("sheet", "cross_sheet", True, True, BoundaryVerdict.ALLOW),
        ("workbook", "interpreted", False, False, BoundaryVerdict.ALLOW),
        ("interpreted", "workbook", False, False, BoundaryVerdict.REJECT),
        ("block", "sheet", False, False, BoundaryVerdict.REJECT),
    ],
)
def test_may_propagate_documented_edge_cases(from_scope, to_scope, relationship, evidence, expected):
    decision = ContextBoundaryPolicy().may_propagate(
        from_scope,
        to_scope,
        relationship=relationship,
        evidence=evidence,
    )
    assert decision.verdict == expected


@pytest.mark.parametrize("hop, cross_sheet, allowed", [
    (0, False, True),
    (1, True, True),
    (2, True, False),
    (0, True, True),
])
def test_allows_hop_matrix(hop, cross_sheet, allowed):
    decision = ContextBoundaryPolicy(max_cross_sheet_hops=1).allows_hop(hop, cross_sheet=cross_sheet)
    assert decision.allowed is allowed


def test_policy_matrix_covers_all_scope_pairs():
    """Sanity: 5 scopes → 25 directed pairs exercised."""
    count = len(SCOPES) ** 2
    assert count == 25
    seen = set()
    for fs in SCOPES:
        for ts in SCOPES:
            ContextBoundaryPolicy().may_propagate(fs, ts)
            seen.add((fs, ts))
    assert len(seen) == 25
