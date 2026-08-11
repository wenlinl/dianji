#!/usr/bin/env python3
"""MCP 连通性测试客户端（本地或公网地址均可）"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url):
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("== TOOLS ==")
            for t in tools.tools:
                print("-", t.name, "|", (t.description or "")[:60])
            print("== CALL dianji_phone_help ==")
            res = await session.call_tool(
                "dianji_phone_help",
                {"text": "我想把微信里的一篇文章转发到家族群"},
            )
            for item in res.content:
                print(item.text[:400] if hasattr(item, "text") else item)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001/mcp/"))
