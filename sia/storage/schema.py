"""SQLite DDL for the SchemaAgent metadata store.

Tables are keyed by ``job_id`` and, where applicable, ``source_id``. All
columns are declared ``TEXT`` unless a numeric semantics is required, with
JSON-encoded payloads stored as ``TEXT``. This keeps the store portable and
easy to dump/restore while still supporting structured repository APIs.
"""

from __future__ import annotations

from typing import List


DDL_STATEMENTS: List[str] = [
    # Jobs
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        template_path TEXT,
        template_version TEXT,
        active_run_version INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # Sources
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        file_name TEXT NOT NULL,
        file_path TEXT NOT NULL,
        sheet_name TEXT,
        sheet_order INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT 'unknown',
        fingerprint TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'discovered',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sources_job ON sources(job_id)",
    # Scope decisions
    """
    CREATE TABLE IF NOT EXISTS scopes (
        source_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    )
    """,
    # Mapping decisions (one row per source, payload contains the list)
    """
    CREATE TABLE IF NOT EXISTS mappings (
        source_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    )
    """,
    # Business rules (one row per source)
    """
    CREATE TABLE IF NOT EXISTS business_rules (
        source_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    )
    """,
    # Relationships (edges between sources). Stored per-job.
    """
    CREATE TABLE IF NOT EXISTS relationships (
        relationship_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        from_source_id TEXT NOT NULL,
        to_source_id TEXT NOT NULL,
        relationship_kind TEXT NOT NULL,
        direction TEXT NOT NULL DEFAULT 'bidirectional',
        join_keys_json TEXT NOT NULL DEFAULT '[]',
        cardinality TEXT NOT NULL DEFAULT 'unknown',
        confidence REAL NOT NULL DEFAULT 0.0,
        status TEXT NOT NULL DEFAULT 'proposed',
        rationale TEXT,
        approved_by TEXT,
        approved_at TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_relationships_job ON relationships(job_id)",
    # Derived field plans for cross-source derivations
    """
    CREATE TABLE IF NOT EXISTS derived_fields (
        derived_field_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        target_source_id TEXT NOT NULL,
        target_column TEXT NOT NULL,
        expression TEXT NOT NULL,
        join_context_json TEXT NOT NULL DEFAULT '{}',
        dependencies_json TEXT NOT NULL DEFAULT '[]',
        fallback TEXT,
        status TEXT NOT NULL DEFAULT 'proposed',
        approved_by TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_derived_job ON derived_fields(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_derived_target ON derived_fields(target_source_id)",
    # Artifact metadata (one row per artifact version)
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        source_id TEXT,
        kind TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        path TEXT NOT NULL,
        format TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        size_bytes INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_artifacts_job_source_kind ON artifacts(job_id, source_id, kind)",
    # HITL checkpoints - used for resume
    """
    CREATE TABLE IF NOT EXISTS hitl_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        source_id TEXT,
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hitl_job ON hitl_checkpoints(job_id)",
]
