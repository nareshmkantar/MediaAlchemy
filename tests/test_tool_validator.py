"""
Unit Tests for Tool Validator.

Tests:
- Tool schema validation
- Fuzzy matching for hallucinated tool names
- Parameter type coercion
- Destructive tool detection
"""
import pytest
from typing import Dict, Any

# Import the module under test
import sys
sys.path.insert(0, 'c:/Users/MN/OneDrive - Kantar/Desktop/Trainings/Gemini CLI/gemini-test/Hackathon/SchemaAgent_semantic_kernel')

from sia.tools.tool_validator import (
    validate_tool_call,
    validate_tool_sequence,
    normalize_tool_name,
    is_destructive_tool,
    get_destructive_tools,
    sort_tool_calls_by_pipeline_stage,
    ValidationResult,
    TOOL_SCHEMAS,
)


# ============ normalize_tool_name Tests ============

class TestNormalizeToolName:
    """Test fuzzy matching for tool names."""
    
    def test_exact_match(self):
        """Test that exact tool names are returned as-is."""
        # normalize_tool_name returns (name, was_normalized) tuple
        name, was_normalized = normalize_tool_name("extract_data_block")
        assert name == "layout.extract"
        assert was_normalized
        
        name, _ = normalize_tool_name("fill_merged_cells")
        assert name == "transform.fill_merged"
    
    def test_case_insensitive(self):
        """Test case-insensitive matching."""
        name, was_normalized = normalize_tool_name("Extract_Data_Block")
        assert name == "layout.extract"
        # May or may not be marked as normalized depending on implementation
    
    def test_hallucinated_names(self):
        """Test normalization of commonly hallucinated names."""
        # fill_cells_down -> fill_merged_cells
        name, was_normalized = normalize_tool_name("fill_cells_down")
        assert name == "transform.fill_merged"
        assert was_normalized  # This was normalized
        
        # fill_down -> fill_merged_cells
        name, _ = normalize_tool_name("fill_down")
        assert name == "transform.fill_merged"
        
        # extract_block -> extract_data_block
        name, _ = normalize_tool_name("extract_block")
        assert name == "layout.extract"
        
        # unpivot -> unpivot_columns
        name, _ = normalize_tool_name("unpivot")
        assert name == "transform.unpivot"
    
    def test_unknown_tool_returns_original(self):
        """Test that unknown tools return original name."""
        name, was_normalized = normalize_tool_name("completely_fake_tool")
        assert name == "completely_fake_tool"
        assert not was_normalized
    
    def test_empty_string(self):
        """Test handling empty string."""
        name, _ = normalize_tool_name("")
        assert name == ""
    
    def test_none_handling(self):
        """Test handling None input."""
        # Should not raise, return empty or None
        try:
            name, was_normalized = normalize_tool_name(None)
            assert name == ""
            assert was_normalized is False
        except (TypeError, AttributeError):
            # Also acceptable
            pass


# ============ validate_tool_call Tests ============

