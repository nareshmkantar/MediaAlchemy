
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestMergeHeaderRows(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"), header=None)

    def test_merge(self):
        result = TransformationTools.merge_header_rows(self.df, header_rows=[0, 1], separator=" - ")
        self.assertTrue(result.success)
        # Headers should be merged, e.g. "Year - Month", "2024 - Jan"
        self.assertIn(" - ", result.data.columns[0])

if __name__ == "__main__":
    unittest.main()
