
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestDetectStructuralSummaryRows(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_detect(self):
        result = TransformationTools.detect_structural_summary_rows(self.df, text_column=0)
        self.assertTrue(result.success)
        # Result is a list of dicts, check if row index 2 is in there
        row_indices = [r['row_index'] for r in result.data]
        self.assertIn(2, row_indices)

if __name__ == "__main__":
    unittest.main()