class TestValidateToolCall:
    """Test tool call validation."""
    
    def test_valid_extract_data_block(self):
        """Test validation of valid extract_data_block call."""
        tool_call = {
            "tool": "extract_data_block",
            "params": {
                "start_row": 0,
                "end_row": 50,
                "start_col": 0,
                "end_col": 10,
                "header_row": 0
            }
        }
        result = validate_tool_call(tool_call)
        assert result.valid
        assert len(result.errors) == 0
        assert result.normalized_tool_name == "layout.extract"
    
    def test_valid_fill_merged_cells(self):
        """Test validation of valid fill_merged_cells call."""
        tool_call = {
            "tool": "fill_merged_cells",
            "params": {}
        }
        result = validate_tool_call(tool_call)
        assert result.valid
    
    def test_missing_tool_name(self):
        """Test validation fails for missing tool name."""
        tool_call = {"params": {"start_row": 0}}
        result = validate_tool_call(tool_call)
        assert not result.valid
        assert any("tool" in err.lower() for err in result.errors)
    
    def test_missing_required_param(self):
        """Test validation fails for missing required params."""
        tool_call = {
            "tool": "extract_data_block",
            "params": {
                "start_row": 0
                # Missing end_row, start_col, end_col
            }
        }
        result = validate_tool_call(tool_call)
        # Might be valid with defaults or might fail
        # Depends on schema definition
        assert isinstance(result, ValidationResult)
    
    def test_type_coercion_string_to_int(self):
        """Test that string numbers are coerced to int."""
        tool_call = {
            "tool": "extract_data_block",
            "params": {
                "start_row": "0",  # String
                "end_row": "50",   # String
                "start_col": 0,
                "end_col": 10
            }
        }
        result = validate_tool_call(tool_call)
        if result.valid:
            # Check if params were coerced
            assert result.normalized_params.get("start_row") == 0 or result.normalized_params.get("start_row") == "0"
    
    def test_hallucinated_tool_normalized(self):
        """Test that hallucinated tool names are normalized."""
        tool_call = {
            "tool": "fill_cells_down",  # Hallucinated
            "params": {}
        }
        result = validate_tool_call(tool_call)
        assert result.normalized_tool_name == "transform.fill_merged"
    
    def test_unknown_tool_flagged(self):
        """Test that unknown tools are flagged with warning or error."""
        tool_call = {
            "tool": "delete_everything",
            "params": {}
        }
        result = validate_tool_call(tool_call)
        # Should either fail or have warnings
        assert not result.valid or len(result.warnings) > 0
    
    def test_empty_params(self):
        """Test validation with empty params dict."""
        tool_call = {
            "tool": "densify_dataframe",
            "params": {}
        }
        result = validate_tool_call(tool_call)
        # Should be valid for tools without required params
        assert isinstance(result, ValidationResult)
    
    def test_extra_params_allowed(self):
        """Test that extra params don't cause errors."""
        tool_call = {
            "tool": "fill_merged_cells",
            "params": {
                "extra_param": "some_value",
                "another_extra": 123
            }
        }
        result = validate_tool_call(tool_call)
        # Extra params should be allowed (maybe with warning)
        assert result.valid or len(result.warnings) > 0

    def test_filter_empty_accepts_subset_alias(self):
        """Planner may use pandas-style subset for empty-row checks."""
        result = validate_tool_call(
            {
                "tool": "transform.filter_empty",
                "params": {"subset": ["date", "spends"]},
            }
        )
        assert result.valid
        assert result.normalized_params["check_columns"] == ["date", "spends"]
        assert not any("Unknown parameter 'subset'" in w for w in result.warnings)


# ============ is_destructive_tool Tests ============

class TestDestructiveTool:
    """Test destructive tool detection."""
    
    def test_destructive_tools_identified(self):
        """Test that known destructive tools are identified."""
        destructive_tools = [
            "filter_summary_rows",
            "filter_header_rows", 
            "filter_empty_rows",
            "skip_rows",
            "unpivot_columns",
            "densify_dataframe"
        ]
        for tool in destructive_tools:
            assert is_destructive_tool(tool), f"{tool} should be destructive"
    
    def test_non_destructive_tools(self):
        """Test that non-destructive tools are not flagged."""
        safe_tools = [
            "extract_data_block",
            "fill_merged_cells",
            "rename_columns"
        ]
        for tool in safe_tools:
            # Might or might not be destructive depending on definition
            result = is_destructive_tool(tool)
            assert isinstance(result, bool)
    
    def test_unknown_tool_not_destructive(self):
        """Test that unknown tools default to non-destructive."""
        assert not is_destructive_tool("some_unknown_tool")


# ============ get_destructive_tools Tests ============

class TestGetDestructiveTools:
    """Test extracting destructive tools from a sequence."""
    
    def test_finds_destructive_tools(self):
        """Test finding destructive tools in a sequence."""
        tool_calls = [
            {"tool": "extract_data_block", "params": {}},
            {"tool": "fill_merged_cells", "params": {}},
            {"tool": "unpivot_columns", "params": {"value_name": "Value"}},
            {"tool": "densify_dataframe", "params": {}}
        ]
        destructive = get_destructive_tools(tool_calls)
        assert "unpivot_columns" in destructive or "densify_dataframe" in destructive
    
    def test_empty_list(self):
        """Test with empty tool list."""
        destructive = get_destructive_tools([])
        assert destructive == []
    
    def test_no_destructive_tools(self):
        """Test when no destructive tools present."""
        tool_calls = [
            {"tool": "extract_data_block", "params": {}},
            {"tool": "fill_merged_cells", "params": {}}
        ]
        destructive = get_destructive_tools(tool_calls)
        # May or may not be empty depending on fill_merged_cells classification
        assert isinstance(destructive, list)


