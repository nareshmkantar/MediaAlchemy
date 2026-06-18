
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestPropagateDimensions(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_propagate(self):
        result = TransformationTools.propagate_dimensions(self.df, dimension_cols=["Category"])
        self.assertTrue(result.success)
        # Category should be forward-filled
        self.assertEqual(result.data.iloc[1]["Category"], "A")
        self.assertEqual(result.data.iloc[4]["Category"], "B")

if __name__ == "__main__":
    unittest.main()
