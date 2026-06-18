"""Test MCP client tool execution."""
from sia.utils.mcp_client import get_mcp_client

client = get_mcp_client()

# Test 1: List tools
print("Test 1: List tools")
tools = client.list_tools()
print(f"  Tools available: {len(tools)}")
print(f"  Tool names: {[t['name'] for t in tools]}")

# Test 2: Call rename_columns
print("\nTest 2: Call rename_columns")
result = client.call_tool('rename_columns', {
    'data': [{'A': 1, 'B': 2}, {'A': 3, 'B': 4}],
    'mapping': {'A': 'Column_A'}
})
print(f"  Success: {result.get('success')}")
print(f"  Message: {result.get('message')}")
if result.get('data_preview'):
    print(f"  Data preview: {result.get('data_preview')[:2]}")

# Test 3: Call filter_empty_rows  
print("\nTest 3: Call filter_empty_rows")
result = client.call_tool('filter_empty_rows', {
    'data': [{'A': 1, 'B': 2}, {'A': None, 'B': None}, {'A': 3, 'B': 4}],
})
print(f"  Success: {result.get('success')}")
print(f"  Message: {result.get('message')}")

print("\n=== All tests complete ===")
