"""
Derived field context propagation (Phase 8.4 — spec until DerivedFieldPlan ships).

When cross-source derived columns land (see ``ARCHITECTURE.md`` §9), propagation
must require:

- ``join_context`` on the :class:`~sia.agent.cross_source.DerivedFieldPlan`
- ``approved_by`` / ``status == approved`` on the plan record
- Field scope ``interpreted`` or narrower — never workbook-level dimension bleed

Implementation hooks (future):

1. ``propagate_field(..., relationship=True)`` only for columns listed in the plan.
2. ``ContextBoundaryPolicy.may_propagate`` rejects derived dimensions without approval.
3. HITL checkpoint ``DERIVED_FIELD_REVIEW`` before stamping derived literals.

This module documents the contract; runtime enforcement is not wired yet.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet

DERIVED_CONTEXT_REQUIRED_KEYS: FrozenSet[str] = frozenset(
    {"join_context", "approved_by", "target_source_id", "target_column"}
)


def derived_plan_allows_context_stamp(plan: Dict[str, Any]) -> bool:
    """True when a derived-field plan is approved and has join context."""
    if not isinstance(plan, dict):
        return False
    if str(plan.get("status") or "").strip().lower() not in ("approved", "active"):
        return False
    if not str(plan.get("approved_by") or "").strip():
        return False
    join_ctx = plan.get("join_context")
    return isinstance(join_ctx, dict) and bool(join_ctx)
