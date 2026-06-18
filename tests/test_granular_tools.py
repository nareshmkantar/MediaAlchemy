import pandas as pd
import numpy as np
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sia.tools.transformation_tools import TransformationTools

def test_propagate_dimensions():
    print("Testing propagate_dimensions...")
    df = pd.DataFrame({
        'Region': ['North', '', '', 'South', None, ''],
        'Sales': [100, 200, 300, 400, 500, 600]
    })
    
    result = TransformationTools.propagate_dimensions(df, dimension_cols=['Region'])
    
    assert result.success
    # Check if Region column is forward-filled
    assert list(result.data['Region']) == ['North', 'North', 'North', 'South', 'South', 'South']
    print("DONE: propagate_dimensions passed!")

def test_filter_incomplete_rows():
    print("\nTesting filter_incomplete_rows...")
    df = pd.DataFrame({
        'Date': ['2023-01-01', '2023-01-02', 'Total', '', '2023-01-03'],
        'Value': [10, 20, 30, None, 40]
    })
    
    # Test 1: Drop rows where Value is missing
    result = TransformationTools.filter_incomplete_rows(df, value_cols=['Value'])
    assert result.success
    assert len(result.data) == 4
    assert 'Total' in result.data['Date'].values
    
    # Test 2: Drop if Value is N/A (Note: 'Total' might be a row we want to filter later, 
    # but filter_incomplete_rows is about metric presence)
    df_with_nan = pd.DataFrame({
        'Metric': ['Reach', 'Frequency', '', None],
        'Result': [1000, 2.5, np.nan, None]
    })
    result2 = TransformationTools.filter_incomplete_rows(df_with_nan, value_cols=['Result'])
    assert result2.success
    assert len(result2.data) == 2
    assert list(result2.data['Metric']) == ['Reach', 'Frequency']
    
    print("DONE: filter_incomplete_rows passed!")

if __name__ == "__main__":
    try:
        test_propagate_dimensions()
        test_filter_incomplete_rows()
        print("\nAll granular tool tests passed! Genie out.")
    except AssertionError as e:
        print(f"\nFAIL: Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nFAIL: Unexpected error: {e}")
        sys.exit(1)
