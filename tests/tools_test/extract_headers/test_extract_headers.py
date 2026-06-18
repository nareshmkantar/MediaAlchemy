
import unittest
import pandas as pd
import sys
import os

# Add project root to path to allow imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(project_root)

from sia.tools.transformation_tools import TransformationTools

class TestExtractHeaders(unittest.TestCase):
    def setUp(self):
        self.input_file = os.path.join(os.path.dirname(__file__), "input.csv")
        # Load without header initially to simulate raw grid
        self.df = pd.read_csv(self.input_file, header=None)

    def test_extract(self):
        """Test extract headers."""
        print(f"\nTesting Extract Headers on {self.input_file}")
        
        # Row 0 contains "Col1", "Col2", "Col3" (since read without header, first row is data)
        # Actually pd.read_csv without header puts it at row 0? No, puts it as row 0 data.
        
        result = TransformationTools.extract_headers(self.df, header_row=0)
        
        self.assertTrue(result.success, f"Extraction failed: {result.message}")
        
        expected_headers = ["Col1", "Col2", "Col3"]
        self.assertEqual(result.data, expected_headers)

if __name__ == "__main__":
    unittest.main()
