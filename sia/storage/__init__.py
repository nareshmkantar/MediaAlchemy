"""Durable storage layer for SchemaAgent.

This package provides a persistent home for job metadata, source registry
decisions, relationship records, derived field plans, and per-source
artifacts. It is intentionally additive: existing in-memory code paths keep
working, and the storage layer can be enabled incrementally via a feature
flag or wiring change.

Modules:
- :mod:`sia.storage.schema` - SQLite DDL statements for the metadata store.
- :mod:`sia.storage.repositories` - Repository classes for each record type.
- :mod:`sia.storage.artifacts` - Filesystem/Parquet-backed artifact store
  keyed by ``(job_id, source_id, artifact_kind, version)``.
- :mod:`sia.storage.fingerprint` - Stable fingerprinting for artifact
  invalidation.
"""

from .artifacts import ArtifactKind, ArtifactRef, ArtifactStore
from .fingerprint import compute_fingerprint
from .repositories import (
    ArtifactRecord,
    DerivedFieldRecord,
    HITLCheckpointRecord,
    JobRecord,
    MetadataStore,
    RelationshipRecord,
    SourceRecord,
)

__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactRecord",
    "ArtifactStore",
    "DerivedFieldRecord",
    "HITLCheckpointRecord",
    "JobRecord",
    "MetadataStore",
    "RelationshipRecord",
    "SourceRecord",
    "compute_fingerprint",
]
