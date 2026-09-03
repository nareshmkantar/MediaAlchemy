"""Tests for per-source context isolation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from sia.integrity.context_isolation import (
    align_output_metrics_to_clean_template,
    apply_local_context_dimensions,
    assess_source_context_isolation,
    check_context_packet_lineage,
    has_context_packet_lineage,
    infer_tab_scope_fields,
    local_context_fields,
    rebind_source_local_plan_literals,
)


def test_infer_tab_scope_fields_cp07_sheets():
    assert infer_tab_scope_fields("Digital_UK") == {"channel": "digital", "market": "UK"}
    assert infer_tab_scope_fields("Radio_DE") == {"channel": "radio", "market": "DE"}
    assert infer_tab_scope_fields("TV_FR")["channel"] == "TV"


def test_infer_tab_scope_fields_does_not_invent_channel_from_country_prefix():
    """Geography-first tabs must not invent channel=UK/US (or market=Nation_Spend)."""
    assert infer_tab_scope_fields("UK_Nation_Spend") == {}
    assert infer_tab_scope_fields("US_State_Spend") == {}
    assert infer_tab_scope_fields("Germany_TV_Spend") == {}
    assert "channel" not in infer_tab_scope_fields("Pepsi_Max_Spend")


def test_infer_tab_scope_fields_ignores_non_geo_suffix():
    """Suffixes like Continuation must not become market=Continuation."""
    assert infer_tab_scope_fields("Digital_Continuation") == {"channel": "digital"}
    assert "market" not in infer_tab_scope_fields("Digital_Continuation")


def test_rebind_plan_literals_from_local_context():
    cp = {
        "lineage": {"sheet_name": "Radio_DE", "source_id": "job:file:Radio_DE"},
        "interpreted_context": {
            "fields": {"market": "Germany", "channel": "radio"},
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "Germany",
                    "scope": "block",
                    "confidence": 1.0,
                    "source_id": "job:file:Radio_DE",
                },
                "channel": {
                    "name": "channel",
                    "value": "radio",
                    "scope": "block",
                    "confidence": 1.0,
                    "source_id": "job:file:Radio_DE",
                },
            },
        },
    }
    tools = [
        {
            "tool": "transform.add_column",
            "params": {"target_column": "channel", "value": "digital", "when": "missing"},
        },
        {
            "tool": "transform.add_column",
            "params": {"target_column": "market", "value": "UK", "when": "missing"},
        },
    ]
    rebound, actions = rebind_source_local_plan_literals(tools, cp)
    assert rebound[0]["params"]["value"] == "radio"
    assert rebound[1]["params"]["value"] == "Germany"
    assert actions


def test_apply_local_context_dimensions_stamps_columns():
    df = pd.DataFrame({"date": ["2025-03-01"], "publisher": ["SiteC"], "spends": [258]})
    cp = {
        "lineage": {"sheet_name": "Radio_DE"},
        "interpreted_context": {
            "scoped_fields": {
                "channel": {
                    "name": "channel",
                    "value": "radio",
                    "scope": "block",
                    "confidence": 1.0,
                },
                "market": {
                    "name": "market",
                    "value": "DE",
                    "scope": "block",
                    "confidence": 1.0,
                },
            },
        },
    }
    out = apply_local_context_dimensions(df, cp)
    assert out is not None
    assert out["channel"].iloc[0] == "radio"
    assert out["market"].iloc[0] == "DE"


def test_apply_local_context_dimensions_does_not_stamp_from_tab_name_only():
    """Tab hints stay in planner context; finalize stamp requires block-level confidence."""
    df = pd.DataFrame(
        {
            "date": ["2025-01-06"],
            "channel": ["TV"],
            "market": ["UK"],
            "publisher": ["Meta"],
            "spends": [100],
        }
    )
    cp = {"lineage": {"sheet_name": "Digital_Continuation", "source_id": "job:file:Digital_Continuation"}}
    out = apply_local_context_dimensions(df, cp)
    assert out is not None
    assert list(out["channel"]) == ["TV"]
    assert list(out["market"]) == ["UK"]


def test_apply_local_context_dimensions_preserves_row_channels_for_geo_tabs():
    """UK_Nation_Spend must not overwrite media channels with tab inventiveness."""
    df = pd.DataFrame(
        {
            "date": ["2025-01-06", "2025-01-13"],
            "channel": ["TV", "Radio"],
            "market": ["England", "Scotland"],
            "publisher": ["Meta", "Google"],
            "spends": [100, 200],
        }
    )
    cp = {
        "lineage": {"sheet_name": "UK_Nation_Spend", "source_id": "job:file:UK"},
        "interpreted_context": {
            "fields": {"market": "United Kingdom"},
            "evidence": [
                {
                    "field": "market",
                    "value": "United Kingdom",
                    "line": "Country | United Kingdom",
                    "scope": "block",
                    "confidence": 1.0,
                }
            ],
        },
    }
    out = apply_local_context_dimensions(df, cp)
    assert out is not None
    assert list(out["channel"]) == ["TV", "Radio"]
    assert list(out["market"]) == ["England", "Scotland"]


def test_align_normalizes_publisher_whitespace():
    job_id = "norm-test"
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "radio_clean.xlsx"
        clean = pd.DataFrame(
            {
                "date": ["2025-03-03"],
                "publisher": ["SiteB"],
                "spends": [180.0],
                "impressions": [1154.0],
            }
        )
        clean.to_excel(path, index=False)
        source_id = f"{job_id}:file_001:Radio_DE"
        job = {"materialized_clean_templates": {source_id: {"path": str(path)}}}
        df = pd.DataFrame(
            {
                "date": ["2025-03-03"],
                "publisher": [" SiteB "],
                "spends": [937.0],
                "impressions": [5274.0],
            }
        )
        fixed = align_output_metrics_to_clean_template(df, job, source_id)
        assert fixed is not None
        assert float(fixed["spends"].iloc[0]) == 180.0
        assert float(fixed["impressions"].iloc[0]) == 1154.0


def test_assess_metrics_pass_after_align_and_stamp():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "radio_clean.xlsx"
        pd.DataFrame(
            {
                "date": ["2025-03-03"],
                "publisher": ["SiteB"],
                "spends": [180.0],
                "impressions": [1154.0],
            }
        ).to_excel(path, index=False)
        source_id = "job:file:Radio_DE"
        job = {"materialized_clean_templates": {source_id: {"path": str(path)}}}
        cp = {
            "lineage": {"sheet_name": "Radio_DE", "source_id": source_id},
            "interpreted_context": {
                "scoped_fields": {
                    "channel": {
                        "name": "channel",
                        "value": "radio",
                        "scope": "block",
                        "confidence": 1.0,
                    },
                    "market": {
                        "name": "market",
                        "value": "DE",
                        "scope": "block",
                        "confidence": 1.0,
                    },
                },
            },
        }
        bleed = pd.DataFrame(
            {
                "date": ["2025-03-03"],
                "publisher": ["SiteB"],
                "channel": ["digital"],
                "market": ["UK"],
                "spends": [937.0],
                "impressions": [5274.0],
            }
        )
        aligned = align_output_metrics_to_clean_template(bleed, job, source_id)
        stamped = apply_local_context_dimensions(aligned, cp)
        report = assess_source_context_isolation(stamped, cp, job=job, source_id=source_id)
        assert not any(v.get("type") == "metric_mismatch" for v in report["violations"])
        assert report["pass"] is True


def test_align_output_metrics_to_clean_template_restores_radio_de():
    job_id = "b4605910"
    base = Path(f"runtime/cleaned_templates/{job_id}")
    if not base.exists():
        return
    source_id = f"{job_id}:file_001:Radio_DE"
    job = {
        "materialized_clean_templates": {
            source_id: {
                "path": str(base / f"{job_id}_file_001_Radio_DE_clean.xlsx"),
            }
        }
    }
    # Simulated bleed: Digital_UK metrics + wrong dimensions
    df = pd.DataFrame(
        {
            "date": ["2025-03-01"],
            "publisher": ["SiteC"],
            "channel": ["digital"],
            "market": ["UK"],
            "spends": [258],
            "impressions": [3030],
        }
    )
    fixed = align_output_metrics_to_clean_template(df, job, source_id)
    assert fixed is not None
    assert float(fixed["spends"].iloc[0]) == 230.0
    assert float(fixed["impressions"].iloc[0]) == 2270.0


def test_assess_detects_dimension_mismatch():
    df = pd.DataFrame(
        {
            "date": ["2025-03-01"],
            "publisher": ["SiteC"],
            "channel": ["digital"],
            "market": ["UK"],
            "spends": [230],
            "impressions": [2270],
        }
    )
    cp = {"lineage": {"sheet_name": "Radio_DE"}}
    report = assess_source_context_isolation(df, cp)
    assert report["pass"] is False
    assert any(v["type"] == "dimension_mismatch" for v in report["violations"])


def test_local_context_fields_prefers_context_blocks_over_tab():
    cp = {
        "lineage": {"sheet_name": "Radio_DE"},
        "context_block_snippets": [
            {
                "text_preview": ["Market: Germany", "Channel_Context: radio"],
            }
        ],
        "interpreted_context": {"fields": {}},
    }
    fields = local_context_fields(cp)
    assert fields.get("market") == "Germany"
    assert fields.get("channel") == "radio"


def test_check_context_packet_lineage_missing_sheet():
    violations = check_context_packet_lineage({})
    assert len(violations) == 1
    assert violations[0]["type"] == "context_lineage_missing"
    assert not has_context_packet_lineage({})


def test_check_context_packet_lineage_ok_with_sheet_name():
    cp = {"lineage": {"sheet_name": "Radio_DE", "source_id": "job:file:Radio_DE"}}
    assert not check_context_packet_lineage(cp)
    assert has_context_packet_lineage(cp)


def test_finalize_order_align_then_stamp_fixes_bleed():
    """Align metrics first, then stamp dimensions (Phase 0.3)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "radio_clean.xlsx"
        pd.DataFrame(
            {
                "date": ["2025-03-01"],
                "publisher": ["SiteC"],
                "spends": [230.0],
                "impressions": [2270.0],
            }
        ).to_excel(path, index=False)
        source_id = "job:file:Radio_DE"
        job = {"materialized_clean_templates": {source_id: {"path": str(path)}}}
        cp = {
            "lineage": {"sheet_name": "Radio_DE", "source_id": source_id},
            "interpreted_context": {
                "scoped_fields": {
                    "channel": {
                        "name": "channel",
                        "value": "radio",
                        "scope": "block",
                        "confidence": 1.0,
                    },
                    "market": {
                        "name": "market",
                        "value": "DE",
                        "scope": "block",
                        "confidence": 1.0,
                    },
                },
            },
        }
        bleed = pd.DataFrame(
            {
                "date": ["2025-03-01"],
                "publisher": ["SiteC"],
                "channel": ["digital"],
                "market": ["UK"],
                "spends": [258.0],
                "impressions": [3030.0],
            }
        )
        aligned = align_output_metrics_to_clean_template(bleed, job, source_id)
        stamped = apply_local_context_dimensions(aligned, cp)
        assert stamped is not None
        assert stamped["market"].iloc[0] == "DE"
        assert stamped["channel"].iloc[0] == "radio"
        assert float(stamped["spends"].iloc[0]) == 230.0
        report = assess_source_context_isolation(stamped, cp, job=job, source_id=source_id)
        assert report["pass"] is True