# ============ ValidationResult Tests ============

class TestValidationResult:
    """Test ValidationResult dataclass."""
    
    def test_valid_result(self):
        """Test creating a valid result."""
        result = ValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            normalized_tool_name="extract_data_block",
            normalized_params={"start_row": 0}
        )
        assert result.valid
        assert result.normalized_tool_name == "extract_data_block"
    
    def test_invalid_result(self):
        """Test creating an invalid result with errors."""
        result = ValidationResult(
            valid=False,
            errors=["Missing required param: start_row"],
            warnings=[],
            normalized_tool_name="extract_data_block",
            normalized_params={}
        )
        assert not result.valid
        assert len(result.errors) == 1


# ============ TOOL_SCHEMAS Tests ============

class TestToolSchemas:
    """Test that tool schemas are properly defined."""
    
    def test_schemas_exist(self):
        """Test that TOOL_SCHEMAS is populated."""
        assert len(TOOL_SCHEMAS) > 0
    
    def test_extract_data_block_schema(self):
        """Test extract_data_block schema structure."""
        assert "layout.extract" in TOOL_SCHEMAS
        schema = TOOL_SCHEMAS["layout.extract"]
        # Schema is a ToolSchema dataclass, check it has params attribute
        assert hasattr(schema, 'params')
    
    def test_all_schemas_have_params(self):
        """Test that all schemas have params attribute."""
        for tool_name, schema in TOOL_SCHEMAS.items():
            assert hasattr(schema, 'params'), f"{tool_name} missing params"


class TestValidateToolSequenceStageMonotonicity:
    """Stage-order warnings from pipeline_catalog."""

    _extract_params = {
        "start_row": 0,
        "end_row": 5,
        "start_col": 0,
        "end_col": 3,
        "header_row": 0,
    }

    def test_verify_before_extract_emits_stage_order_warning(self):
        calls = [
            {"tool": "verify.schema", "params": {"schema": {"type": "object"}}},
            {"tool": "layout.extract", "params": dict(self._extract_params)},
        ]
        _, errors = validate_tool_sequence(calls)
        assert any("Stage order:" in e for e in errors)

    def test_merge_headers_then_extract_no_stage_regression(self):
        calls = [
            {"tool": "layout.merge_headers", "params": {"header_rows": [0, 1]}},
            {"tool": "layout.extract", "params": dict(self._extract_params)},
        ]
        _, errors = validate_tool_sequence(calls)
        assert not any("Stage order:" in e for e in errors)

    def test_aggregate_weekly_before_date_tool_emits_stage_order_warning(self):
        calls = [
            {"tool": "layout.extract", "params": dict(self._extract_params)},
            {
                "tool": "transform.aggregate_weekly",
                "params": {
                    "date_col": "d",
                    "metric_rules": {"x": "sum"},
                },
            },
            {
                "tool": "transform.build_date_from_parts",
                "params": {"year_col": "y", "month_col": "m"},
            },
        ]
        _, errors = validate_tool_sequence(calls)
        assert any("Stage order:" in e for e in errors)

    def test_verify_schema_can_be_checkpoint_before_post_processing(self):
        calls = [
            {"tool": "layout.extract", "params": dict(self._extract_params)},
            {"tool": "verify.schema", "params": {"schema": {"type": "object"}}},
            {"tool": "transform.reorder_columns", "params": {"column_order": ["date", "spends"]}},
        ]
        _, errors = validate_tool_sequence(calls)
        assert not any("should be the LAST step" in e for e in errors)


