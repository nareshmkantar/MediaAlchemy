"""Phase 2.5: context debug API payload."""

from __future__ import annotations

from sia.context.debug import build_source_context_debug_payload


def test_build_source_context_debug_payload_shape(context_flow_job):
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    source_id = job["source_registry"][0]["source_id"]
    payload = build_source_context_debug_payload(
        job,
        source_id,
        target_template=template,
    )
    assert payload["source_id"] == source_id
    assert payload["local_context"]
    assert isinstance(payload["scoped_fields"], list)
    assert isinstance(payload["interpreted_context"]["evidence"], list)
    assert payload.get("context_fingerprint")
