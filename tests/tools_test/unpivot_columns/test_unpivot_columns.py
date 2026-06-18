
import unittest
import pandas as pd
import sys
import os

# Add project root to path to allow imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(project_root)

from sia.tools.transformation_tools import TransformationTools

class TestUnpivotColumns(unittest.TestCase):
    def setUp(self):
        self.input_file = os.path.join(os.path.dirname(__file__), "input.csv")
        self.df = pd.read_csv(self.input_file)

    def test_unpivot(self):
        """Test unpivot columns (melt)."""
        print(f"\nTesting Unpivot Columns on {self.input_file}")
        
        result = TransformationTools.unpivot_columns(
            self.df, 
            id_cols=["ID", "Name"],
            value_cols=["2023", "2024"],
            var_name="Year",
            value_name="Amount"
        )
        
        self.assertTrue(result.success, f"Unpivot failed: {result.message}")
        
        # Expected rows: 2 people * 2 years = 4 rows
        self.assertEqual(len(result.data), 4, f"Expected 4 rows, got {len(result.data)}")
        
        # Check columns
        expected_cols = ["ID", "Name", "Year", "Amount"]
        self.assertEqual(list(result.data.columns), expected_cols)
        
        # Check specific value
        # Alice (101) 2023 should be 500
        alice_2023 = result.data[(result.data["Name"] == "Alice") & (result.data["Year"] == "2023")]
        self.assertEqual(alice_2023.iloc[0]["Amount"], 500)

if __name__ == "__main__":
    unittest.main()
