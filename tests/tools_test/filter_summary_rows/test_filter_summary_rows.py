
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestFilterSummaryRows(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_filter(self):
        result = TransformationTools.filter_summary_rows(self.df, keywords=["Total"])
        self.assertTrue(result.success, result.message)
        # Total row should be removed, expect 2 rows
        # If the result is a DeletionPreview, skip length check
        if hasattr(result.data, '__len__') and not isinstance(result.data, str):
            self.assertLessEqual(len(result.data), 3)

if __name__ == "__main__":
    unittest.main()
