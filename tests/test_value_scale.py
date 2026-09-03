"""Tests for header denomination (value scale) detection and application."""
import pandas as pd
import pytest

from sia.agent.base import ExtractionPlan
from sia.agent.context_packet import ContextPacket, build_context_packet, normalize_mapping_records
from sia.agent.planner import PlanGenerator
from sia.agent.value_scale import (
    build_target_scales_from_mappings,
    detect_value_scale_from_header,
    enrich_mapping_value_scale,
    value_scale_summary_from_mappings,
)
from sia.tools.transformation_tools import TransformationTools


@pytest.mark.parametrize(
    "header,expected_scale",
    [
        ("Spends in '000", 1000.0),
        ("Spend in '000", 1000.0),
        ("Impressions (000)", 1000.0),
        ("Cost in thousands", 1000.0),
        ("Revenue in millions", 1_000_000.0),
        ("Event Date", 1.0),
        ("Spend", 1.0),
    ],
)
def test_detect_value_scale_from_header(header, expected_scale):
    scale, note = detect_value_scale_from_header(header)
    assert scale == expected_scale
    if expected_scale != 1.0:
        assert note


def test_enrich_mapping_value_scale_from_header():
    row = enrich_mapping_value_scale(
        {"source_column": "Spends in '000", "target_column": "spends", "decision": "Keep"}
    )
    assert row["value_scale"] == 1000.0
    assert "thousand" in row["value_scale_note"].lower()
    assert "Header denomination" in row["reasoning"]


def test_normalize_mapping_records_detects_value_scale():
    rows = normalize_mapping_records(
        [{"source_column": "Impressions in '000", "target_column": "impressions", "decision": "Keep"}],
        source_id="src1",
    )
    assert rows[0]["value_scale"] == 1000.0


def test_build_context_packet_includes_value_scale_in_summary():
    job = {
        "id": "job_scale",
        "filename": "scale.xlsx",
        "file_path": "uploads/scale.xlsx",
        "source_registry": [
            {
                "source_id": "scale:Sheet1",
                "file_name": "scale.xlsx",
                "file_path": "uploads/scale.xlsx",
                "sheet_name": "Sheet1",
            }
        ],
        "mapping_registry": [
            {
                "source_id": "scale:Sheet1",
                "source_column": "Spends in '000",
                "target_column": "spends",
                "decision": "Keep",
            }
        ],
    }
    packet = build_context_packet(job, target_template={"properties": {}}, selected_sheet="Sheet1")
    summary = ContextPacket(**packet).planning_summary()
    notes = summary["mapping_summary"]["value_scale_notes"]
    assert any("spends" in n.lower() for n in notes)
    assert packet["approved_mappings"][0]["value_scale"] == 1000.0


def test_scale_columns_multiplies_numeric_values():
    df = pd.DataFrame({"spends": [120, 95], "impressions": [4000, 3100]})
    result = TransformationTools.scale_columns(df, scales={"spends": 1000})
    assert result.success
    assert result.data["spends"].tolist() == [120_000, 95_000]
    assert result.data["impressions"].tolist() == [4000, 3100]


def test_ensure_value_scale_inserts_after_rename():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "layout.extract", "params": {}},
            {"tool": "transform.rename", "params": {"mapping": {"Spends in '000": "spends"}}},
            {"tool": "transform.type_cast", "params": {"columns": ["spends"]}},
        ],
        reasoning="",
    )
    cp = {
        "approved_mappings": [
            {
                "source_column": "Spends in '000",
                "target_column": "spends",
                "decision": "Keep",
                "value_scale": 1000,
                "value_scale_note": "values in thousands ('000)",
            }
        ]
    }
    out = planner._ensure_value_scale_from_mappings(plan, cp)
    tools = [t.get("tool") for t in out.tool_calls]
    assert "transform.scale_values" in tools
    assert tools.index("transform.scale_values") > tools.index("transform.rename")
    step = next(t for t in out.tool_calls if t.get("tool") == "transform.scale_values")
    assert step["params"]["scales"]["spends"] == 1000


def test_ensure_value_scale_skips_when_calculate_already_scales():
    planner = PlanGenerator(llm_client=None)
    plan = ExtractionPlan(
        tool_calls=[
            {"tool": "transform.rename", "params": {"mapping": {"Spends in '000": "spends"}}},
            {
                "tool": "transform.calculate",
                "params": {"target_column": "spends", "expression": "spends * 1000"},
            },
        ],
        reasoning="",
    )
    cp = {
        "approved_mappings": [
            {
                "source_column": "Spends in '000",
                "target_column": "spends",
                "decision": "Keep",
                "value_scale": 1000,
            }
        ]
    }
    out = planner._ensure_value_scale_from_mappings(plan, cp)
    assert "transform.scale_values" not in [t.get("tool") for t in out.tool_calls]


def test_build_target_scales_from_mappings():
    scales = build_target_scales_from_mappings(
        [
            {"source_column": "A", "target_column": "spends", "decision": "Keep", "value_scale": 1000},
            {"source_column": "B", "target_column": "impressions", "decision": "Discard", "value_scale": 1000},
        ]
    )
    assert scales == {"spends": 1000.0}


def test_value_scale_summary_from_mappings():
    lines = value_scale_summary_from_mappings(
        [
            {
                "source_column": "Spends in '000",
                "target_column": "spends",
                "decision": "Keep",
                "value_scale": 1000,
                "value_scale_note": "values in thousands ('000)",
            }
        ]
    )
    assert len(lines) == 1
    assert "Spends in '000" in lines[0]
