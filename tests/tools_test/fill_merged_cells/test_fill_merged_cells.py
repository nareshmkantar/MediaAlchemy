
import unittest
import pandas as pd
import sys
import os

# Add project root to path to allow imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(project_root)

from sia.tools.transformation_tools import TransformationTools

class TestFillMergedCells(unittest.TestCase):
    def setUp(self):
        self.input_file = os.path.join(os.path.dirname(__file__), "input.csv")
        self.df = pd.read_csv(self.input_file)

    def test_fill_down(self):
        """Test filling merged cells downwards."""
        print(f"\nTesting Fill Merged Cells on {self.input_file}")
        
        # Fill 'Category' column down
        # Expected: 
        # A, 1, 10
        # A, 1, 20 (Category filled)
        # B, 1, 30
        # B, 1, 40 (Category filled)
        
        result = TransformationTools.fill_merged_cells(
            self.df, 
            direction="down",
            columns=["Category"]
        )
        
        self.assertTrue(result.success, f"Fill failed: {result.message}")
        
        # Verify
        filled_col = result.data["Category"].tolist()
        expected = ["A", "A", "B", "B"]
        self.assertEqual(filled_col, expected)

if __name__ == "__main__":
    unittest.main()
