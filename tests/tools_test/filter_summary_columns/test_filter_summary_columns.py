
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestFilterSummaryColumns(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_filter(self):
        result = TransformationTools.filter_summary_columns(self.df, keywords=["Total"])
        self.assertTrue(result.success)
        # Total column should be removed
        self.assertNotIn("Total", result.data.columns)

if __name__ == "__main__":
    unittest.main()
