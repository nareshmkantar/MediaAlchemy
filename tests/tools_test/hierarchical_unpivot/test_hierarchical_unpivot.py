
import unittest
import pandas as pd
import sys
import os

# Add project root to path to allow imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(project_root)

from sia.tools.transformation_tools import TransformationTools

class TestHierarchicalUnpivot(unittest.TestCase):
    def setUp(self):
        self.input_file = os.path.join(os.path.dirname(__file__), "input.csv")
        self.df = pd.read_csv(self.input_file)

    def test_basic_unpivot(self):
        """Test basic hierarchical unpivot."""
        print(f"\nTesting Hierarchical Unpivot on {self.input_file}")
        
        # Unpivot Math and Science, keeping Class and Student as IDs
        result = TransformationTools.hierarchical_unpivot(
            self.df, 
            id_cols=["Class", "Student"],
            value_cols=["Math", "Science"],
            var_name="Subject",
            value_name="Score"
        )
        
        self.assertTrue(result.success, f"Unpivot failed: {result.message}")
        
        # Expected rows: 4 students * 2 subjects = 8 rows? 
        # Wait, input has 3 rows (John, Jane, Bob). So 3 * 2 = 6 rows.
        self.assertEqual(len(result.data), 6, f"Expected 6 rows, got {len(result.data)}")
        
        # Check columns
        expected_cols = ["Class", "Student", "Subject", "Score"]
        self.assertEqual(list(result.data.columns), expected_cols)
        
        # Check specific value
        # John (A) Math should be 90
        john_math = result.data[(result.data["Student"] == "John") & (result.data["Subject"] == "Math")]
        self.assertEqual(john_math.iloc[0]["Score"], 90)

if __name__ == "__main__":
    unittest.main()
