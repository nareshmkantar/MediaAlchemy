"""Context engineering: scoped fields, artifacts, fingerprints, debug payloads."""

from sia.context.confidence import MIN_CONFIDENCE_TO_STAMP
from sia.context.merge import merge_scoped_field, merge_scoped_fields
from sia.context.scoped_field import (
    CONTEXT_FIELD_SCOPES,
    ScopedField,
    scoped_fields_from_dict,
    scoped_fields_to_dict,
    scoped_fields_to_flat,
)

__all__ = [
    "CONTEXT_FIELD_SCOPES",
    "MIN_CONFIDENCE_TO_STAMP",
    "ScopedField",
    "merge_scoped_field",
    "merge_scoped_fields",
    "scoped_fields_from_dict",
    "scoped_fields_to_dict",
    "scoped_fields_to_flat",
]
