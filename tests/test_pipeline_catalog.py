"""Pipeline stage catalog: coverage and formatting."""

from sia.tools.tool_validator import TOOL_SCHEMAS
from sia.tools.pipeline_catalog import (
    PipelineStage,
    STAGE_DEFINITIONS,
    TOOL_PRIMARY_STAGE,
    assert_primary_stage_coverage,
    format_catalog_markdown,
    serialize_catalog_json,
    augment_system_prompt_with_catalog,
    stage_for_tool,
    stage_sort_key,
)


def test_available_tools_matches_tool_schema_registry():
    from sia.tools import transformation_tools as tt

    registry = {entry["name"] for entry in tt.AVAILABLE_TOOLS}
    assert registry == set(TOOL_SCHEMAS.keys())
    assert len(tt.AVAILABLE_TOOLS) == len(TOOL_SCHEMAS)


def test_every_tool_schema_has_primary_stage():
    assert_primary_stage_coverage(list(TOOL_SCHEMAS.keys()))


def test_format_catalog_markdown_covers_all_stages():
    md = format_catalog_markdown()
    assert md.strip()
    for defn in STAGE_DEFINITIONS:
        assert defn.stage.value in md


def test_serialize_catalog_json_shape():
    data = serialize_catalog_json()
    assert len(data) == len(STAGE_DEFINITIONS)
    for row in data:
        assert "stage" in row
        assert "sort_key" in row
        assert "ux_phase" in row
        assert 1 <= row["ux_phase"] <= 4
        assert "entry_contract" in row
        assert "exit_contract" in row
        assert "intent_setting" in row
        assert "tools" in row
        assert isinstance(row["tools"], list)
    tool_count = sum(len(row["tools"]) for row in data)
    assert tool_count == len(TOOL_SCHEMAS)


def test_intent_setting_stages():
    intent_stages = {d.stage for d in STAGE_DEFINITIONS if d.intent_setting}
    assert PipelineStage.SEMANTIC_MAPPING in intent_stages
    assert PipelineStage.DATE_INTERPRETATION in intent_stages
    assert PipelineStage.LIGHTWEIGHT_PREP in intent_stages


def test_stage_for_tool_returns_expected():
    assert stage_for_tool("layout.extract") == PipelineStage.BLOCK_EXTRACTION
    assert stage_for_tool("layout.merge_headers") == PipelineStage.TABLE_SHAPE
    assert stage_for_tool("transform.unpivot") == PipelineStage.RESHAPING_AGGREGATION
    assert stage_sort_key(PipelineStage.VALIDATION) > stage_sort_key(PipelineStage.DISCOVERY)


def test_ux_phase_grouping():
    by_phase = {}
    for d in STAGE_DEFINITIONS:
        by_phase.setdefault(d.ux_phase, []).append(d.stage)
    assert set(by_phase.keys()) == {1, 2, 3, 4}


def test_augment_system_prompt_contains_catalog_header():
    out = augment_system_prompt_with_catalog("SYSTEM")
    assert "Pipeline stages and tool membership" in out
    assert out.startswith("SYSTEM")
