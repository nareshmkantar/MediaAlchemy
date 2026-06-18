"""SQLite-backed repositories for SchemaAgent metadata.

All repositories share a single :class:`MetadataStore` which owns the
SQLite connection and ensures the schema is applied on startup. Each
record type has a small ``dataclass`` used as the in/out type so callers
do not need to know the SQL layout.

Design rules:
- JSON payloads are stored as ``TEXT`` columns with ``_json`` suffix.
- ``upsert_*`` methods are idempotent on their primary key.
- Read methods never return SQL row objects; they always return dataclasses
  or plain dicts to keep the surface portable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .schema import DDL_STATEMENTS


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _loads(value: Optional[str]) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


@dataclass
class JobRecord:
    job_id: str
    status: str = "created"
    template_path: Optional[str] = None
    template_version: Optional[str] = None
    active_run_version: int = 0
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceRecord:
    source_id: str
    job_id: str
    file_id: str
    file_name: str
    file_path: str
    sheet_name: Optional[str] = None
    sheet_order: int = 0
    role: str = "unknown"
    fingerprint: str = ""
    status: str = "discovered"
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)


@dataclass
class RelationshipRecord:
    relationship_id: str
    job_id: str
    from_source_id: str
    to_source_id: str
    relationship_kind: str  # 'join' | 'lookup' | 'union'
    direction: str = "bidirectional"  # 'from_to' | 'to_from' | 'bidirectional'
    join_keys: List[Dict[str, str]] = field(default_factory=list)  # [{"from": str, "to": str}]
    cardinality: str = "unknown"  # '1:1' | '1:many' | 'many:1' | 'many:many'
    confidence: float = 0.0
    status: str = "proposed"  # 'proposed' | 'approved' | 'rejected'
    rationale: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)


@dataclass
class DerivedFieldRecord:
    derived_field_id: str
    job_id: str
    target_source_id: str
    target_column: str
    expression: str
    join_context: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[Dict[str, str]] = field(default_factory=list)  # [{"source_id": str, "column": str}]
    fallback: Optional[str] = None
    status: str = "proposed"
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)


@dataclass
class ArtifactRecord:
    artifact_id: str
    job_id: str
    source_id: Optional[str]
    kind: str
    version: int
    path: str
    format: str
    fingerprint: str
    size_bytes: int = 0
    created_at: str = field(default_factory=_utcnow)


@dataclass
class HITLCheckpointRecord:
    checkpoint_id: str
    job_id: str
    source_id: Optional[str]
    stage: str
    status: str
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)


class MetadataStore:
    """Thin, thread-safe SQLite wrapper with per-record-type helpers."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._apply_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def _apply_schema(self) -> None:
        with self._cursor() as cur:
            for statement in DDL_STATEMENTS:
                cur.execute(statement)

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def upsert_job(self, record: JobRecord) -> JobRecord:
        record.updated_at = _utcnow()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (job_id, status, template_path, template_version, active_run_version,
                                  created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    template_path = excluded.template_path,
                    template_version = excluded.template_version,
                    active_run_version = excluded.active_run_version,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    record.job_id,
                    record.status,
                    record.template_path,
                    record.template_version,
                    record.active_run_version,
                    record.created_at,
                    record.updated_at,
                    _dumps(record.metadata),
                ),
            )
        return record

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            row = cur.fetchone()
        if not row:
            return None
        return JobRecord(
            job_id=row["job_id"],
            status=row["status"],
            template_path=row["template_path"],
            template_version=row["template_version"],
            active_run_version=int(row["active_run_version"] or 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=_loads(row["metadata_json"]),
        )

    def list_jobs(self) -> List[JobRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM jobs ORDER BY created_at DESC")
            rows = cur.fetchall()
        return [
            JobRecord(
                job_id=row["job_id"],
                status=row["status"],
                template_path=row["template_path"],
                template_version=row["template_version"],
                active_run_version=int(row["active_run_version"] or 0),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                metadata=_loads(row["metadata_json"]),
            )
            for row in rows
        ]

    def delete_job(self, job_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------
    def upsert_source(self, record: SourceRecord) -> SourceRecord:
        record.updated_at = _utcnow()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO sources (source_id, job_id, file_id, file_name, file_path, sheet_name,
                                     sheet_order, role, fingerprint, status, metadata_json,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    file_id = excluded.file_id,
                    file_name = excluded.file_name,
                    file_path = excluded.file_path,
                    sheet_name = excluded.sheet_name,
                    sheet_order = excluded.sheet_order,
                    role = excluded.role,
                    fingerprint = excluded.fingerprint,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.source_id,
                    record.job_id,
                    record.file_id,
                    record.file_name,
                    record.file_path,
                    record.sheet_name,
                    record.sheet_order,
                    record.role,
                    record.fingerprint,
                    record.status,
                    _dumps(record.metadata),
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def list_sources(self, job_id: str) -> List[SourceRecord]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM sources WHERE job_id = ? ORDER BY sheet_order, source_id",
                (job_id,),
            )
            rows = cur.fetchall()
        return [self._row_to_source(row) for row in rows]

    def get_source(self, source_id: str) -> Optional[SourceRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,))
            row = cur.fetchone()
        return self._row_to_source(row) if row else None

    @staticmethod
    def _row_to_source(row: sqlite3.Row) -> SourceRecord:
        return SourceRecord(
            source_id=row["source_id"],
            job_id=row["job_id"],
            file_id=row["file_id"],
            file_name=row["file_name"],
            file_path=row["file_path"],
            sheet_name=row["sheet_name"],
            sheet_order=int(row["sheet_order"] or 0),
            role=row["role"],
            fingerprint=row["fingerprint"],
            status=row["status"],
            metadata=_loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Scope / mappings / business rules (payload per source)
    # ------------------------------------------------------------------
    def _upsert_payload(self, table: str, source_id: str, job_id: str, payload: Any, version: int = 1) -> None:
        now = _utcnow()
        with self._cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {table} (source_id, job_id, version, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    version = excluded.version,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (source_id, job_id, version, _dumps(payload), now),
            )

    def _get_payload(self, table: str, source_id: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE source_id = ?", (source_id,))
            row = cur.fetchone()
        if not row:
            return None
        return {
            "source_id": row["source_id"],
            "job_id": row["job_id"],
            "version": int(row["version"] or 1),
            "payload": _loads(row["payload_json"]),
            "updated_at": row["updated_at"],
        }

    def save_scope(self, source_id: str, job_id: str, payload: Any, version: int = 1) -> None:
        self._upsert_payload("scopes", source_id, job_id, payload, version)

    def get_scope(self, source_id: str) -> Optional[Dict[str, Any]]:
        return self._get_payload("scopes", source_id)

    def save_mapping(self, source_id: str, job_id: str, payload: Any, version: int = 1) -> None:
        self._upsert_payload("mappings", source_id, job_id, payload, version)

    def get_mapping(self, source_id: str) -> Optional[Dict[str, Any]]:
        return self._get_payload("mappings", source_id)

    def save_business_rules(self, source_id: str, job_id: str, payload: Any, version: int = 1) -> None:
        self._upsert_payload("business_rules", source_id, job_id, payload, version)

    def get_business_rules(self, source_id: str) -> Optional[Dict[str, Any]]:
        return self._get_payload("business_rules", source_id)

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    def upsert_relationship(self, record: RelationshipRecord) -> RelationshipRecord:
        record.updated_at = _utcnow()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO relationships (relationship_id, job_id, from_source_id, to_source_id,
                                           relationship_kind, direction, join_keys_json, cardinality,
                                           confidence, status, rationale, approved_by, approved_at,
                                           payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(relationship_id) DO UPDATE SET
                    from_source_id = excluded.from_source_id,
                    to_source_id = excluded.to_source_id,
                    relationship_kind = excluded.relationship_kind,
                    direction = excluded.direction,
                    join_keys_json = excluded.join_keys_json,
                    cardinality = excluded.cardinality,
                    confidence = excluded.confidence,
                    status = excluded.status,
                    rationale = excluded.rationale,
                    approved_by = excluded.approved_by,
                    approved_at = excluded.approved_at,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.relationship_id,
                    record.job_id,
                    record.from_source_id,
                    record.to_source_id,
                    record.relationship_kind,
                    record.direction,
                    _dumps(record.join_keys),
                    record.cardinality,
                    float(record.confidence or 0.0),
                    record.status,
                    record.rationale,
                    record.approved_by,
                    record.approved_at,
                    _dumps(record.payload),
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def list_relationships(self, job_id: str, *, status: Optional[str] = None) -> List[RelationshipRecord]:
        query = "SELECT * FROM relationships WHERE job_id = ?"
        params: List[Any] = [job_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at"
        with self._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [self._row_to_relationship(row) for row in rows]

    @staticmethod
    def _row_to_relationship(row: sqlite3.Row) -> RelationshipRecord:
        return RelationshipRecord(
            relationship_id=row["relationship_id"],
            job_id=row["job_id"],
            from_source_id=row["from_source_id"],
            to_source_id=row["to_source_id"],
            relationship_kind=row["relationship_kind"],
            direction=row["direction"],
            join_keys=_loads(row["join_keys_json"]) or [],
            cardinality=row["cardinality"],
            confidence=float(row["confidence"] or 0.0),
            status=row["status"],
            rationale=row["rationale"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            payload=_loads(row["payload_json"]) or {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Derived fields
    # ------------------------------------------------------------------
    def upsert_derived_field(self, record: DerivedFieldRecord) -> DerivedFieldRecord:
        record.updated_at = _utcnow()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO derived_fields (derived_field_id, job_id, target_source_id, target_column,
                                            expression, join_context_json, dependencies_json, fallback,
                                            status, approved_by, approved_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(derived_field_id) DO UPDATE SET
                    target_source_id = excluded.target_source_id,
                    target_column = excluded.target_column,
                    expression = excluded.expression,
                    join_context_json = excluded.join_context_json,
                    dependencies_json = excluded.dependencies_json,
                    fallback = excluded.fallback,
                    status = excluded.status,
                    approved_by = excluded.approved_by,
                    approved_at = excluded.approved_at,
                    updated_at = excluded.updated_at
                """,
                (
                    record.derived_field_id,
                    record.job_id,
                    record.target_source_id,
                    record.target_column,
                    record.expression,
                    _dumps(record.join_context),
                    _dumps(record.dependencies),
                    record.fallback,
                    record.status,
                    record.approved_by,
                    record.approved_at,
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def list_derived_fields(
        self,
        job_id: str,
        *,
        target_source_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[DerivedFieldRecord]:
        query = "SELECT * FROM derived_fields WHERE job_id = ?"
        params: List[Any] = [job_id]
        if target_source_id:
            query += " AND target_source_id = ?"
            params.append(target_source_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at"
        with self._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [
            DerivedFieldRecord(
                derived_field_id=row["derived_field_id"],
                job_id=row["job_id"],
                target_source_id=row["target_source_id"],
                target_column=row["target_column"],
                expression=row["expression"],
                join_context=_loads(row["join_context_json"]) or {},
                dependencies=_loads(row["dependencies_json"]) or [],
                fallback=row["fallback"],
                status=row["status"],
                approved_by=row["approved_by"],
                approved_at=row["approved_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Artifact metadata
    # ------------------------------------------------------------------
    def record_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifacts (artifact_id, job_id, source_id, kind, version, path,
                                       format, fingerprint, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    source_id = excluded.source_id,
                    kind = excluded.kind,
                    version = excluded.version,
                    path = excluded.path,
                    format = excluded.format,
                    fingerprint = excluded.fingerprint,
                    size_bytes = excluded.size_bytes,
                    created_at = excluded.created_at
                """,
                (
                    record.artifact_id,
                    record.job_id,
                    record.source_id,
                    record.kind,
                    record.version,
                    record.path,
                    record.format,
                    record.fingerprint,
                    record.size_bytes,
                    record.created_at,
                ),
            )
        return record

    def find_latest_artifact(
        self,
        *,
        job_id: str,
        source_id: Optional[str],
        kind: str,
        fingerprint: Optional[str] = None,
    ) -> Optional[ArtifactRecord]:
        query = "SELECT * FROM artifacts WHERE job_id = ? AND kind = ?"
        params: List[Any] = [job_id, kind]
        if source_id is None:
            query += " AND source_id IS NULL"
        else:
            query += " AND source_id = ?"
            params.append(source_id)
        if fingerprint:
            query += " AND fingerprint = ?"
            params.append(fingerprint)
        query += " ORDER BY version DESC, created_at DESC LIMIT 1"
        with self._cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
        if not row:
            return None
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            job_id=row["job_id"],
            source_id=row["source_id"],
            kind=row["kind"],
            version=int(row["version"] or 1),
            path=row["path"],
            format=row["format"],
            fingerprint=row["fingerprint"],
            size_bytes=int(row["size_bytes"] or 0),
            created_at=row["created_at"],
        )

    def list_artifacts(self, job_id: str, *, source_id: Optional[str] = None) -> List[ArtifactRecord]:
        query = "SELECT * FROM artifacts WHERE job_id = ?"
        params: List[Any] = [job_id]
        if source_id is not None:
            query += " AND source_id = ?"
            params.append(source_id)
        query += " ORDER BY created_at"
        with self._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [
            ArtifactRecord(
                artifact_id=row["artifact_id"],
                job_id=row["job_id"],
                source_id=row["source_id"],
                kind=row["kind"],
                version=int(row["version"] or 1),
                path=row["path"],
                format=row["format"],
                fingerprint=row["fingerprint"],
                size_bytes=int(row["size_bytes"] or 0),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # HITL checkpoints
    # ------------------------------------------------------------------
    def upsert_hitl_checkpoint(self, record: HITLCheckpointRecord) -> HITLCheckpointRecord:
        record.updated_at = _utcnow()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO hitl_checkpoints (checkpoint_id, job_id, source_id, stage, status,
                                              payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    source_id = excluded.source_id,
                    stage = excluded.stage,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.checkpoint_id,
                    record.job_id,
                    record.source_id,
                    record.stage,
                    record.status,
                    _dumps(record.payload),
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def list_hitl_checkpoints(self, job_id: str) -> List[HITLCheckpointRecord]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM hitl_checkpoints WHERE job_id = ? ORDER BY created_at",
                (job_id,),
            )
            rows = cur.fetchall()
        return [
            HITLCheckpointRecord(
                checkpoint_id=row["checkpoint_id"],
                job_id=row["job_id"],
                source_id=row["source_id"],
                stage=row["stage"],
                status=row["status"],
                payload=_loads(row["payload_json"]) or {},
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