def test_apply_local_context_dimensions_idempotent():
    """Phase 10.5: double stamp must not drift dimensions or metrics."""
    df = pd.DataFrame(
        {
            "date": ["2025-03-01"],
            "publisher": ["SiteC"],
            "channel": ["digital"],
            "market": ["UK"],
            "spends": [258.0],
            "impressions": [3030.0],
        }
    )
    cp = {
        "lineage": {"sheet_name": "Radio_DE", "source_id": "job:file:Radio_DE"},
        "interpreted_context": {
            "fields": {"market": "DE", "channel": "radio"},
            "scoped_fields": {
                "market": {
                    "name": "market",
                    "value": "DE",
                    "scope": "block",
                    "confidence": 1.0,
                    "source_id": "job:file:Radio_DE",
                },
                "channel": {
                    "name": "channel",
                    "value": "radio",
                    "scope": "block",
                    "confidence": 1.0,
                    "source_id": "job:file:Radio_DE",
                },
            },
        },
    }
    once = apply_local_context_dimensions(df.copy(), cp)
    twice = apply_local_context_dimensions(once.copy(), cp)
    assert once is not None and twice is not None
    assert list(once["market"]) == list(twice["market"]) == ["DE"]
    assert list(once["channel"]) == list(twice["channel"]) == ["radio"]
    assert float(once["spends"].iloc[0]) == float(twice["spends"].iloc[0])
    assert float(once["impressions"].iloc[0]) == float(twice["impressions"].iloc[0])


