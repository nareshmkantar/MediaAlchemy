"""Multi-source orchestration service.

This service ties together the new storage, source graph, and cross-source
derivation modules without touching the existing single-source LangGraph
pipeline. It is used after every selected source has been normalized to:

1. Build an executable :class:`SourceGraph` from persisted source summaries
   and approved relationships.
2. Persist per-source normalized artifacts via the :class:`ArtifactStore`.
3. Validate and apply approved :class:`DerivedFieldPlan` records.
4. Produce the final collated dataframe through
   :meth:`SourceGraph.collate`.

It is deliberately additive; callers (web layer, CLI, or tests) opt in by
constructing the service with an existing metadata store and artifact store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import pandas as pd

from .cross_source import (
    DerivedFieldPlan,
    apply_derived_field_plan,
    validate_derived_field_plan,
)
from .source_graph import SourceGraph, build_source_graph
from ..storage import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactStore,
    DerivedFieldRecord,
    MetadataStore,
    compute_fingerprint,
)


@dataclass
class DerivationOutcome:
    derived_field_id: str
    target_source_id: str
    target_column: str
    status: str  # 'applied' | 'validation_failed' | 'execution_failed'
    errors: List[str] = field(default_factory=list)


@dataclass
class MultiSourceRunResult:
    collated: pd.DataFrame
    graph: SourceGraph
    derivations: List[DerivationOutcome] = field(default_factory=list)
    artifact_refs: Dict[str, Dict[str, str]] = field(default_factory=dict)


class MultiSourceService:
    """Post-per-source orchestration: artifacts, graph, derivations, collation."""

    def __init__(
        self,
        *,
        metadata_store: Optional[MetadataStore] = None,
        artifact_store: Optional[ArtifactStore] = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.artifact_store = artifact_store

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def persist_normalized_source(
        self,
        *,
        job_id: str,
        source_id: str,
        frame: pd.DataFrame,
        fingerprint_parts: Sequence[Any] = (),
    ) -> Optional[str]:
        """Persist a normalized per-source dataframe and record its metadata.

        Returns the artifact path or ``None`` when no artifact store is
        configured. Callers may still use the service purely in-memory.
        """
        if self.artifact_store is None:
            return None
        fingerprint = compute_fingerprint(
            ("normalized_dataframe", source_id),
            list(frame.columns),
            *fingerprint_parts,
        )
        ref = self.artifact_store.save(
            job_id=job_id,
            source_id=source_id,
            kind=ArtifactKind.NORMALIZED_DATAFRAME,
            payload=frame,
            fingerprint=fingerprint,
        )
        if self.metadata_store is not None:
            self.metadata_store.record_artifact(
                ArtifactRecord(
                    artifact_id=f"{job_id}:{source_id}:{ArtifactKind.NORMALIZED_DATAFRAME.value}:{ref.version}",
                    job_id=job_id,
                    source_id=source_id,
                    kind=ArtifactKind.NORMALIZED_DATAFRAME.value,
                    version=ref.version,
                    path=ref.path,
                    format=ref.format,
                    fingerprint=ref.fingerprint,
                    size_bytes=ref.size_bytes,
                )
            )
        return ref.path

    def load_normalized_frames(self, job_id: str, source_ids: Iterable[str]) -> Dict[str, pd.DataFrame]:
        """Best-effort reload of persisted per-source normalized frames."""
        frames: Dict[str, pd.DataFrame] = {}
        if self.metadata_store is None or self.artifact_store is None:
            return frames
        for source_id in source_ids:
            record = self.metadata_store.find_latest_artifact(
                job_id=job_id,
                source_id=source_id,
                kind=ArtifactKind.NORMALIZED_DATAFRAME.value,
            )
            if record is None:
                continue
            try:
                ref = _artifact_record_to_ref(record)
                frames[source_id] = self.artifact_store.load(ref)
            except FileNotFoundError:
                continue
        return frames

    # ------------------------------------------------------------------
    # Run orchestration
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        job_id: str,
        source_summaries: Sequence[Dict[str, Any]],
        approved_relationships: Sequence[Any],
        frames_by_source: Mapping[str, pd.DataFrame],
        derived_field_plans: Sequence[DerivedFieldPlan] = (),
    ) -> MultiSourceRunResult:
        """Execute the post-normalization multi-source stage.

        Steps:
        1. Build the :class:`SourceGraph` from ``source_summaries`` and
           ``approved_relationships``.
        2. Apply each approved derived field plan against its target frame
           using the approved join paths.
        3. Collate all frames using the graph's executable collation.
        """
        graph = build_source_graph(source_summaries, approved_relationships)

        mutable_frames: Dict[str, pd.DataFrame] = {sid: frame.copy() for sid, frame in frames_by_source.items()}

        outcomes: List[DerivationOutcome] = []
        for plan in derived_field_plans:
            if plan.target_source_id not in mutable_frames:
                outcomes.append(
                    DerivationOutcome(
                        derived_field_id=plan.derived_field_id,
                        target_source_id=plan.target_source_id,
                        target_column=plan.target_column,
                        status="validation_failed",
                        errors=[f"Target frame {plan.target_source_id!r} not provided"],
                    )
                )
                continue
            validation = validate_derived_field_plan(plan, graph)
            if not validation.ok:
                outcomes.append(
                    DerivationOutcome(
                        derived_field_id=plan.derived_field_id,
                        target_source_id=plan.target_source_id,
                        target_column=plan.target_column,
                        status="validation_failed",
                        errors=validation.error_messages(),
                    )
                )
                continue
            try:
                mutable_frames[plan.target_source_id] = apply_derived_field_plan(
                    plan,
                    graph=graph,
                    target_frame=mutable_frames[plan.target_source_id],
                    frames_by_source={
                        sid: frame for sid, frame in mutable_frames.items() if sid != plan.target_source_id
                    },
                )
                outcomes.append(
                    DerivationOutcome(
                        derived_field_id=plan.derived_field_id,
                        target_source_id=plan.target_source_id,
                        target_column=plan.target_column,
                        status="applied",
                    )
                )
                if self.metadata_store is not None and job_id:
                    self.metadata_store.upsert_derived_field(
                        DerivedFieldRecord(
                            derived_field_id=plan.derived_field_id,
                            job_id=job_id,
                            target_source_id=plan.target_source_id,
                            target_column=plan.target_column,
                            expression=plan.expression,
                            join_context=dict(plan.join_context),
                            dependencies=list(plan.dependencies),
                            fallback=plan.fallback,
                            status="approved" if plan.status == "approved" else plan.status,
                            approved_by=plan.approved_by,
                        )
                    )
            except Exception as exc:  # pragma: no cover - defensive
                outcomes.append(
                    DerivationOutcome(
                        derived_field_id=plan.derived_field_id,
                        target_source_id=plan.target_source_id,
                        target_column=plan.target_column,
                        status="execution_failed",
                        errors=[str(exc)],
                    )
                )

        collated = graph.collate(mutable_frames)
        return MultiSourceRunResult(collated=collated, graph=graph, derivations=outcomes)


def _artifact_record_to_ref(record: ArtifactRecord):
    from ..storage.artifacts import ArtifactKind, ArtifactRef

    return ArtifactRef(
        job_id=record.job_id,
        source_id=record.source_id,
        kind=ArtifactKind(record.kind),
        version=record.version,
        fingerprint=record.fingerprint,
        path=record.path,
        format=record.format,
        size_bytes=record.size_bytes,
        created_at=record.created_at,
    )
