
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestRenameColumns(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_rename(self):
        result = TransformationTools.rename_columns(self.df, mapping={0: "Category", 1: "Value"})
        self.assertTrue(result.success)
        self.assertEqual(list(result.data.columns), ["Category", "Value"])

if __name__ == "__main__":
    unittest.main()
