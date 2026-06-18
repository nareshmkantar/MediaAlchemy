"""End-to-end tests for the multi-source orchestration service.

These tests exercise the target architecture's post-normalization stage:
the source graph is built from approved relationships, the artifact store
persists normalized frames, cross-source derived fields are validated and
applied, and the final collated frame is returned.
"""

from __future__ import annotations

import pandas as pd

from sia.agent.cross_source import DerivedFieldPlan
from sia.agent.multi_source_service import MultiSourceService
from sia.storage import ArtifactKind, ArtifactStore, JobRecord, MetadataStore


def _summary(source_id, columns, role="fact", file_name="f.xlsx", sheet="S"):
    return {
        "source_id": source_id,
        "role": role,
        "file_name": file_name,
        "sheet_name": sheet,
        "columns": list(columns),
    }


def test_multi_source_service_applies_derivation_and_collates(tmp_path):
    metadata = MetadataStore(tmp_path / "metadata.sqlite")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    service = MultiSourceService(metadata_store=metadata, artifact_store=artifacts)

    metadata.upsert_job(JobRecord(job_id="job_int", status="running"))

    fact = pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "campaign_id": ["c1", "c2", "c3"],
            "spend": [100.0, 200.0, 50.0],
        }
    )
    lookup = pd.DataFrame(
        {
            "campaign_id": ["c1", "c2"],
            "campaign_group": ["Brand", "Performance"],
        }
    )

    service.persist_normalized_source(
        job_id="job_int",
        source_id="fact",
        frame=fact,
        fingerprint_parts=[("mappings", 1)],
    )
    service.persist_normalized_source(
        job_id="job_int",
        source_id="lookup",
        frame=lookup,
        fingerprint_parts=[("mappings", 1)],
    )

    summaries = [
        _summary("fact", list(fact.columns), role="fact"),
        _summary("lookup", list(lookup.columns), role="lookup"),
    ]
    relationships = [
        {
            "relationship_id": "fact_lookup",
            "from_source_id": "fact",
            "to_source_id": "lookup",
            "relationship_kind": "lookup",
            "status": "approved",
            "direction": "from_to",
            "join_keys": [{"from": "campaign_id", "to": "campaign_id"}],
        }
    ]
    plan = DerivedFieldPlan(
        derived_field_id="d_campaign_group",
        target_source_id="fact",
        target_column="campaign_group",
        expression='COALESCE(lookup.campaign_group, literal:"Other")',
        status="approved",
        approved_by="analyst",
    )

    result = service.run(
        job_id="job_int",
        source_summaries=summaries,
        approved_relationships=relationships,
        frames_by_source={"fact": fact, "lookup": lookup},
        derived_field_plans=[plan],
    )

    assert [outcome.status for outcome in result.derivations] == ["applied"]
    collated = result.collated
    assert "campaign_group" in collated.columns
    assert sorted([str(value) for value in collated["campaign_group"].dropna().unique()]) == [
        "Brand", "Other", "Performance",
    ]

    latest = metadata.find_latest_artifact(
        job_id="job_int",
        source_id="fact",
        kind=ArtifactKind.NORMALIZED_DATAFRAME.value,
    )
    assert latest is not None
    assert latest.fingerprint

    reloaded = service.load_normalized_frames("job_int", ["fact", "lookup"])
    assert set(reloaded.keys()) == {"fact", "lookup"}
    assert list(reloaded["fact"].columns) == list(fact.columns)

    derived_rows = metadata.list_derived_fields("job_int")
    assert len(derived_rows) == 1
    assert derived_rows[0].status == "approved"


def test_multi_source_service_records_validation_failures(tmp_path):
    metadata = MetadataStore(tmp_path / "metadata.sqlite")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    service = MultiSourceService(metadata_store=metadata, artifact_store=artifacts)

    metadata.upsert_job(JobRecord(job_id="job_bad"))

    fact = pd.DataFrame({"campaign_id": ["c1"], "spend": [1.0]})
    lookup = pd.DataFrame({"campaign_id": ["c1"], "group": ["A"]})

    summaries = [
        _summary("fact", list(fact.columns), role="fact"),
        _summary("lookup", list(lookup.columns), role="lookup"),
    ]

    # No approved relationship at all
    plan = DerivedFieldPlan(
        derived_field_id="d_bad_path",
        target_source_id="fact",
        target_column="group",
        expression="lookup.group",
        status="approved",
    )

    result = service.run(
        job_id="job_bad",
        source_summaries=summaries,
        approved_relationships=[],
        frames_by_source={"fact": fact, "lookup": lookup},
        derived_field_plans=[plan],
    )
    assert result.derivations[0].status == "validation_failed"
    assert any("no_join_path" in issue or "join path" in issue.lower() for issue in result.derivations[0].errors)
    # Collation should still complete (fact + lookup concatenated as independents)
    assert not result.collated.empty
    # No derived column was materialized on the fact frame itself
    assert result.collated.iloc[:1]["spend"].iloc[0] == 1.0
