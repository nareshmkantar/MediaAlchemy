"""Phase 2: context artifact persistence and fingerprinting."""

from __future__ import annotations

from sia.agent.context_packet import build_context_packet
from sia.context.artifacts import (
    get_artifact_store,
    persist_context_artifacts,
    resolve_source_context_material,
    try_load_cached_context_artifacts,
)
from sia.context.fingerprint import compute_source_context_fingerprint


def test_context_fingerprint_changes_when_layout_changes():
    job = {
        "id": "fp-job",
        "file_path": "uploads/sample.xlsx",
        "layout_registry": [
            {
                "source_id": "s1",
                "sheet_name": "Radio_DE",
                "block_id": "meta",
                "decision": "context",
                "coordinates": {"start_row": 0, "end_row": 2, "start_col": 0, "end_col": 3},
            }
        ],
    }
    fp1 = compute_source_context_fingerprint(job, source_id="s1", sheet_name="Radio_DE")
    job["layout_registry"][0]["coordinates"]["end_row"] = 4
    fp2 = compute_source_context_fingerprint(job, source_id="s1", sheet_name="Radio_DE")
    assert fp1 != fp2


def test_persist_and_reload_context_artifacts(tmp_path):
    store = get_artifact_store(tmp_path / "artifacts")
    job = {"id": "art-job"}
    snippets = [{"block_label": "Meta", "text_preview": ["Market: UK"]}]
    interpreted = {"fields": {"market": "UK"}, "evidence": []}
    fp = "abc123"
    persist_context_artifacts(
        job,
        source_id="s1",
        fingerprint=fp,
        snippets=snippets,
        interpreted_context=interpreted,
        store=store,
    )
    loaded_snippets, loaded_interpreted = try_load_cached_context_artifacts(
        job, source_id="s1", fingerprint=fp, store=store
    )
    assert loaded_snippets == snippets
    assert loaded_interpreted["fields"]["market"] == "UK"


def test_resolve_source_context_material_uses_cache_on_second_call(tmp_path):
    store = get_artifact_store(tmp_path / "artifacts")
    job = {
        "id": "cache-job",
        "file_path": "uploads/x.xlsx",
        "layout_registry": [],
    }
    calls = {"n": 0}

    def build_fresh():
        calls["n"] += 1
        return [{"block_label": "A"}], {"fields": {"market": "DE"}, "evidence": []}

    s1, i1, fp1, cached1 = resolve_source_context_material(
        job,
        source_id="s1",
        sheet_name="Radio_DE",
        source_metadata={"file_path": "uploads/x.xlsx"},
        approved_layout={},
        build_fresh=build_fresh,
        store=store,
    )
    s2, i2, fp2, cached2 = resolve_source_context_material(
        job,
        source_id="s1",
        sheet_name="Radio_DE",
        source_metadata={"file_path": "uploads/x.xlsx"},
        approved_layout={},
        build_fresh=build_fresh,
        store=store,
    )
    assert calls["n"] == 1
    assert cached1 is False
    assert cached2 is True
    assert fp1 == fp2
    assert i1 == i2


def test_build_context_packet_records_fingerprint_on_job(context_flow_job, context_flow_workbook, tmp_path, monkeypatch):
    """Packet build persists artifacts and exposes context_fingerprint."""
    from sia.agent.job_manager import JobManager
    from tests.integration.conftest import demarcation_blocks_to_layout_registry, ui_demarcation_blocks

    monkeypatch.setenv("SCHEMA_AGENT_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    manager = JobManager()
    job = manager.create_job(
        "ctx-art",
        "media_with_metadata.xlsx",
        str(context_flow_workbook),
        sheets=["Sheet1"],
    )
    source_id = manager.get_source_id(job["id"], "Sheet1")
    job = manager.get_job(job["id"])
    layout_rows = demarcation_blocks_to_layout_registry(source_id, "Sheet1", ui_demarcation_blocks())
    manager.save_layout_registry(job["id"], layout_rows, source_id=source_id)
    job = manager.get_job(job["id"])
    packet = build_context_packet(job, target_template={"properties": {"date": {}}}, selected_sheet="Sheet1")
    assert packet.get("context_fingerprint")
    assert job.get("context_artifact_cache", {}).get(source_id)
    scoped = packet.get("interpreted_context", {}).get("scoped_fields", {})
    assert scoped.get("market", {}).get("scope") == "block"
