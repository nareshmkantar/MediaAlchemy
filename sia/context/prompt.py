"""Planner/debug formatting for scoped context fields."""

from __future__ import annotations

from typing import Any, Dict, List

from sia.context.scoped_field import ScopedField


def format_scoped_fields_for_planner(interpreted_context: Optional[Dict[str, Any]]) -> List[str]:
    """Human-readable ``field=value (provenance)`` lines for the planner prompt."""
    ic = dict(interpreted_context or {})
    scoped = ic.get("scoped_fields") if isinstance(ic.get("scoped_fields"), dict) else {}
    lines: List[str] = []
    for name, row in scoped.items():
        if not isinstance(row, dict):
            continue
        sf = ScopedField.from_dict({**row, "name": str(name)})
        if not sf.value:
            continue
        lines.append(f"{sf.name}={sf.value!r} ({sf.provenance_summary()})")
    return lines
