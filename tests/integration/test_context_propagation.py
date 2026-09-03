"""
Integration tests: context handoff across demarcation → mapping → plan review → planner/replan.

These tests answer: when the user marks a block "Use as Metadata", registers business rules,
or resolves planner questions with notes — does that context survive into the context packet,
canonical planning view, planner prompt, and tool plan?
"""
from __future__ import annotations

import pandas as pd
import pytest

from sia.agent.base import ExtractionPlan
from sia.agent.context_packet import (
    ContextPacket,
    apply_context_to_dataframe,
    build_canonical_planning_view,
    build_context_packet,
    merge_plan_review_context_packet,
)
from sia.agent.nodes import verify_output_node
from sia.agent.planner import PlanGenerator
from sia.agent.planner_decisions import (
    apply_resolved_approvals_to_context,
    apply_resolved_approvals_to_plan,
    format_resolved_decisions_for_prompt,
    merge_resolved_approval_items,
)
from sia.agent.replanner import Replanner

from tests.integration.conftest import (
    demarcation_blocks_to_layout_registry,
    enrich_packet_for_planner,
    ui_demarcation_blocks,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "metadata_decision",
    ["Context", "context", "metadata", "use as context"],
)
def test_demarcation_use_as_metadata_routes_to_context_blocks_not_main(
    context_flow_workbook,
    context_flow_job_manager,
    metadata_decision,
):
    """UI 'Use as Metadata' (decision=Context) must not land in main_blocks."""
    manager = context_flow_job_manager
    job = manager.create_job(
        "job_meta_route",
        "media_with_metadata.xlsx",
        str(context_flow_workbook),
        sheets=["Sheet1"],
    )
    source_id = manager.get_source_id(job["id"], "Sheet1")
    job = manager.get_job(job["id"])
    job["scoped_source"] = {"sheet_name": "Sheet1", "header_row": 4}

    blocks = ui_demarcation_blocks()
    blocks[0]["decision"] = metadata_decision
    layout_rows = demarcation_blocks_to_layout_registry(source_id, "Sheet1", blocks)
    manager.save_layout_registry(job["id"], layout_rows, source_id=source_id)
    job = manager.get_job(job["id"])

    packet = build_context_packet(
        job,
        target_template={"properties": {"date": {}}},
        selected_sheet="Sheet1",
        selected_source_id=source_id,
    )
    layout = packet["approved_layout"]

    assert [b["block_id"] for b in layout["context_blocks"]] == ["meta_top"]
    assert [b["block_id"] for b in layout["main_blocks"]] == ["main_table"]
    assert layout["context_blocks"][0]["decision"].lower() in {
        metadata_decision.lower(),
        "context",
    }


def test_demarcation_metadata_reaches_context_packet_and_canonical_view(context_flow_job):
    """Metadata block cells are extracted, interpreted, and surfaced in planning_summary."""
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    packet = build_context_packet(job, target_template=template, selected_sheet="Sheet1")
    summary = ContextPacket(**packet).planning_summary()
    canonical = build_canonical_planning_view(packet)

    assert summary["layout_summary"]["context_blocks_count"] == 1
    assert summary["layout_summary"]["context_block_labels"] == ["Top metadata"]
    assert summary["layout_summary"]["main_blocks_count"] == 1
    assert len(packet["context_block_snippets"]) == 1
    assert "Publisher" in packet["context_block_snippets"][0]["text_preview"][0]
    assert summary["interpreted_context"]["fields"]["publisher"] == "Instagram"
    assert summary["interpreted_context"]["fields"]["market"] == "UK"
    assert summary["interpreted_context"]["fields"]["modeling_period_start"] == "2025-01-01"
    assert summary["interpreted_context"]["fields"]["modeling_period_end"] == "2025-03-31"
    assert summary["source_summary"]["publisher"] == "Instagram"
    assert summary["source_summary"]["market"] == "UK"
    assert summary["source_summary"]["aggregation_logic"] == "impressions: sum"
    assert "Use booking date, not invoice date" in summary["user_notes"]

    assert canonical["interpreted_context"]["fields"]["publisher"] == "Instagram"
    assert canonical["layout_summary"]["context_block_preview"][0]["label"] == "Top metadata"


