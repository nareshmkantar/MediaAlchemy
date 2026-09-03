"""Tests for eval display rollup."""

from __future__ import annotations

from sia.evals.display import build_active_context_scope, build_eval_display, compute_context_health


def test_compute_context_health_bleed_and_pass_pct():
    job = {
        "source_registry": [
            {"source_id": "a", "sheet_name": "S1"},
            {"source_id": "b", "sheet_name": "S2"},
        ],
        "pipeline_evals": {
            "per_source": {
                "a": {
                    "context_isolation": {
                        "pass": True,
                        "violations": [],
                        "metrics": {"scoped_local_context": {"market": {"hop": 0}}},
                    }
                },
                "b": {
                    "context_isolation": {
                        "pass": False,
                        "violations": [{"type": "dimension_mismatch"}],
                        "metrics": {"scoped_local_context": {"market": {"hop": 1}}},
                    }
                },
            }
        },
        "context_diff_log": [{"hop": 2, "field": "market"}],
    }
    h = compute_context_health(job)
    assert h["sources_total"] == 2
    assert h["sources_passing"] == 1
    assert h["pass_pct"] == 50
    assert h["bleed_count"] == 1
    assert h["max_hop"] == 2


def test_build_active_context_scope_from_packet():
    job = {
        "context_packet": {
            "lineage": {"source_id": "j1:file:Radio_DE", "sheet_name": "Radio_DE"},
            "interpreted_context": {"fields": {"market": "DE", "channel": "radio"}},
        },
        "ux_stepper_summary": {"current_source_id": "j1:file:Radio_DE"},
    }
    scope = build_active_context_scope(job)
    assert scope["sheet_name"] == "Radio_DE"
    assert scope["chip"] == "Radio_DE"
    assert scope["local_context"].get("market") == "DE"


def test_build_eval_display_includes_context_health():
    job = {"filename": "x.xlsx", "status": "processing"}
    d = build_eval_display(job)
    assert "context_health" in d
    assert d["context_health"]["pass_pct"] == 100


def test_build_eval_display_blocked_context_bleed():
    job = {
        "filename": "CP-07.xlsx",
        "status": "awaiting_review",
        "source_registry": [
            {"source_id": "j1:file:Radio_DE", "sheet_name": "Radio_DE", "file_name": "CP-07.xlsx"},
        ],
        "pipeline_evals": {
            "critical_gate": {"pass": False, "blocked_reasons": ["dimension_mismatch:j1:file:Radio_DE"]},
            "per_source": {
                "j1:file:Radio_DE": {
                    "context_isolation": {
                        "pass": False,
                        "violations": [
                            {
                                "type": "dimension_mismatch",
                                "message": "channel expected radio, got digital",
                            }
                        ],
                    }
                }
            },
        },
    }
    d = build_eval_display(job)
    assert d["verdict"] == "blocked"
    assert d["export_safe"] is False
    assert "Radio_DE" in d["headline"]
    assert d["counts"]["critical"] >= 1
    assert "open_review" in d["recommended_actions"]


def test_build_eval_display_pending_when_no_pipeline():
    job = {"filename": "new.xlsx", "status": "processing"}
    d = build_eval_display(job)
    assert d["verdict"] == "pending"
