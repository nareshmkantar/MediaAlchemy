"""Confidence-aware merge of scoped context fields (Phase 7.2)."""

from __future__ import annotations

from typing import Dict, Iterable, Mapping

from sia.context.scoped_field import ScopedField, merge_scoped_field


def merge_scoped_fields(
    base: Mapping[str, ScopedField],
    overlay: Iterable[ScopedField],
) -> Dict[str, ScopedField]:
    """Merge overlay candidates into base; higher confidence wins, then narrower scope."""
    out: Dict[str, ScopedField] = dict(base)
    for candidate in overlay:
        if isinstance(candidate, ScopedField):
            merge_scoped_field(out, candidate)
    return out


__all__ = ["merge_scoped_field", "merge_scoped_fields", "ScopedField"]
