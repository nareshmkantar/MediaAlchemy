
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestFuzzyStandardizeColumn(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_standardize(self):
        result = TransformationTools.fuzzy_standardize_column(self.df, column="Brand", threshold=0.85)
        self.assertTrue(result.success)
        # All "Brand A" variants should be standardized
        unique_brands = result.data["Brand"].unique()
        self.assertLessEqual(len(unique_brands), 2)

if __name__ == "__main__":
    unittest.main()
