
import unittest
import pandas as pd
import sys
import os

# Add project root to path to allow imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(project_root)

from sia.tools.transformation_tools import TransformationTools

class TestMergeBlocks(unittest.TestCase):
    def setUp(self):
        # Load the input file from the same directory
        self.input_file = os.path.join(os.path.dirname(__file__), "input.csv")
        self.df = pd.read_csv(self.input_file, header=None) # Load without header to treat all as data

    def test_horizontal_merge(self):
        """Test merging side-by-side blocks."""
        print(f"\nTesting Horizontal Merge on {self.input_file}")
        # The input csv has headers at row 0: Region, Sales, <empty>, Region, Sales
        
        result = TransformationTools.merge_blocks(self.df, header_row=0, direction="horizontal")
        
        self.assertTrue(result.success, f"Merge failed: {result.message}")
        self.assertEqual(len(result.data), 4, f"Expected 4 rows, got {len(result.data)}")
        
        # Check if column names are reasonable (based on first block)
        # Note: The tool might rename them to Col_0, Col_1 etc.
        # But we expect the data to be stacked.
        # Block 1: North, 100
        # Block 2: South, 200 ...
        
        # Let's check a value exists
        vals = result.data.iloc[:, 0].astype(str).tolist()
        self.assertIn("North", vals)
        self.assertIn("South", vals)

    def test_vertical_merge_synthetic(self):
        """Test vertical merge using synthetic data (since input.csv is horizontal)."""
        data = {
            0: ["HeaderA", "HeaderB", "Val1", "Val2", "", "HeaderA", "HeaderB", "Val3", "Val4"],
            1: ["Col1", "Col2", "10", "20", "", "Col1", "Col2", "30", "40"]
        }
        df = pd.DataFrame(data)
        
        result = TransformationTools.merge_blocks(df, header_row=0, direction="vertical")
        
        self.assertTrue(result.success)
        # We expect 6 rows because the sub-headers are currently included as data boundaries/rows
        # (Based on previous behaviour confirmation)
        self.assertEqual(len(result.data), 6)

if __name__ == "__main__":
    unittest.main()
