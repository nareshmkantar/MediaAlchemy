"""
Tests for the new Atomic Skills tools.
Tests: get_file_inventory, fuzzy_column_align, unmerge_and_fill, verify_checksum, inspect_sheet_structure.
"""
import pytest
import pandas as pd
import numpy as np
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sia.tools.transformation_tools import TransformationTools


# ===== Test: unmerge_and_fill (Alias) =====

class TestUnmergeAndFill:
    """Verify that unmerge_and_fill is a working alias for fill_merged_cells."""

    def test_basic_fill_down(self):
        df = pd.DataFrame({
            "Region": ["North", None, None, "South", None],
            "Sales": [100, 200, 300, 400, 500]
        })
        result = TransformationTools.unmerge_and_fill(df, direction="down", columns=["Region"])
        assert result.success
        assert result.data["Region"].tolist() == ["North", "North", "North", "South", "South"]

    def test_fill_specific_columns(self):
        df = pd.DataFrame({
            "Region": ["North", None, None],
            "City": ["NYC", None, None],
            "Sales": [100, 200, 300]
        })
        result = TransformationTools.unmerge_and_fill(df, direction="down", columns=["Region"])
        assert result.success
        assert result.data["Region"].tolist() == ["North", "North", "North"]
        # City should remain unfilled
        assert pd.isna(result.data["City"].iloc[1])


# ===== Test: fuzzy_column_align =====

class TestFuzzyColumnAlign:
    """Test schema alignment between different column naming conventions."""

    def test_exact_match(self):
        result = TransformationTools.fuzzy_column_align(
            source_columns=["Date", "Revenue", "Region"],
            target_columns=["Date", "Revenue", "Region"]
        )
        assert result.success
        assert result.data["matched_count"] == 3
        assert result.data["unmatched_count"] == 0

    def test_case_insensitive_match(self):
        result = TransformationTools.fuzzy_column_align(
            source_columns=["date", "REVENUE"],
            target_columns=["Date", "Revenue"]
        )
        assert result.success
        assert result.data["matched_count"] == 2
        for src, info in result.data["mapping"].items():
            assert info["confidence"] == 1.0

    def test_fuzzy_match(self):
        result = TransformationTools.fuzzy_column_align(
            source_columns=["Sales_Amount", "Product_Name"],
            target_columns=["Sales Amount", "Product Name"]
        )
        assert result.success
        assert result.data["matched_count"] == 2

    def test_unmatched_columns(self):
        result = TransformationTools.fuzzy_column_align(
            source_columns=["Revenue", "XYZ_UNIQUE_COL"],
            target_columns=["Revenue", "Sales"]
        )
        assert result.success
        assert "XYZ_UNIQUE_COL" in result.data["unmatched_source"]


# ===== Test: verify_checksum =====

class TestVerifyChecksum:
    """Test data integrity verification via checksum."""

    def test_checksum_passes(self):
        df = pd.DataFrame({"Sales": [100, 200, 300, 400]})
        result = TransformationTools.verify_checksum(df, "Sales", expected_total=1000.0)
        assert result.success
        assert result.data["passed"] is True
        assert result.data["difference"] == 0.0

    def test_checksum_fails(self):
        df = pd.DataFrame({"Sales": [100, 200, 300]})
        result = TransformationTools.verify_checksum(df, "Sales", expected_total=1000.0)
        assert result.success  # Tool itself succeeds, but checksum fails
        assert result.data["passed"] is False
        assert result.data["difference"] > 0

    def test_checksum_with_tolerance(self):
        df = pd.DataFrame({"Sales": [99, 200, 300, 400]})
        # Expected 1000, actual 999, diff = 0.1% which is within 1% tolerance
        result = TransformationTools.verify_checksum(df, "Sales", expected_total=1000.0, tolerance=0.01)
        assert result.success
        assert result.data["passed"] is True

    def test_checksum_with_non_numeric(self):
        df = pd.DataFrame({"Sales": [100, "N/A", 300, 400]})
        result = TransformationTools.verify_checksum(df, "Sales", expected_total=800.0)
        assert result.success
        assert result.data["passed"] is True
        assert result.data["non_numeric_count"] == 1

    def test_checksum_fuzzy_column_name(self):
        df = pd.DataFrame({"Total Sales": [100, 200, 300]})
        result = TransformationTools.verify_checksum(df, "total_sales", expected_total=600.0)
        assert result.success
        assert result.data["passed"] is True


