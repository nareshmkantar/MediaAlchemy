
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestSplitColumn(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_split(self):
        result = TransformationTools.split_column(self.df, column="YearMonth", separator=" - ", new_names=["Year", "Month"])
        self.assertTrue(result.success)
        self.assertIn("Year", result.data.columns)
        self.assertIn("Month", result.data.columns)
        self.assertEqual(result.data.iloc[0]["Year"], "2024")

if __name__ == "__main__":
    unittest.main()
