"""Demo buyer agent: talks to the Countersign MCP server with any OpenAI-compatible LLM.

Usage (repo root): uv run --env-file .env python scripts/buyer_agent.py "Buy one linen throw in sage under $90"
Needs PUBLIC_BASE_URL, MCP_AGENT_KEYS (uses the first key) and LLM_* in .env.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx2
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_settings  # noqa: E402
from app.llm import chat_model  # noqa: E402

SYSTEM = (
    "You are a shopping agent buying on behalf of a human (your principal). Use the tools to find a product that "
    "matches the principal's instruction, then call create_mandate with the instruction, the spend cap and any "
    "required attributes. You cannot pay. Finish by telling the human to open the confirm_url to confirm and pay."
)


async def main(instruction: str) -> None:
    s = load_settings()
    key = next(iter(s.mcp_agent_keys))
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60)
    async with Client(streamable_http_client(f"{s.public_base_url}/mcp", http_client=http)) as mcp:
        tools = (await mcp.list_tools()).tools
        specs = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                   "parameters": t.input_schema}} for t in tools]
        model = chat_model(s).bind_tools(specs)
        messages = [SystemMessage(content=SYSTEM), HumanMessage(content=instruction)]
        for _ in range(6):
            reply = await model.ainvoke(messages)
            messages.append(reply)
            if not reply.tool_calls:
                print(reply.content)
                return
            for call in reply.tool_calls:
                result = await mcp.call_tool(call["name"], call["args"])
                text = result.content[0].text if result.content else "{}"
                print(f"-> {call['name']}({json.dumps(call['args'])[:120]}) = {text[:200]}")
                messages.append(ToolMessage(content=text, tool_call_id=call["id"]))
        print("Stopped after 6 steps.")


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "Buy one linen throw in sage under $90"))
