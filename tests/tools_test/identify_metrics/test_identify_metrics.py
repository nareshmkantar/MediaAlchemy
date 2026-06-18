
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestIdentifyMetrics(unittest.TestCase):
    def setUp(self):
        self.df = pd.read_csv(os.path.join(os.path.dirname(__file__), "input.csv"))

    def test_identify(self):
        result = TransformationTools.identify_metrics(self.df, metric_col=0)
        self.assertTrue(result.success)
        # Result.data is a DataFrame; check metric column
        metrics = result.data["Metric"].tolist()
        self.assertIn("Sales", metrics)
        self.assertIn("Profit", metrics)

if __name__ == "__main__":
    unittest.main()