def test_main_data_ai_category_with_context_decision_routes_to_context_only(
    context_flow_workbook,
    context_flow_job_manager,
):
    """CP-12 Validation_Rules: AI labels Main Data; user clicks Use as Metadata (decision=Context)."""
    manager = context_flow_job_manager
    job = manager.create_job(
        "job_validation_rules",
        "CP-12_Context_Poisoning_Test.xlsx",
        str(context_flow_workbook),
        sheets=["Validation_Rules"],
    )
    source_id = manager.get_source_id(job["id"], "Validation_Rules")
    job = manager.get_job(job["id"])
    job["scoped_source"] = {"sheet_name": "Validation_Rules", "header_row": 0}

    blocks = [
        {
            "id": "block_1",
            "label": "Block 1",
            "category": "Main Data",
            "decision": "Context",
            "coordinates": {
                "start_row": 0,
                "end_row": 2,
                "start_col": 0,
                "end_col": 3,
                "header_row": 0,
            },
        }
    ]
    layout_rows = demarcation_blocks_to_layout_registry(source_id, "Validation_Rules", blocks)
    manager.save_layout_registry(job["id"], layout_rows, source_id=source_id)
    job = manager.get_job(job["id"])

    packet = build_context_packet(
        job,
        target_template={"properties": {"date": {}}},
        selected_sheet="Validation_Rules",
        selected_source_id=source_id,
    )
    layout = packet["approved_layout"]

    assert layout["main_blocks"] == []
    assert len(layout["context_blocks"]) == 1
    assert layout["context_blocks"][0]["decision"].lower() == "context"


def test_metadata_context_included_in_planner_prompt(context_flow_job, monkeypatch):
    """Planner LLM prompt must carry snippet text and interpreted metadata fields."""
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    packet = build_context_packet(job, target_template=template, selected_sheet="Sheet1")
    packet = enrich_packet_for_planner(packet)
    captured = {}

    class DummyResponse:
        text = (
            '{"confidence": 0.9, "tool_calls": [], "business_rule_actions": [], '
            '"approval_items": [], "expected_schema": []}'
        )

    planner = PlanGenerator(llm_client=None)
    monkeypatch.setattr(planner.llm_wrapper, "generate_content", lambda prompt, **_: captured.update({"prompt": prompt}) or DummyResponse())

    planner.generate(
        structure_analysis={"tables": [], "column_analysis": [], "overall_structure": "flat", "confidence": 0.9},
        examples=[],
        target_template=template,
        context_packet=packet,
    )

    prompt = captured["prompt"]
    assert "Approved Context Block Snippets" in prompt
    assert "Top metadata" in prompt
    assert "Instagram" in prompt
    assert "Interpreted Context" in prompt
    assert '"publisher": "Instagram"' in prompt or "publisher" in prompt
    assert "Use booking date, not invoice date" in prompt


def test_business_rules_flow_from_registry_to_packet_and_planner(context_flow_job, monkeypatch):
    """Business rules registered on the job appear in rules_summary and planner prompt."""
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    packet = build_context_packet(job, target_template=template, selected_sheet="Sheet1")
    summary = ContextPacket(**packet).planning_summary()

    rules = {r["target_column"]: r for r in packet["business_rules"]}
    assert rules["date_paid_media"]["rule_type"] == "format"
    assert rules["clicks_paid_media"]["rule_type"] == "fill_blank"
    assert rules["publisher_paid_media"]["rule_type"] == "default_value"
    assert "date_paid_media: format -> yyyy-mm-dd" in summary["rules_summary"]
    assert "clicks_paid_media: fill_blank -> 0" in summary["rules_summary"]

    captured = {}

    class DummyResponse:
        text = (
            '{"confidence": 0.9, "tool_calls": [], "business_rule_actions": [], '
            '"approval_items": [], "expected_schema": []}'
        )

    planner = PlanGenerator(llm_client=None)
    monkeypatch.setattr(planner.llm_wrapper, "generate_content", lambda prompt, **_: captured.update({"prompt": prompt}) or DummyResponse())
    planner.generate(
        structure_analysis={"tables": [], "column_analysis": [], "overall_structure": "flat", "confidence": 0.9},
        examples=[],
        target_template=template,
        context_packet=enrich_packet_for_planner(packet),
    )

    assert "Approved Business Rules" in captured["prompt"]
    assert "date_paid_media: format -> yyyy-mm-dd" in captured["prompt"]
    assert "clicks_paid_media: fill_blank -> 0" in captured["prompt"]


