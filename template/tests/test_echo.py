import asyncio

from index import build_server


def test_tool_registered_and_runs():
    async def go():
        mcp = build_server()
        names = [t.name for t in await mcp.list_tools()]
        assert "echo_message" in names
        res = await mcp.call_tool("echo_message", {"message": "hi"})
        assert "echo: hi" in str(res)
    asyncio.run(go())
