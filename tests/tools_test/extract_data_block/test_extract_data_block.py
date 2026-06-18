
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestExtractDataBlock(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"), header=None)

    def test_extract(self):
        # Extract block from row 2-3, col 1-3 with header at row 1
        result = TransformationTools.extract_data_block(self.df, start_row=2, end_row=3, start_col=1, end_col=3, header_row=1)
        self.assertTrue(result.success, result.message)
        self.assertEqual(len(result.data), 2)
        self.assertIn("Col1", result.data.columns)

if __name__ == "__main__":
    unittest.main()