def test_business_rules_applied_to_dataframe_via_context_packet_mappings(context_flow_job):
    """Rules + mappings from context packet change the dataframe deterministically."""
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    packet = build_context_packet(job, target_template=template, selected_sheet="Sheet1")

    raw = pd.DataFrame(
        {
            "Event Date": ["2025-01-15", "2025-01-16"],
            "Spend": [120.5, 95.0],
            "Impressions": [4000, 3100],
            "Clicks": [22, ""],
        }
    )
    out, actions = apply_context_to_dataframe(
        raw,
        approved_mappings=packet["approved_mappings"],
        business_rules=packet["business_rules"],
    )

    assert "date_paid_media" in out.columns
    assert "clicks_paid_media" in out.columns
    assert out.loc[1, "clicks_paid_media"] == 0
    assert "filled blanks in clicks_paid_media" in actions or "formatted date_paid_media" in actions


def test_plan_review_notes_merge_and_reach_planner_prompt(context_flow_job, monkeypatch):
    """Simulate Review Plan approve: notes + decisions overlay fresh context for resume."""
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    fresh = build_context_packet(job, target_template=template, selected_sheet="Sheet1")

    original_approval = [
        {
            "target_column": "total_cost_paid_media",
            "question": "Spend appears twice — use one column or sum?",
            "options": [
                {"id": "single_column", "label": "Use Spend column only"},
                {"id": "combine_sources", "label": "Sum Fee + Media + Other"},
            ],
        }
    ]
    resolution = [
        {
            "analyst_confirm": "single_column",
            "resolution_note": "Use Spend only — Fee/Media are informational sub-rows.",
        }
    ]
    resolved = merge_resolved_approval_items(original_approval, resolution)
    saved_context = apply_resolved_approvals_to_context(
        {
            **fresh,
            "approved_mappings": [
                {"source_column": "Spend", "target_column": "total_cost_paid_media", "decision": "Keep"},
                {"source_column": "Fee", "target_column": "total_cost_paid_media", "decision": "Keep"},
            ],
        },
        resolved,
    )
    merged = merge_plan_review_context_packet(fresh, saved_context, job=job)
    merged = enrich_packet_for_planner(merged)

    assert merged["resolved_planner_decisions"][0]["resolution_note"].startswith("Use Spend only")
    assert "planner_decision_notes" in saved_context

    decisions_block = format_resolved_decisions_for_prompt(merged["resolved_planner_decisions"])
    assert "MANDATORY" in decisions_block
    assert "Use Spend only" in decisions_block
    assert "single_column" in decisions_block

    captured = {}

    class DummyResponse:
        text = (
            '{"confidence": 0.9, "tool_calls": [], "business_rule_actions": [], '
            '"approval_items": [], "expected_schema": []}'
        )

    planner = PlanGenerator(llm_client=None)
    monkeypatch.setattr(planner.llm_wrapper, "generate_content", lambda prompt, **_: captured.update({"prompt": prompt}) or DummyResponse())
    planner.generate(
        structure_analysis={"tables": [], "column_analysis": [], "overall_structure": "flat", "confidence": 0.9},
        examples=[],
        target_template=template,
        context_packet=merged,
    )

    assert "Analyst decisions from planner review" in captured["prompt"]
    assert "Use Spend only" in captured["prompt"]


