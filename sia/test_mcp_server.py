"""
Test script for verifying the MCP server starts correctly and lists tools.
"""
import asyncio
import pytest

@pytest.mark.asyncio
async def test_mcp_server():
    """Test that the MCP server can start and list its tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os
    
    server_script = os.path.join(os.path.dirname(__file__), "mcp_server.py")
    
    # Create server parameters for STDIO transport
    server_params = StdioServerParameters(
        command="python",
        args=[server_script],
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the connection
            await session.initialize()
            
            # List available tools
            tools = await session.list_tools()
            
            print("=" * 60)
            print("MCP Server Tools Available:")
            print("=" * 60)
            for tool in tools.tools:
                print(f"  - {tool.name}: {tool.description[:80]}...")
            print("=" * 60)
            print(f"Total tools: {len(tools.tools)}")
            
            return tools

if __name__ == "__main__":
    result = asyncio.run(test_mcp_server())
    print("\n✅ MCP Server test passed!")
