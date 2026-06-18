
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestTransposeData(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_transpose(self):
        result = TransformationTools.transpose_data(self.df, header_col=0)
        self.assertTrue(result.success, result.message)
        # Should have 3 data columns (A, B, C) as rows now
        self.assertEqual(len(result.data), 3)

if __name__ == "__main__":
    unittest.main()