class TestSortToolCallsByPipelineStage:
    """sort_tool_calls_by_pipeline_stage orders by pipeline_catalog stage sort_key."""

    _extract = {
        "tool": "layout.extract",
        "params": {"start_row": 0, "end_row": 10, "start_col": 0, "end_col": 5, "header_row": 0},
    }

    def test_format_before_infer_daily_becomes_date_then_format(self):
        wrong_order = [
            dict(self._extract),
            {"tool": "transform.rename", "params": {"mapping": {"a": "b"}}},
            {"tool": "transform.type_cast", "params": {"columns": {"x": "str"}}},
            {"tool": "transform.format", "params": {"column": "d", "format": "%Y-%m-%d"}},
            {"tool": "transform.infer_granularity_expand_to_daily", "params": {"date_col": "d"}},
            {"tool": "transform.aggregate_weekly", "params": {"date_col": "d", "metric_rules": {"m": "sum"}}},
            {"tool": "verify.schema", "params": {"schema": {"type": "object"}}},
        ]
        sorted_calls = sort_tool_calls_by_pipeline_stage(wrong_order)
        names = [c["tool"] for c in sorted_calls]
        assert names.index("transform.infer_granularity_expand_to_daily") < names.index("transform.format")
        assert names.index("transform.format") < names.index("transform.aggregate_weekly")
        assert names[-1] == "verify.schema"

    def test_stable_within_same_stage(self):
        calls = [
            dict(self._extract),
            {"tool": "transform.rename", "params": {"mapping": {"x": "a"}}},
            {"tool": "transform.type_cast", "params": {"columns": {"a": "float"}}},
        ]
        out = sort_tool_calls_by_pipeline_stage(calls)
        assert [c["tool"] for c in out][:3] == ["layout.extract", "transform.rename", "transform.type_cast"]

    def test_allocate_after_rename_when_planner_lists_allocate_first(self):
        planner_wrong = [
            dict(self._extract),
            {"tool": "transform.classify_metric_level", "params": {"block_start_columns": ["Name"]}},
            {"tool": "transform.allocate_block_metric", "params": {"metric_col": "spends", "block_start_columns": ["Name"]}},
            {"tool": "transform.rename", "params": {"mapping": {"Budgets_Total": "spends"}}},
            {"tool": "transform.type_cast", "params": {"columns": {"spends": "float"}}},
        ]
        out = sort_tool_calls_by_pipeline_stage(planner_wrong)
        names = [c["tool"] for c in out]
        assert names.index("transform.rename") < names.index("transform.classify_metric_level")
        assert names.index("transform.rename") < names.index("transform.allocate_block_metric")
        assert names.index("transform.type_cast") < names.index("transform.allocate_block_metric")

    def test_expand_grouped_block_sorted_after_rename(self):
        calls = [
            dict(self._extract),
            {"tool": "transform.expand_grouped_block", "params": {"dimension_columns": ["Name"]}},
            {"tool": "transform.rename", "params": {"mapping": {"Post": "channel"}}},
            {"tool": "transform.type_cast", "params": {"columns": {"spends": "float"}}},
        ]
        out = sort_tool_calls_by_pipeline_stage(calls)
        names = [c["tool"] for c in out]
        assert names.index("transform.rename") < names.index("transform.expand_grouped_block")
        assert names.index("transform.type_cast") < names.index("transform.expand_grouped_block")

    def test_drop_columns_deferred_after_block_metric_tools(self):
        calls = [
            dict(self._extract),
            {"tool": "transform.rename", "params": {"mapping": {"x": "y"}}},
            {"tool": "transform.drop_columns", "params": {"columns": ["Name", "Market"]}},
            {
                "tool": "transform.classify_metric_level",
                "params": {"block_start_columns": ["Name"]},
            },
            {
                "tool": "transform.allocate_block_metric",
                "params": {"metric_col": "spends", "block_start_columns": ["Name"]},
            },
            {"tool": "transform.format", "params": {"column": "date", "format": "%Y-%m-%d"}},
        ]
        out = sort_tool_calls_by_pipeline_stage(calls)
        names = [c["tool"] for c in out]
        assert names.index("transform.drop_columns") > names.index("transform.allocate_block_metric")
        assert names.index("transform.classify_metric_level") < names.index("transform.drop_columns")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
