
import unittest
import pandas as pd
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
from sia.tools.transformation_tools import TransformationTools

class TestFilterSummaryFalsePositives(unittest.TestCase):
    def test_density_protection(self):
        # Dataset with normal data that looks "dense"
        data = {
            'Date': ['2023-01-01', '2023-01-02', '2023-01-03', 'Total'],
            'Impressions': [1000, 2000, 3000, 6000],
            'Spend': [10.5, 20.8, 30.2, 61.5]
        }
        df = pd.DataFrame(data)
        
        # Enable structural detection
        result = TransformationTools.filter_summary_rows(
            df, 
            keywords=["Total"], 
            use_structural_detection=True,
            numeric_threshold=0.7
        )
        
        self.assertTrue(result.success)
        
        # It should ONLY remove the "Total" row (keyword match)
        # The first 3 rows ('2023-01-01' etc) should be PROTECTED even though they are dense
        # because 'Date' is a protected keyword and they are numeric dates.
        self.assertEqual(len(result.data), 3)
        self.assertNotIn('Total', result.data['Date'].values)
        self.assertIn('2023-01-01', result.data['Date'].values)

    def test_brand_protection(self):
        # Dataset where col 0 is a brand name and row is dense
        data = {
            'Brand': ['Brand A', 'Brand B', 'Brand C', 'Overall'],
            'Reach': [0.5, 0.6, 0.7, 0.9],
            'Budget': [100, 200, 300, 600]
        }
        df = pd.DataFrame(data)
        
        result = TransformationTools.filter_summary_rows(
            df, 
            keywords=["Overall"], 
            use_structural_detection=True
        )
        
        # Should keep the brands (protected category 'brand' match)
        # and remove 'Overall'
        self.assertEqual(len(result.data), 3)
        self.assertIn('Brand A', result.data['Brand'].values)

    def test_sum_substring_does_not_remove_consumption(self):
        data = {
            "Metric": ["Consumption", "Reach", "Total"],
            "Value": [100, 200, 300],
        }
        df = pd.DataFrame(data)
        result = TransformationTools.filter_summary_rows(
            df,
            keywords=["Total", "Grand Total", "Sum", "Subtotal"],
            use_structural_detection=True,
        )
        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 2)
        self.assertIn("Consumption", result.data["Metric"].values)

    def test_dense_dimension_row_not_removed_without_summary_keyword(self):
        data = {
            "Region": ["National", "Regional", "Total"],
            "Impressions": [1000, 2000, 3000],
            "Spend": [10.5, 20.8, 30.2],
        }
        df = pd.DataFrame(data)
        result = TransformationTools.filter_summary_rows(
            df,
            keywords=["Total"],
            use_structural_detection=True,
            numeric_threshold=0.7,
        )
        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 2)
        self.assertIn("National", result.data["Region"].values)
        self.assertIn("Regional", result.data["Region"].values)

if __name__ == "__main__":
    unittest.main()
