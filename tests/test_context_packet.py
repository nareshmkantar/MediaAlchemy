import pandas as pd

from sia.agent.context_packet import (
    ContextPacket,
    apply_context_to_dataframe,
    build_canonical_planning_view,
    build_context_packet,
    compute_date_granularity_alignment,
    derive_date_context_from_mappings,
    effective_target_date_granularity,
    normalize_mapping_records,
)
from sia.agent.job_manager import JobManager


def test_build_context_packet_summarizes_current_job_context():
    job = {
        "id": "job_123",
        "filename": "HS_Media_Q3.xlsx",
        "file_path": "uploads/HS_Media_Q3.xlsx",
        "source_registry": [
            {
                "source_id": "src_001",
                "file_name": "HS_Media_Q3.xlsx",
                "file_path": "uploads/HS_Media_Q3.xlsx",
                "sheet_name": "Healthy Snack model",
                "source_type": "paid_media",
                "variable_type": "Paid Media impressions",
                "modeling_period_start": "2025-07-01",
                "modeling_period_end": "2025-09-30",
                "uid": ["date", "market", "brand", "channel", "publisher_name"],
                "date_granularity": "daily",
                "aggregation_logic": "impressions: sum\nNever average impressions.",
                "user_notes": ["Use execution date, not invoice date"],
            }
        ],
        "scoped_source": {
            "sheet_name": "Healthy Snack model",
            "header_row": 2,
            "scope_type": "demarcated_table",
            "analysis_bounds": {"start_row": 2, "end_row": 40, "start_col": 0, "end_col": 4},
        },
        "mapping_registry": [
            {
                "source_column": "Event Date",
                "target_column": "date_paid_media",
                "decision": "Keep",
                "target_match_confidence": 0.99,
            },
            {
                "source_column": "Total Cost",
                "target_column": "total_cost_paid_media",
                "decision": "Keep",
                "target_match_confidence": 0.98,
            },
            {
                "source_column": "Targeting Value",
                "target_column": "No match",
                "decision": "Discard",
                "target_match_confidence": 0.75,
            },
        ],
        "business_rules_registry": [
            {
                "target_column": "date_paid_media",
                "rule_type": "format",
                "rule_expression": "yyyy-mm-dd",
            },
            {
                "target_column": "clicks_paid_media",
                "rule_type": "fill_blank",
                "rule_expression": "0",
            },
        ],
        "user_notes": ["Healthy Snack only"],
    }
    target_template = {
        "x_scope": {
            "variable_type": "Paid Media impressions",
            "uid_hierarchy": ["date_paid_media"],
            "metrics": ["total_cost_paid_media", "clicks_paid_media"],
            "supporting_columns": [],
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {},
        "properties": {
            "date_paid_media": {"type": "string"},
            "total_cost_paid_media": {"type": "number"},
            "clicks_paid_media": {"type": "integer"},
        },
    }

    packet = build_context_packet(job, target_template=target_template, selected_sheet="Healthy Snack model")
    summary = ContextPacket(**packet).planning_summary()

    assert packet["source_metadata"]["sheet_name"] == "Healthy Snack model"
    assert packet["source_metadata"]["variable_type"] == "Paid Media impressions"
    assert packet["source_metadata"]["uid"] == ["date", "market", "brand", "channel", "publisher_name"]
    assert packet["approved_layout"]["header_row"] == 2
    assert packet["unresolved_items"] == [
        {
            "target_column": "clicks_paid_media",
            "status": "missing_mapping",
            "requirement": "requires_source",
        }
    ]
    assert "Event Date -> date_paid_media" in summary["mapping_summary"]["mapped_columns"]
    assert "Targeting Value" in summary["mapping_summary"]["excluded_columns"]
    assert "date_paid_media: format -> yyyy-mm-dd" in summary["rules_summary"]
    assert summary["source_summary"]["variable_type"] == "Paid Media impressions"
    assert summary["source_summary"]["modeling_period_start"] == "2025-07-01"
    assert summary["source_summary"]["modeling_period_end"] == "2025-09-30"
    assert summary["source_summary"]["date_granularity"] == "daily"
    assert summary["source_summary"]["aggregation_logic"] == "impressions: sum\nNever average impressions."
    assert "Use execution date, not invoice date" in summary["user_notes"]
    align = summary.get("date_granularity_alignment") or {}
    assert align.get("normalized_target") in ("unknown", "daily", "")


def test_compute_date_granularity_alignment_daily_to_weekly():
    a = compute_date_granularity_alignment("daily", "weekly")
    assert a["requires_weekly_rollup_step"] is True
    assert "aggregate_weekly" in (a.get("recommended_primary_tool") or "")
    assert "aggregate_weekly" in a["planner_obligation"].lower()


def test_normalize_mapping_records_preserves_date_semantic():
    out = normalize_mapping_records(
        [
            {
                "source_column": "Start Date",
                "target_column": "date",
                "decision": "Keep",
                "date_semantic": "range_start",
            }
        ],
        source_id="src_1",
    )
    assert len(out) == 1
    assert out[0]["date_semantic"] == "range_start"


def test_derive_date_context_from_mappings_range_pair():
    derived = derive_date_context_from_mappings(
        [
            {
                "source_column": "Start Date",
                "target_column": "date",
                "decision": "Keep",
                "date_semantic": "range_start",
            },
            {
                "source_column": "End Date",
                "target_column": "date",
                "decision": "Keep",
                "date_semantic": "range_end",
            },
        ]
    )
    assert derived["date_shape"] == "period_span"
    assert derived["range_start_column"] == "Start Date"
    assert derived["range_end_column"] == "End Date"
    assert derived["date_granularity"] == "range"


def test_effective_target_date_granularity_required_key():
    assert effective_target_date_granularity({"date_granularity_required": "weekly"}) == "weekly"
    assert effective_target_date_granularity({"date_granularity": "daily"}) == "daily"


def test_planning_summary_alignment_period_span_weekly_target():
    job = {
        "id": "job_span",
        "filename": "f.xlsx",
        "file_path": "uploads/f.xlsx",
        "source_registry": [
            {
                "source_id": "src_span",
                "file_name": "f.xlsx",
                "sheet_name": "S1",
                "date_granularity": "range",
                "date_shape": "period_span",
                "range_start_column": "Flight Start",
                "range_end_column": "Flight End",
                "variable_type": "Paid Media impressions",
            }
        ],
        "scoped_source": {"sheet_name": "S1", "header_row": 0},
        "mapping_registry": [],
        "business_rules_registry": [],
    }
    tpl = {
        "x_scope": {"date_granularity": "weekly", "uid_hierarchy": ["date"], "metrics": ["spend"]},
        "properties": {"date": {"type": "string"}, "spend": {"type": "number"}},
    }
    packet = build_context_packet(job, target_template=tpl, selected_sheet="S1", selected_source_id="src_span")
    summary = ContextPacket(**packet).planning_summary()
    align = summary["date_granularity_alignment"]
    assert align["normalized_target"] == "weekly"
    assert align["normalized_date_shape"] == "period_span"
    assert "expand_date_range_to_daily" in (align.get("recommended_primary_tool") or "").lower()
    obl = (align.get("planner_obligation") or "").lower()
    assert "rename" in obl and "not" in obl


def test_planning_summary_includes_alignment_weekly_target():
    job = {
        "id": "job_g",
        "filename": "f.xlsx",
        "file_path": "uploads/f.xlsx",
        "source_registry": [
            {
                "source_id": "src_g",
                "file_name": "f.xlsx",
                "sheet_name": "S1",
                "date_granularity": "daily",
                "variable_type": "Paid Media impressions",
            }
        ],
        "scoped_source": {"sheet_name": "S1", "header_row": 0},
        "mapping_registry": [],
        "business_rules_registry": [],
    }
    tpl = {
        "x_scope": {"date_granularity": "weekly", "uid_hierarchy": ["date"], "metrics": ["spend"]},
        "properties": {"date": {"type": "string"}, "spend": {"type": "number"}},
    }
    packet = build_context_packet(job, target_template=tpl, selected_sheet="S1", selected_source_id="src_g")
    summary = ContextPacket(**packet).planning_summary()
    align = summary["date_granularity_alignment"]
    assert align["normalized_target"] == "weekly"
    assert align["normalized_source"] == "daily"
    assert align["requires_weekly_rollup_step"] is True


def test_apply_context_to_dataframe_renames_formats_and_fills_values():
    df = pd.DataFrame(
        {
            "Event Date": ["03/20/2026", None],
            "Clicks": [None, ""],
            "Total Cost": [10.5, 20.0],
        }
    )
    approved_mappings = [
        {"source_column": "Event Date", "target_column": "date_paid_media", "decision": "Keep"},
        {"source_column": "Clicks", "target_column": "clicks_paid_media", "decision": "Keep"},
        {"source_column": "Total Cost", "target_column": "total_cost_paid_media", "decision": "Keep"},
    ]
    business_rules = [
        {"target_column": "date_paid_media", "rule_type": "format", "rule_expression": "yyyy-mm-dd"},
        {"target_column": "clicks_paid_media", "rule_type": "fill_blank", "rule_expression": "0"},
        {"target_column": "creative_type_paid_media", "rule_type": "default_value", "rule_expression": "blank-social"},
    ]

    result, actions = apply_context_to_dataframe(df, approved_mappings=approved_mappings, business_rules=business_rules)

    assert list(result.columns) == [
        "date_paid_media",
        "clicks_paid_media",
        "total_cost_paid_media",
        "creative_type_paid_media",
    ]
    assert result["date_paid_media"].tolist() == ["2026-03-20", None]
    assert result["clicks_paid_media"].tolist() == [0, 0]
    assert result["creative_type_paid_media"].tolist() == ["blank-social", "blank-social"]
    assert "applied 3 approved column mappings" in actions
    assert "formatted date_paid_media" in actions


def test_job_manager_persists_context_registries_by_sheet():
    manager = JobManager()
    manager.create_job("job_001", "example.xlsx", "uploads/example.xlsx", sheets=["Main", "Notes"])

    updated = manager.update_source_metadata("job_001", "Main", {"source_type": "paid_media", "variable_type": "Paid Media impressions"})
    layouts = manager.save_layout_registry("job_001", [{"layout_id": "l1", "source_id": updated["source_id"], "sheet_name": "Main"}])
    mappings = manager.save_mapping_registry(
        "job_001",
        [{"column_name": "Spend", "target_column": "total_cost_paid_media", "decision": "Keep"}],
        sheet_name="Main",
    )
    rules = manager.save_business_rules(
        "job_001",
        [{"target_column": "clicks_paid_media", "rule_type": "fill_blank", "rule_expression": "0"}],
        sheet_name="Main",
    )

    job = manager.get_job("job_001")
    assert updated["source_type"] == "paid_media"
    assert updated["variable_type"] == "Paid Media impressions"
    assert len(layouts) == 1
    assert mappings[0]["source_id"] == updated["source_id"]
    assert rules[0]["applies_to_source_id"] == updated["source_id"]
    assert len(job["source_registry"]) == 2


def test_build_context_packet_filters_to_active_source_layout_and_rules():
    job = {
        "id": "job_scope",
        "filename": "example.xlsx",
        "file_path": "uploads/example.xlsx",
        "source_registry": [
            {"source_id": "example:Main", "file_name": "example.xlsx", "file_path": "uploads/example.xlsx", "sheet_name": "Main"},
            {"source_id": "example:Notes", "file_name": "example.xlsx", "file_path": "uploads/example.xlsx", "sheet_name": "Notes"},
        ],
        "scoped_source": {
            "sheet_name": "Main",
            "header_row": 1,
            "scope_type": "demarcated_table",
            "analysis_bounds": {"start_row": 1, "end_row": 20, "start_col": 0, "end_col": 5},
        },
        "layout_registry": [
            {"source_id": "example:Main", "sheet_name": "Main", "block_id": "b1", "block_category": "main_data", "decision": "Keep"},
            {"source_id": "example:Main", "sheet_name": "Main", "block_id": "b2", "block_category": "metadata", "decision": "Context"},
            {"source_id": "example:Main", "sheet_name": "Main", "block_id": "b3", "block_category": "noise", "decision": "Discard"},
            {"source_id": "example:Notes", "sheet_name": "Notes", "block_id": "b4", "block_category": "main_data", "decision": "Keep"},
        ],
        "mapping_registry": [
            {"source_id": "example:Main", "source_column": "Spend", "target_column": "total_cost_paid_media", "decision": "Keep"},
            {"source_id": "example:Main", "source_column": "QA Note", "target_column": "No match", "decision": "Discard"},
            {"source_id": "example:Notes", "source_column": "Ignore Me", "target_column": "note_field", "decision": "Keep"},
        ],
        "business_rules_registry": [
            {"applies_to_source_id": "example:Main", "target_column": "date_paid_media", "rule_type": "format", "rule_expression": "yyyy-mm-dd"},
            {"applies_to_source_id": "example:Notes", "target_column": "note_field", "rule_type": "default_value", "rule_expression": "n/a"},
        ],
    }

    packet = build_context_packet(job, target_template={"properties": {}}, selected_sheet="Main")

    assert [item["source_column"] for item in packet["approved_mappings"]] == ["Spend", "QA Note"]
    assert [item["target_column"] for item in packet["business_rules"]] == ["date_paid_media"]
    assert [item["block_id"] for item in packet["approved_layout"]["main_blocks"]] == ["b1"]
    assert [item["block_id"] for item in packet["approved_layout"]["context_blocks"]] == ["b2"]
    summary = ContextPacket(**packet).planning_summary()
    assert summary["layout_summary"]["main_blocks_count"] == 1
    assert summary["layout_summary"]["context_blocks_count"] == 1
    assert summary["layout_summary"]["context_block_labels"] == ["b2"]
    assert all(item["block_id"] != "b3" for item in packet["approved_layout"]["blocks"])
    assert all(item["block_id"] != "b4" for item in packet["approved_layout"]["blocks"])


def test_build_context_packet_extracts_approved_context_block_snippets(tmp_path):
    file_path = tmp_path / "context-sheet.xlsx"
    pd.DataFrame(
        [
            ["Modeling Period", "2025-01-01 to 2025-03-31", "", ""],
            ["Publisher", "Instagram", "", ""],
            ["Date", "Spend", "Channel", "Publisher"],
            ["2025-01-01", 100, "social", "Instagram"],
        ]
    ).to_excel(file_path, index=False, header=False)

    job = {
        "id": "job_ctx",
        "filename": "context-sheet.xlsx",
        "file_path": str(file_path),
        "source_registry": [
            {
                "source_id": "ctx:Sheet1",
                "file_name": "context-sheet.xlsx",
                "file_path": str(file_path),
                "sheet_name": "Sheet1",
            }
        ],
        "layout_registry": [
            {
                "source_id": "ctx:Sheet1",
                "sheet_name": "Sheet1",
                "block_id": "meta_1",
                "block_label": "Top metadata",
                "block_category": "metadata",
                "decision": "Context",
                "start_row": 0,
                "end_row": 1,
                "start_col": 0,
                "end_col": 1,
            },
            {
                "source_id": "ctx:Sheet1",
                "sheet_name": "Sheet1",
                "block_id": "main_1",
                "block_category": "main_data",
                "decision": "Keep",
                "start_row": 2,
                "end_row": 3,
                "start_col": 0,
                "end_col": 3,
                "header_row": 2,
            },
        ],
        "scoped_source": {
            "sheet_name": "Sheet1",
            "header_row": 2,
            "scope_type": "demarcated_table",
            "analysis_bounds": {"start_row": 2, "end_row": 3, "start_col": 0, "end_col": 3},
        },
    }

    packet = build_context_packet(job, target_template={"properties": {}}, selected_sheet="Sheet1", selected_source_id="ctx:Sheet1")
    summary = ContextPacket(**packet).planning_summary()

    assert len(packet["context_block_snippets"]) == 1
    snippet = packet["context_block_snippets"][0]
    assert snippet["block_id"] == "meta_1"
    assert "Modeling Period" in snippet["text_preview"][0]
    assert summary["layout_summary"]["context_block_snippets_count"] == 1
    assert "Modeling Period" in summary["layout_summary"]["context_block_preview"][0]["text_preview"][0]
    assert summary["interpreted_context"]["fields"]["modeling_period_start"] == "2025-01-01"
    assert summary["interpreted_context"]["fields"]["modeling_period_end"] == "2025-03-31"


def test_build_context_packet_interprets_context_fields_into_effective_source_summary(tmp_path):
    file_path = tmp_path / "context-fields.xlsx"
    pd.DataFrame(
        [
            ["Publisher", "Instagram", "", ""],
            ["Market", "US", "", ""],
            ["Date Granularity", "Weekly", "", ""],
            ["Aggregation Logic", "Never average impressions", "", ""],
            ["Date", "Spend", "Channel", "Publisher"],
            ["2025-01-01", 100, "social", "Instagram"],
        ]
    ).to_excel(file_path, index=False, header=False)

    job = {
        "id": "job_ctx_fields",
        "filename": "context-fields.xlsx",
        "file_path": str(file_path),
        "source_registry": [
            {
                "source_id": "ctx:Sheet1",
                "file_name": "context-fields.xlsx",
                "file_path": str(file_path),
                "sheet_name": "Sheet1",
            }
        ],
        "layout_registry": [
            {
                "source_id": "ctx:Sheet1",
                "sheet_name": "Sheet1",
                "block_id": "meta_1",
                "block_label": "Top metadata",
                "block_category": "metadata",
                "decision": "Context",
                "start_row": 0,
                "end_row": 3,
                "start_col": 0,
                "end_col": 1,
            },
            {
                "source_id": "ctx:Sheet1",
                "sheet_name": "Sheet1",
                "block_id": "main_1",
                "block_category": "main_data",
                "decision": "Keep",
                "start_row": 4,
                "end_row": 5,
                "start_col": 0,
                "end_col": 3,
                "header_row": 4,
            },
        ],
        "scoped_source": {
            "sheet_name": "Sheet1",
            "header_row": 4,
            "scope_type": "demarcated_table",
            "analysis_bounds": {"start_row": 4, "end_row": 5, "start_col": 0, "end_col": 3},
        },
    }

    packet = build_context_packet(job, target_template={"properties": {}}, selected_sheet="Sheet1", selected_source_id="ctx:Sheet1")
    summary = ContextPacket(**packet).planning_summary()

    assert summary["interpreted_context"]["fields"]["publisher"] == "Instagram"
    assert summary["interpreted_context"]["fields"]["market"] == "US"
    assert summary["interpreted_context"]["fields"]["date_granularity"] == "weekly"
    assert summary["interpreted_context"]["fields"]["aggregation_logic"] == "Never average impressions"
    assert summary["source_summary"]["publisher"] == "Instagram"
    assert summary["source_summary"]["market"] == "US"
    assert summary["source_summary"]["date_granularity"] == "weekly"


def test_job_manager_remove_job_clears_related_state():
    manager = JobManager()
    manager.create_job("job_delete", "example.xlsx", "uploads/example.xlsx", sheets=["Main"])
    manager.review_queue["job_delete"] = {"job_id": "job_delete", "status": "pending"}
    manager.pending_deletions["job_delete"] = {"job_id": "job_delete"}
    manager.pending_schema_mappings["job_delete"] = [{"source_column": "Spend"}]
    manager.pending_demarcations["job_delete"] = {"blocks": []}
    manager.pending_checkpoints["cp_1"] = {"checkpoint_id": "cp_1", "job_id": "job_delete"}

    removed = manager.remove_job("job_delete")

    assert removed["id"] == "job_delete"
    assert manager.get_job("job_delete") is None
    assert "job_delete" not in manager.review_queue
    assert "job_delete" not in manager.pending_deletions
    assert "job_delete" not in manager.pending_schema_mappings
    assert "job_delete" not in manager.pending_demarcations
    assert "cp_1" not in manager.pending_checkpoints


def test_job_manager_cancel_job_marks_job_and_resolves_checkpoints():
    manager = JobManager()
    manager.create_job("job_cancel", "example.xlsx", "uploads/example.xlsx", sheets=["Main"])
    manager.pending_checkpoints["cp_cancel"] = {"checkpoint_id": "cp_cancel", "job_id": "job_cancel", "status": "pending"}

    cancelled = manager.cancel_job("job_cancel", "User cancelled planner review")

    assert cancelled["status"] == "cancelled"
    assert manager.get_job("job_cancel")["review_status"] == "cancelled"
    assert "cp_cancel" not in manager.pending_checkpoints


def test_job_manager_add_data_file_creates_stable_multi_source_registry():
    manager = JobManager()
    manager.create_job("job_multi", "meta.xlsx", "uploads/meta.xlsx", sheets=["Meta"])

    added = manager.add_data_file("job_multi", "youtube.xlsx", "uploads/youtube.xlsx", sheets=["YT", "Summary"])
    job = manager.get_job("job_multi")

    assert added["file_id"] == "file_002"
    assert len(job["data_files"]) == 2
    assert any(source["source_id"].startswith("job_multi:file_002:") for source in job["source_registry"])


def test_build_context_packet_includes_other_source_summaries_for_relationship_review():
    job = {
        "id": "job_multi",
        "filename": "meta.xlsx +1 more",
        "file_path": "uploads/meta.xlsx",
        "source_registry": [
            {
                "source_id": "job_multi:file_001:Meta",
                "file_id": "file_001",
                "file_name": "meta.xlsx",
                "file_path": "uploads/meta.xlsx",
                "sheet_name": "Meta",
                "variable_type": "Paid Media impressions",
                "uid": ["date", "channel", "publisher_name"],
                "date_granularity": "daily",
                "contains_main_data": True,
            },
            {
                "source_id": "job_multi:file_002:YT",
                "file_id": "file_002",
                "file_name": "youtube.xlsx",
                "file_path": "uploads/youtube.xlsx",
                "sheet_name": "YT",
                "variable_type": "Paid Media impressions",
                "uid": ["date", "channel", "publisher_name"],
                "date_granularity": "daily",
                "contains_main_data": True,
            },
        ],
        "mapping_registry": [
            {"source_id": "job_multi:file_001:Meta", "source_column": "Date", "target_column": "date", "decision": "Keep"},
            {"source_id": "job_multi:file_001:Meta", "source_column": "Impressions", "target_column": "impressions", "decision": "Keep"},
            {"source_id": "job_multi:file_002:YT", "source_column": "Date", "target_column": "date", "decision": "Keep"},
            {"source_id": "job_multi:file_002:YT", "source_column": "Impressions", "target_column": "impressions", "decision": "Keep"},
        ],
        "approved_file_relationships": [{"relationship_id": "r1", "status": "approved"}],
    }

    packet = build_context_packet(
        job,
        target_template={
            "x_scope": {"uid_hierarchy": ["date"], "metrics": ["impressions"], "supporting_columns": []},
            "business_logic": {"column_rules": []},
            "aggregation_logic": {},
            "properties": {"date": {"type": "string"}, "impressions": {"type": "integer"}},
        },
        selected_source_id="job_multi:file_001:Meta",
    )

    assert packet["source_metadata"]["source_id"] == "job_multi:file_001:Meta"
    assert len(packet["available_source_summaries"]) == 2
    assert packet["file_relationships"][0]["relationship_id"] == "r1"


def test_build_context_packet_fills_modeling_period_from_template_when_source_missing_dates():
    job = {
        "id": "job_modeling",
        "filename": "f.xlsx",
        "file_path": "x/f.xlsx",
        "source_registry": [
            {
                "source_id": "s1",
                "file_name": "f.xlsx",
                "sheet_name": "Raw",
                "variable_type": "Paid Media Impressions",
            }
        ],
    }
    target_template = {
        "x_scope": {
            "variable_type": "Paid Media Impressions",
            "uid_hierarchy": ["date"],
            "metrics": ["impressions"],
            "supporting_columns": [],
            "modeling_period": {
                "start_date": "2024-01-01T00:00:00",
                "end_date": "2024-12-31",
            },
        },
        "business_logic": {"column_rules": []},
        "properties": {
            "date": {"type": "string", "format": "date"},
            "impressions": {"type": "integer"},
        },
    }
    packet = build_context_packet(job, target_template=target_template, selected_sheet="Raw")
    assert packet["source_metadata"]["modeling_period_start"] == "2024-01-01"
    assert packet["source_metadata"]["modeling_period_end"] == "2024-12-31"


def test_build_context_packet_fills_modeling_period_from_modelling_date_keys():
    """British spelling modelling_* on x_scope should merge like modeling_*."""
    job = {
        "id": "job_modelling_keys",
        "filename": "f.xlsx",
        "file_path": "x/f.xlsx",
        "source_registry": [
            {
                "source_id": "s1",
                "file_name": "f.xlsx",
                "sheet_name": "Raw",
                "variable_type": "Paid Media Impressions",
            }
        ],
    }
    target_template = {
        "x_scope": {
            "variable_type": "Paid Media Impressions",
            "uid_hierarchy": ["date"],
            "metrics": ["impressions"],
            "supporting_columns": [],
            "modelling_start_date": "2023-04-01",
            "modelling_end_date": "2025-03-31",
        },
        "business_logic": {"column_rules": []},
        "properties": {
            "date": {"type": "string", "format": "date"},
            "impressions": {"type": "integer"},
        },
    }
    packet = build_context_packet(job, target_template=target_template, selected_sheet="Raw")
    assert packet["source_metadata"]["modeling_period_start"] == "2023-04-01"
    assert packet["source_metadata"]["modeling_period_end"] == "2025-03-31"


def test_build_context_packet_fills_modeling_period_when_template_shape_invalid():
    """Shape validation can fail while x_scope still carries modeling_period / variable_type."""
    job = {
        "id": "job_bad_shape",
        "filename": "f.xlsx",
        "file_path": "x/f.xlsx",
        "source_registry": [
            {
                "source_id": "s1",
                "file_name": "f.xlsx",
                "sheet_name": "Raw",
            }
        ],
    }
    # properties keys != x_scope union → validate_template_shape is False
    target_template = {
        "x_scope": {
            "variable_type": "Paid Media Impressions",
            "uid_hierarchy": ["date"],
            "metrics": ["impressions"],
            "supporting_columns": [],
            "modeling_period": {
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
            },
        },
        "business_logic": {"column_rules": []},
        "properties": {
            "date": {"type": "string", "format": "date"},
        },
    }
    packet = build_context_packet(job, target_template=target_template, selected_sheet="Raw")
    assert packet["source_metadata"]["modeling_period_start"] == "2024-01-01"
    assert packet["source_metadata"]["modeling_period_end"] == "2024-12-31"
    assert packet["source_metadata"]["variable_type"] == "Paid Media Impressions"


def test_canonical_planning_view_merges_mapping_supplement_into_full_context():
    job = {
        "id": "job_canonical",
        "filename": "canonical.xlsx",
        "file_path": "uploads/canonical.xlsx",
        "source_registry": [
            {
                "source_id": "s1",
                "file_name": "canonical.xlsx",
                "sheet_name": "Main",
                "variable_type": "Paid Media impressions",
                "modeling_period_start": "2025-01-01",
                "modeling_period_end": "2025-03-31",
            }
        ],
        "mapping_registry": [
            {"source_column": "Spend", "target_column": "spends", "decision": "Keep"}
        ],
        "business_rules_registry": [],
        "approved_file_relationships": [{"relationship_id": "r1", "status": "approved"}],
    }
    packet = build_context_packet(job, target_template={"properties": {}}, selected_sheet="Main")

    view = build_canonical_planning_view(
        packet,
        mapping_supplement={
            "prepared_columns": ["Spend", "Date"],
            "header_derivation": {"derived": True, "message": "merged two header rows"},
            "sparse_dimension_columns": [{"source_column": "Market", "blank_ratio": 0.5}],
        },
    )

    assert view["source_summary"]["variable_type"] == "Paid Media impressions"
    assert view["source_summary"]["modeling_period_start"] == "2025-01-01"
    assert view["source_summary"]["prepared_columns"] == ["Spend", "Date"]
    assert view["source_summary"]["header_derivation"]["derived"] is True
    assert view["source_summary"]["sparse_dimension_columns"][0]["source_column"] == "Market"
    assert view["mapping_summary"]["mapped_count"] == 1
    assert view["relationship_context"]["count"] == 1
    assert view["relationship_context"]["approved"][0]["relationship_id"] == "r1"


def test_canonical_planning_view_handles_empty_packet():
    assert build_canonical_planning_view(None) == {}
    assert build_canonical_planning_view({}) == {}


def test_planning_summary_does_not_flag_supporting_targets_unresolved_when_mapped_as_metadata():
    job = {
        "id": "job_region_meta",
        "filename": "example.xlsx",
        "file_path": "uploads/example.xlsx",
        "source_registry": [
            {
                "source_id": "src_region",
                "file_name": "example.xlsx",
                "sheet_name": "Raw",
            }
        ],
        "mapping_registry": [
            {
                "source_id": "src_region",
                "source_column": "Market",
                "target_column": "region",
                "decision": "Metadata",
                "role": "supporting",
            }
        ],
        "business_rules_registry": [],
    }
    target_template = {
        "x_scope": {
            "uid_hierarchy": ["date", "channel", "publisher"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": ["region"],
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {},
        "properties": {
            "date": {"type": "string", "format": "date"},
            "channel": {"type": "string"},
            "publisher": {"type": "string"},
            "region": {"type": "string"},
            "spends": {"type": "number"},
            "impressions": {"type": "integer"},
        },
    }
    packet = build_context_packet(job, target_template=target_template, selected_sheet="Raw")
    summary = ContextPacket(**packet).planning_summary()
    assert "region" not in (summary.get("mapping_summary") or {}).get("unresolved_target_columns", [])
