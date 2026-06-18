
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestDeduplicateRows(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_deduplicate(self):
        result = TransformationTools.deduplicate_rows(self.df)
        self.assertTrue(result.success, result.message)
        # Should remove 1 duplicate (A,10 appears twice)
        self.assertEqual(len(result.data), 3)

if __name__ == "__main__":
    unittest.main()
