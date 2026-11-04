"""S8 part 2: a tiny Deep Agent on the configured LLM: one tool gated by interrupt_on, Postgres checkpointer,
pause then resume. Proves the provider's tool calling works with Deep Agents."""
import asyncio

from common import record, settings
from deepagents import create_deep_agent
from langchain_core.tools import tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from app.llm import chat_model


@tool
def echo(text: str) -> str:
    """Echo the text back. Call this exactly once with the word 'countersign'."""
    return f"echo: {text}"


async def main():
    s = settings()
    async with AsyncPostgresSaver.from_conn_string(s.database_url) as cp:
        await cp.setup()
        agent = create_deep_agent(model=chat_model(s), tools=[echo], system_prompt="Call the echo tool once, then stop.",
                                  interrupt_on={"echo": {"allowed_decisions": ["approve", "reject"]}}, checkpointer=cp)
        cfg = {"configurable": {"thread_id": "s8-spike"}}
        details = {}
        try:
            first = await agent.ainvoke({"messages": [{"role": "user", "content": "Go."}]}, config=cfg)
            details["interrupted"] = bool(first.get("__interrupt__"))
            details["tool_args"] = first["__interrupt__"][0].value["action_requests"][0]["args"] if details["interrupted"] else None
            second = await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config=cfg)
            details["final"] = str(second["messages"][-1].content)[:300]
            result = "PASS" if details["interrupted"] else "FAIL"
        except Exception as e:  # spike: record whatever happened
            details["error"], result = repr(e)[:400], "FAIL"
    record(f"S8 Deep Agent interrupt/resume ({s.llm_model})", result, details)


asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