# ===== Test: get_file_inventory =====

class TestGetFileInventory:
    """Test file inventory on sample Excel files."""

    def test_inventory_on_sample(self):
        """Test with any available sample file."""
        samples_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
        if not os.path.exists(samples_dir):
            pytest.skip("samples directory not found")

        # Find any .xlsx file
        xlsx_files = [f for f in os.listdir(samples_dir) if f.endswith('.xlsx')]
        if not xlsx_files:
            pytest.skip("No .xlsx files in samples directory")

        sample_path = os.path.join(samples_dir, xlsx_files[0])
        result = TransformationTools.get_file_inventory(sample_path)

        assert result.success
        assert "sheets" in result.data
        assert "total_sheets" in result.data
        assert isinstance(result.data["sheets"], list)
        assert len(result.data["sheets"]) > 0

        # Each sheet should have expected fields
        sheet = result.data["sheets"][0]
        assert "name" in sheet
        assert "rows" in sheet
        assert "cols" in sheet
        assert "state" in sheet

    def test_inventory_nonexistent_file(self):
        result = TransformationTools.get_file_inventory("/nonexistent/file.xlsx")
        assert not result.success


# ===== Test: inspect_sheet_structure =====

class TestInspectSheetStructure:
    """Test the deterministic structural scanner."""

    @staticmethod
    def _get_sample_path():
        """Get path to a sample xlsx file, or skip."""
        samples_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
        if not os.path.exists(samples_dir):
            pytest.skip("samples directory not found")
        xlsx_files = [f for f in os.listdir(samples_dir) if f.endswith('.xlsx')]
        if not xlsx_files:
            pytest.skip("No .xlsx files in samples directory")
        return os.path.join(samples_dir, xlsx_files[0])

    def test_report_structure(self):
        """Verify the report contains all expected keys."""
        sample = self._get_sample_path()
        result = TransformationTools.inspect_sheet_structure(sample)

        assert result.success
        report = result.data
        expected_keys = [
            "sheet_name", "dimensions", "merged_cells", "empty_rows",
            "empty_cols", "header_candidates", "hidden_rows", "hidden_cols",
            "data_region", "density", "noise_score", "summary"
        ]
        for key in expected_keys:
            assert key in report, f"Missing key: {key}"

    def test_noise_score_range(self):
        """Noise score should always be between 0.0 and 1.0."""
        sample = self._get_sample_path()
        result = TransformationTools.inspect_sheet_structure(sample)
        assert result.success
        assert 0.0 <= result.data["noise_score"] <= 1.0

    def test_density_range(self):
        """Density should be between 0.0 and 1.0."""
        sample = self._get_sample_path()
        result = TransformationTools.inspect_sheet_structure(sample)
        assert result.success
        assert 0.0 <= result.data["density"] <= 1.0

    def test_header_candidates_have_samples(self):
        """Each header candidate should include sample values."""
        sample = self._get_sample_path()
        result = TransformationTools.inspect_sheet_structure(sample)
        assert result.success
        for hc in result.data["header_candidates"]:
            assert "row" in hc
            assert "text_ratio" in hc
            assert "sample_values" in hc

    def test_specific_sheet(self):
        """Test inspecting a specific sheet by name."""
        sample = self._get_sample_path()
        # First get inventory to find sheet names
        inv = TransformationTools.get_file_inventory(sample)
        if inv.success and inv.data["sheets"]:
            sheet_name = inv.data["sheets"][0]["name"]
            result = TransformationTools.inspect_sheet_structure(sample, sheet_name=sheet_name)
            assert result.success
            assert result.data["sheet_name"] == sheet_name

    def test_nonexistent_sheet(self):
        """Should fail with clear error for nonexistent sheet."""
        sample = self._get_sample_path()
        result = TransformationTools.inspect_sheet_structure(sample, sheet_name="NONEXISTENT_SHEET_XYZ")
        assert not result.success
        assert "not found" in result.message

    def test_nonexistent_file(self):
        result = TransformationTools.inspect_sheet_structure("/nonexistent/file.xlsx")
        assert not result.success


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
