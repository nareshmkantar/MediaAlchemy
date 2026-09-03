"""Scoped field model: value + provenance for source-local context."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional

CONTEXT_FIELD_SCOPES = frozenset({"workbook", "sheet", "block", "interpreted", "cross_sheet"})

_SCOPE_PRECEDENCE = {
    "block": 4,
    "interpreted": 3,
    "sheet": 2,
    "workbook": 1,
    "cross_sheet": 0,
}


@dataclass
class ScopedField:
    """One context literal with scope, provenance, and confidence."""

    name: str
    value: str
    scope: str
    source_id: str = ""
    sheet_name: str = ""
    block_id: Optional[str] = None
    block_label: Optional[str] = None
    evidence_line: Optional[str] = None
    confidence: float = 1.0
    hop: int = 0

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        self.value = str(self.value or "").strip()
        scope = str(self.scope or "interpreted").strip().lower()
        self.scope = scope if scope in CONTEXT_FIELD_SCOPES else "interpreted"
        self.source_id = str(self.source_id or "").strip()
        self.sheet_name = str(self.sheet_name or "").strip()
        if self.block_id is not None:
            self.block_id = str(self.block_id).strip() or None
        if self.block_label is not None:
            self.block_label = str(self.block_label).strip() or None
        if self.evidence_line is not None:
            self.evidence_line = str(self.evidence_line).strip() or None
        try:
            self.confidence = float(self.confidence)
        except (TypeError, ValueError):
            self.confidence = 1.0
        try:
            self.hop = int(self.hop)
        except (TypeError, ValueError):
            self.hop = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScopedField":
        row = dict(data or {})
        return cls(
            name=str(row.get("name") or row.get("field") or ""),
            value=str(row.get("value") or ""),
            scope=str(row.get("scope") or "interpreted"),
            source_id=str(row.get("source_id") or ""),
            sheet_name=str(row.get("sheet_name") or ""),
            block_id=row.get("block_id"),
            block_label=row.get("block_label"),
            evidence_line=row.get("evidence_line") or row.get("line"),
            confidence=row.get("confidence", 1.0),
            hop=row.get("hop", 0),
        )

    def provenance_summary(self) -> str:
        """One-line provenance for debug / planner prompts."""
        parts = [self.scope]
        if self.block_label:
            parts.append(self.block_label)
        elif self.sheet_name and self.scope == "sheet":
            parts.append(self.sheet_name)
        if self.evidence_line:
            parts.append(self.evidence_line[:80])
        return " · ".join(p for p in parts if p)


def merge_scoped_field(
    fields: Dict[str, ScopedField],
    candidate: ScopedField,
) -> None:
    """Keep the candidate when it has higher confidence or narrower scope."""
    if not candidate.name or not candidate.value:
        return
    existing = fields.get(candidate.name)
    if existing is None:
        fields[candidate.name] = candidate
        return
    if candidate.confidence > existing.confidence:
        fields[candidate.name] = candidate
        return
    if candidate.confidence < existing.confidence:
        return
    cand_rank = _SCOPE_PRECEDENCE.get(candidate.scope, 0)
    exist_rank = _SCOPE_PRECEDENCE.get(existing.scope, 0)
    if cand_rank > exist_rank:
        fields[candidate.name] = candidate


def scoped_fields_to_flat(fields: Mapping[str, ScopedField]) -> Dict[str, str]:
    return {name: sf.value for name, sf in fields.items() if sf.value}


def scoped_fields_to_dict(fields: Mapping[str, ScopedField]) -> Dict[str, Dict[str, Any]]:
    return {name: sf.to_dict() for name, sf in fields.items()}


def scoped_fields_from_dict(data: Optional[Mapping[str, Any]]) -> Dict[str, ScopedField]:
    out: Dict[str, ScopedField] = {}
    if not isinstance(data, Mapping):
        return out
    for key, row in data.items():
        if isinstance(row, ScopedField):
            out[str(key)] = row
            continue
        if not isinstance(row, dict):
            continue
        sf = ScopedField.from_dict({**row, "name": row.get("name") or key})
        if sf.name and sf.value:
            out[sf.name] = sf
    return out
