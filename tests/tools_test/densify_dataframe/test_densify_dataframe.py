
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestDensifyDataframe(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_densify(self):
        result = TransformationTools.densify_dataframe(self.df, dimension_cols=[0])
        self.assertTrue(result.success)
        # Dim1 should be forward-filled
        self.assertEqual(result.data.iloc[1]["Dim1"], "A")

if __name__ == "__main__":
    unittest.main()
