
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestTypeCastColumns(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_cast(self):
        result = TransformationTools.type_cast_columns(self.df, type_map={"Value": "numeric", "Date": "date"})
        self.assertTrue(result.success)
        self.assertTrue(pd.api.types.is_numeric_dtype(result.data["Value"]))

if __name__ == "__main__":
    unittest.main()
