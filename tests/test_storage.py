"""Tests for the durable metadata and artifact storage layer."""

from __future__ import annotations

import pandas as pd

from sia.storage import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactStore,
    DerivedFieldRecord,
    JobRecord,
    MetadataStore,
    RelationshipRecord,
    SourceRecord,
    compute_fingerprint,
)


def _store(tmp_path) -> MetadataStore:
    return MetadataStore(tmp_path / "metadata.sqlite")


def test_fingerprint_is_stable_and_order_insensitive():
    fp_a = compute_fingerprint({"b": 2, "a": 1}, [1, 2, 3])
    fp_b = compute_fingerprint({"a": 1, "b": 2}, [1, 2, 3])
    fp_c = compute_fingerprint({"a": 1, "b": 3}, [1, 2, 3])
    assert fp_a == fp_b
    assert fp_a != fp_c
    assert len(fp_a) == 16


def test_metadata_store_roundtrips_job_and_source(tmp_path):
    store = _store(tmp_path)
    store.upsert_job(JobRecord(job_id="job_1", status="running", template_path="/tpl.json"))
    job = store.get_job("job_1")
    assert job is not None
    assert job.status == "running"
    assert job.template_path == "/tpl.json"

    store.upsert_source(
        SourceRecord(
            source_id="src_1",
            job_id="job_1",
            file_id="file_a",
            file_name="a.xlsx",
            file_path="/uploads/a.xlsx",
            sheet_name="Main",
            role="fact",
            fingerprint="abcd",
        )
    )
    sources = store.list_sources("job_1")
    assert len(sources) == 1
    assert sources[0].role == "fact"


def test_metadata_store_payload_updates(tmp_path):
    store = _store(tmp_path)
    store.upsert_job(JobRecord(job_id="job_x"))
    store.upsert_source(
        SourceRecord(
            source_id="src_x",
            job_id="job_x",
            file_id="f",
            file_name="f.xlsx",
            file_path="/uploads/f.xlsx",
        )
    )

    store.save_scope("src_x", "job_x", {"header_row": 2, "main_blocks": [{"id": "b1"}]})
    store.save_mapping("src_x", "job_x", [{"source_column": "A", "target_column": "a"}])
    store.save_business_rules("src_x", "job_x", [{"target_column": "a", "rule_type": "format"}])

    scope = store.get_scope("src_x")
    assert scope and scope["payload"]["header_row"] == 2

    mapping = store.get_mapping("src_x")
    assert mapping and mapping["payload"][0]["target_column"] == "a"

    rules = store.get_business_rules("src_x")
    assert rules and rules["payload"][0]["rule_type"] == "format"

    store.save_scope("src_x", "job_x", {"header_row": 5}, version=2)
    assert store.get_scope("src_x")["version"] == 2


def test_metadata_store_relationships_and_derived_fields(tmp_path):
    store = _store(tmp_path)
    store.upsert_job(JobRecord(job_id="job_r"))
    store.upsert_relationship(
        RelationshipRecord(
            relationship_id="r1",
            job_id="job_r",
            from_source_id="src_a",
            to_source_id="src_b",
            relationship_kind="join",
            direction="from_to",
            join_keys=[{"from": "date", "to": "date"}],
            cardinality="1:many",
            confidence=0.9,
            status="approved",
        )
    )
    approved = store.list_relationships("job_r", status="approved")
    assert len(approved) == 1
    assert approved[0].join_keys == [{"from": "date", "to": "date"}]

    store.upsert_derived_field(
        DerivedFieldRecord(
            derived_field_id="d1",
            job_id="job_r",
            target_source_id="src_a",
            target_column="campaign_group",
            expression="src_b.group_name",
            join_context={"join_keys": [{"from": "campaign_id", "to": "campaign_id"}]},
            dependencies=[{"source_id": "src_b", "column": "group_name"}],
            status="approved",
        )
    )
    derived = store.list_derived_fields("job_r", target_source_id="src_a")
    assert len(derived) == 1
    assert derived[0].expression == "src_b.group_name"
    assert derived[0].dependencies[0]["column"] == "group_name"


def test_artifact_store_persists_dataframe_and_json(tmp_path):
    ms = _store(tmp_path)
    ms.upsert_job(JobRecord(job_id="job_art"))
    artifacts = ArtifactStore(tmp_path / "artifacts")

    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    fp = compute_fingerprint({"schema": ["a", "b"]})
    ref_df = artifacts.save(
        job_id="job_art",
        source_id="src_1",
        kind=ArtifactKind.NORMALIZED_DATAFRAME,
        payload=df,
        fingerprint=fp,
    )
    assert ref_df.format in {"parquet", "csv"}
    loaded_df = artifacts.load(ref_df)
    assert list(loaded_df.columns) == ["a", "b"]
    assert len(loaded_df) == 3

    ref_json = artifacts.save(
        job_id="job_art",
        source_id="src_1",
        kind=ArtifactKind.INTERPRETED_CONTEXT,
        payload={"fields": {"publisher": "Instagram"}, "assumptions": []},
        fingerprint=fp,
    )
    assert ref_json.format == "json"
    loaded = artifacts.load(ref_json)
    assert loaded["fields"]["publisher"] == "Instagram"

    ms.record_artifact(
        ArtifactRecord(
            artifact_id=f"{ref_df.job_id}:{ref_df.source_id}:{ref_df.kind.value}:{ref_df.version}",
            job_id=ref_df.job_id,
            source_id=ref_df.source_id,
            kind=ref_df.kind.value,
            version=ref_df.version,
            path=ref_df.path,
            format=ref_df.format,
            fingerprint=ref_df.fingerprint,
            size_bytes=ref_df.size_bytes,
        )
    )
    latest = ms.find_latest_artifact(
        job_id="job_art",
        source_id="src_1",
        kind=ArtifactKind.NORMALIZED_DATAFRAME.value,
    )
    assert latest is not None
    assert latest.fingerprint == fp
