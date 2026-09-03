"""Tests for ScopedField and build_scoped_fields (Phase 1.2–1.4)."""

from __future__ import annotations

from sia.agent.context_packet import CONTEXT_PACKET_SCHEMA_VERSION, ContextPacket, build_context_packet
from sia.context.scoped_field import ScopedField, merge_scoped_field, scoped_fields_from_dict, scoped_fields_to_dict
from sia.integrity.context_isolation import build_scoped_fields, local_scoped_fields


def test_scoped_field_round_trip():
    sf = ScopedField(
        name="market",
        value="Germany",
        scope="block",
        sheet_name="Radio_DE",
        block_label="Top meta",
        evidence_line="Market: Germany",
        confidence=1.0,
        hop=0,
    )
    restored = ScopedField.from_dict(sf.to_dict())
    assert restored.name == "market"
    assert restored.value == "Germany"
    assert restored.scope == "block"
    assert "block" in restored.provenance_summary()


def test_merge_scoped_field_block_beats_tab():
    fields: dict = {}
    merge_scoped_field(
        fields,
        ScopedField(name="market", value="DE", scope="sheet", confidence=0.7),
    )
    merge_scoped_field(
        fields,
        ScopedField(name="market", value="Germany", scope="block", confidence=1.0),
    )
    assert fields["market"].value == "Germany"
    assert fields["market"].scope == "block"


def test_build_scoped_fields_block_beats_tab_inference():
    scoped = build_scoped_fields(
        {"fields": {}, "evidence": []},
        [{"block_label": "Meta", "text_preview": ["Market: Germany", "Channel: radio"]}],
        "Radio_DE",
        source_id="job:file:Radio_DE",
    )
    assert scoped["market"].value == "Germany"
    assert scoped["market"].scope == "block"
    assert scoped["channel"].value == "radio"


def test_local_scoped_fields_uses_stored_packet_scoped_fields():
    cp = {
        "interpreted_context": {
            "fields": {"market": "UK"},
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "DE",
                    "scope": "sheet",
                    "sheet_name": "Radio_DE",
                    "evidence_line": "tab:Radio_DE",
                    "confidence": 0.7,
                    "hop": 0,
                }
            },
        },
    }
    scoped = local_scoped_fields(cp)
    assert scoped["market"].value == "DE"
    from_dict = scoped_fields_from_dict(cp["interpreted_context"]["scoped_fields"])
    assert from_dict["market"].scope == "sheet"


def test_build_context_packet_sets_schema_version():
    packet = build_context_packet(
        {"id": "j1", "source_registry": [{"source_id": "s1", "sheet_name": "S", "file_name": "f.xlsx"}]},
        target_template={},
        selected_sheet="S",
        selected_source_id="s1",
    )
    assert packet.get("_schema_version") == CONTEXT_PACKET_SCHEMA_VERSION
    model = ContextPacket.from_dict(packet)
    assert model._schema_version == CONTEXT_PACKET_SCHEMA_VERSION


def test_scoped_fields_to_dict_keys():
    scoped = {
        "market": ScopedField(name="market", value="UK", scope="block"),
    }
    out = scoped_fields_to_dict(scoped)
    assert out["market"]["value"] == "UK"


def test_append_tab_inference_evidence_adds_sheet_scope_rows():
    from sia.integrity.context_isolation import append_tab_inference_evidence

    scoped = {
        "market": ScopedField(
            name="market",
            value="DE",
            scope="sheet",
            sheet_name="Radio_DE",
            source_id="s-radio",
            evidence_line="tab:Radio_DE",
        ),
    }
    ic = append_tab_inference_evidence({"fields": {"market": "DE"}, "evidence": []}, scoped)
    assert len(ic["evidence"]) == 1
    row = ic["evidence"][0]
    assert row["scope"] == "sheet"
    assert row["line"] == "tab:Radio_DE"
    assert row["field"] == "market"
