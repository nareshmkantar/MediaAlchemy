
import unittest
import pandas as pd
import sys
import os

# Add project root to path to allow imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(project_root)

from sia.tools.transformation_tools import TransformationTools

class TestExpandHierarchicalRows(unittest.TestCase):
    def setUp(self):
        self.input_file = os.path.join(os.path.dirname(__file__), "input.csv")
        self.df = pd.read_csv(self.input_file)

    def test_expand(self):
        """Test expand hierarchical rows."""
        print(f"\nTesting Expand Hierarchical Rows on {self.input_file}")
        
        # Parent col is 'Parent' (col 0), Child is 'Child' (col 1)
        # Child indicator columns: In this input, rows with 'Child' value are children.
        # But wait, input CSV loading:
        # A, NaN, 10 -> Parent
        # NaN, 1, 20 -> Child of A
        
        # The tool expects `parent_col` index and `child_indicator_cols` indices.
        # Parent is col 0. Child indicator is col 1 (if it has value).
        
        result = TransformationTools.expand_hierarchical_rows(
            self.df, 
            parent_col=0,
            child_indicator_cols=[1]
        )
        
        self.assertTrue(result.success, f"Expansion failed: {result.message}")
        
        # Verify A propagated to its children
        # Row 1 (index 1) should now have Parent='A'
        self.assertEqual(result.data.iloc[1]["Parent"], "A")
        
        # Row 2 (index 2) should have Parent='A'
        self.assertEqual(result.data.iloc[2]["Parent"], "A")
        
        # Row 4 (index 4) should have Parent='B'
        self.assertEqual(result.data.iloc[4]["Parent"], "B")

if __name__ == "__main__":
    unittest.main()
