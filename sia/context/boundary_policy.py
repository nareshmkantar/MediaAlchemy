"""Context propagation boundary rules (workbook → sheet → block → field)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Tuple

from sia.context.scoped_field import CONTEXT_FIELD_SCOPES

# Fields a relationship may enrich from a peer source (not local dimensions).
RELATIONSHIP_ENRICHABLE_FIELDS = frozenset(
    {
        "campaign_group",
        "campaign",
        "publisher",
        "owner",
        "brand",
        "segment",
        "product",
    }
)

_SCOPE_RANK = {
    "workbook": 0,
    "sheet": 1,
    "block": 2,
    "interpreted": 3,
    "cross_sheet": 4,
}

_DOWNWARD_ALLOW: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("workbook", "sheet"),
        ("workbook", "block"),
        ("workbook", "interpreted"),
        ("sheet", "block"),
        ("sheet", "interpreted"),
        ("block", "interpreted"),
    }
)


class BoundaryVerdict(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    REJECT = "reject"


@dataclass(frozen=True)
class BoundaryDecision:
    verdict: BoundaryVerdict
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict != BoundaryVerdict.REJECT


def _norm_scope(scope: str) -> str:
    s = str(scope or "interpreted").strip().lower()
    return s if s in CONTEXT_FIELD_SCOPES else "interpreted"


class ContextBoundaryPolicy:
    """Enforce the Allow / warn / reject table for scope propagation."""

    def __init__(self, *, max_cross_sheet_hops: int = 1) -> None:
        self.max_cross_sheet_hops = max(0, int(max_cross_sheet_hops))

    def may_propagate(
        self,
        from_scope: str,
        to_scope: str,
        *,
        relationship: bool = False,
        evidence: bool = False,
    ) -> BoundaryDecision:
        fs = _norm_scope(from_scope)
        ts = _norm_scope(to_scope)

        if fs == ts:
            return BoundaryDecision(BoundaryVerdict.ALLOW, "same scope refresh")

        if fs == "cross_sheet" or ts == "cross_sheet":
            if relationship and evidence:
                return BoundaryDecision(BoundaryVerdict.ALLOW, "cross-sheet with relationship and evidence")
            if relationship:
                return BoundaryDecision(
                    BoundaryVerdict.WARN,
                    "cross-sheet with relationship but no evidence line",
                )
            return BoundaryDecision(
                BoundaryVerdict.REJECT,
                "cross-sheet propagation requires an approved relationship",
            )

        if (fs, ts) in _DOWNWARD_ALLOW:
            if fs == "block" and ts == "interpreted" and not evidence:
                return BoundaryDecision(
                    BoundaryVerdict.WARN,
                    "block → field without evidence line",
                )
            return BoundaryDecision(BoundaryVerdict.ALLOW, f"{fs} → {ts} downward")

        if _SCOPE_RANK.get(fs, 0) > _SCOPE_RANK.get(ts, 0):
            return BoundaryDecision(
                BoundaryVerdict.REJECT,
                f"upward propagation {fs} → {ts} is not allowed",
            )

        return BoundaryDecision(
            BoundaryVerdict.REJECT,
            f"propagation {fs} → {ts} is not in the boundary policy",
        )

    def allows_hop(self, hop: int, *, cross_sheet: bool = False) -> BoundaryDecision:
        limit = self.max_cross_sheet_hops if cross_sheet else 0
        if cross_sheet and hop > limit:
            return BoundaryDecision(
                BoundaryVerdict.REJECT,
                f"hop {hop} exceeds cross-sheet limit {limit}",
            )
        return BoundaryDecision(BoundaryVerdict.ALLOW, "hop within limit")