def test_plan_review_single_column_decision_patches_tool_plan(context_flow_job):
    """Resolved single-column spend choice removes erroneous transform.calculate."""
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    packet = build_context_packet(job, target_template=template, selected_sheet="Sheet1")

    resolved = [
        {
            "target_column": "total_cost_paid_media",
            "analyst_confirm": "single_column",
            "resolution_note": "Spend is the authoritative total.",
            "options": [{"id": "single_column", "label": "Use Spend only"}],
        }
    ]
    cp = apply_resolved_approvals_to_context(
        {
            **packet,
            "approved_mappings": [
                {"source_column": "Spend", "target_column": "total_cost_paid_media", "decision": "Keep"},
                {"source_column": "Fee", "target_column": "total_cost_paid_media", "decision": "Keep"},
            ],
            "duplicate_target_mappings": {"total_cost_paid_media": ["Spend", "Fee"]},
        },
        resolved,
    )

    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "transform.rename", "params": {"mapping": {"Spend": "total_cost_paid_media"}}},
            {
                "tool": "transform.calculate",
                "params": {
                    "target_column": "total_cost_paid_media",
                    "expression": "spends_1 + spends_2",
                },
            },
        ],
        reasoning="LLM guessed a sum.",
    )
    patched = apply_resolved_approvals_to_plan(plan, cp)

    tool_names = [t.get("tool") for t in patched.tool_calls]
    assert "transform.calculate" not in tool_names
    assert "transform.calculate" in patched.reasoning.lower() or "single" in patched.reasoning.lower()


def test_verification_uses_business_rules_from_context_chain(context_flow_job, monkeypatch):
    """After mapping rename, verifier still enforces rules carried on the job context."""
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    packet = build_context_packet(job, target_template=template, selected_sheet="Sheet1")

    class StubVerifier:
        def verify(self, **_kwargs):
            from sia.agent.base import VerificationResult

            return VerificationResult(
                is_flat=True,
                confidence=0.95,
                issues=[],
                rule_issues=[],
                suggested_tools=[],
                summary="ok",
            )

    monkeypatch.setattr("sia.agent.nodes.OutputVerifier", lambda _llm: StubVerifier())

    state = {
        "llm_client": object(),
        "current_df": pd.DataFrame(
            {
                "date_paid_media": ["03/20/2026", "2026-03-21"],
                "clicks_paid_media": [1, ""],
            }
        ),
        "expected_columns": ["date_paid_media", "clicks_paid_media"],
        "tools_history": [],
        "issues_history": [],
        "iteration": 1,
        "max_iterations": 3,
        "business_rules": packet["business_rules"],
        "planned_rule_actions": [
            {"target_column": "date_paid_media", "action": "format", "value": "yyyy-mm-dd"},
            {"target_column": "clicks_paid_media", "action": "fill_blank", "value": "0"},
        ],
        "hitl_checkpoints": [],
        "hitl_pending_approval": False,
        "confidence_trajectory": [],
        "verifier_issues": [],
        "context_packet": packet,
    }

    result = verify_output_node(state)
    assert result["is_flat"] is False
    assert any(i["issue_type"] == "business_rule_format_violation" for i in result["verifier_issues"])
    assert any(i["issue_type"] == "business_rule_blank_violation" for i in result["verifier_issues"])


def test_replan_prompt_includes_date_granularity_from_context(context_flow_job, monkeypatch):
    """Replanner receives alignment obligation derived from context + template."""
    job = context_flow_job
    template = job.pop("_target_template_fixture")
    packet = build_context_packet(job, target_template=template, selected_sheet="Sheet1")
    summary = ContextPacket(**packet).planning_summary()
    alignment = summary["date_granularity_alignment"]

    captured = {}

    class DummyResponse:
        text = (
            '{"analysis": "fix dates", "root_cause": "format", "new_strategy": "format", '
            '"tool_calls": [{"tool": "transform.format", "params": {"columns": ["date_paid_media"]}}], '
            '"confidence": 0.8, "recommendation": "proceed"}'
        )

    replanner = Replanner(llm_client=object())
    monkeypatch.setattr(
        replanner.llm_wrapper,
        "generate_content",
        lambda prompt, **_: captured.update({"prompt": prompt}) or DummyResponse(),
    )

    from sia.agent.base import VerificationResult

    replanner.replan(
        current_df=pd.DataFrame({"date_paid_media": ["bad"]}),
        previous_tools=[],
        previous_issues=[{"message": "date format wrong"}],
        verification_result=VerificationResult(
            is_flat=False,
            confidence=0.5,
            issues=["date format"],
            rule_issues=[],
            suggested_tools=[],
            summary="Date column not ISO.",
        ),
        iteration=2,
        max_iterations=3,
        target_template=template,
        date_granularity_alignment=alignment,
    )

    assert "Date granularity alignment" in captured["prompt"]
    assert alignment.get("planner_obligation", "") in captured["prompt"] or "Obligation" in captured["prompt"]
