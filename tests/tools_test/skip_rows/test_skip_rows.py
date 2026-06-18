
import unittest
import pandas as pd
import sys
import os

# Add project root to path to allow imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(project_root)

from sia.tools.transformation_tools import TransformationTools

class TestSkipRows(unittest.TestCase):
    def setUp(self):
        self.input_file = os.path.join(os.path.dirname(__file__), "input.csv")
        self.df = pd.read_csv(self.input_file)

    def test_skip(self):
        """Test skipping rows."""
        print(f"\nTesting Skip Rows on {self.input_file}")
        
        # Skip rows 1 and 3 (0-based indices)
        # Input:
        # 0: 1, 10
        # 1: 2, 20  <- Skip
        # 2: 3, 30
        # 3: 4, 40  <- Skip
        
        result = TransformationTools.skip_rows(
            self.df, 
            rows_to_skip=[1, 3]
        )
        
        self.assertTrue(result.success, f"Skip failed: {result.message}")
        
        # Expected rows: 2
        self.assertEqual(len(result.data), 2, f"Expected 2 rows, got {len(result.data)}")
        
        # Check remaining values
        vals = result.data["Value"].tolist()
        self.assertEqual(vals, [10, 30])

if __name__ == "__main__":
    unittest.main()
