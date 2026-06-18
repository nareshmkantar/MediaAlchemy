
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestCrosstabUnpivot(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"), header=None)

    def test_unpivot(self):
        result = TransformationTools.crosstab_unpivot(
            self.df,
            row_header_col=0,
            col_header_row=0,
            data_start_row=1,
            data_start_col=1,
            row_dim_name="Brand",
            col_dim_name="Region",
            value_name="Sales"
        )
        self.assertTrue(result.success, result.message)
        # Should have 2 brands x 3 regions = 6 rows
        self.assertEqual(len(result.data), 6)
        self.assertIn("Brand", result.data.columns)
        self.assertIn("Region", result.data.columns)
        self.assertIn("Sales", result.data.columns)

if __name__ == "__main__":
    unittest.main()
