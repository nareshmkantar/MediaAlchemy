"""Confidence tiers for scoped context fields (Phase 7)."""

from __future__ import annotations

CONFIDENCE_BLOCK_PARSE = 1.0
CONFIDENCE_TAB_INFERENCE = 0.7
CONFIDENCE_PLANNER_ASSUMPTION = 0.5
CONFIDENCE_CROSS_SHEET = 0.3
CONFIDENCE_CROSS_SHEET_APPROVED = 0.6

MIN_CONFIDENCE_TO_STAMP = 0.6
# Tab-name inference is a soft hint for planner context; finalize stamp only when
# confidence meets this bar (tab inference itself stays at CONFIDENCE_TAB_INFERENCE).
MIN_CONFIDENCE_TAB_TO_STAMP = 0.95

MAX_CONTEXT_HOPS = 1
