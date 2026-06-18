
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestFilterHeaderRows(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_filter(self):
        result = TransformationTools.filter_header_rows(self.df)
        self.assertTrue(result.success)
        # Should remove the repeated header row
        self.assertEqual(len(result.data), 2)

if __name__ == "__main__":
    unittest.main()
