"""One chat-model factory for any OpenAI-compatible endpoint (Gemini, Groq, Nebius)."""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError


class LLMError(Exception):
    pass


def chat_model(settings, fast: bool = False) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.llm_fast_model if fast else settings.llm_model,
        base_url=settings.llm_base_url or None,
        api_key=settings.llm_api_key or "missing",
        temperature=0,
        max_retries=2,
    )


async def json_call(model, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    bound = model.bind(response_format={"type": "json_object"})
    messages = [SystemMessage(content=system + "\nReply with one JSON object only."), HumanMessage(content=user)]
    for attempt in range(2):
        reply = await bound.ainvoke(messages)
        try:
            return schema.model_validate_json(_strip_fences(reply.content))
        except ValidationError as e:
            if attempt == 1:
                raise LLMError(f"model did not return valid {schema.__name__}: {e.errors()[:2]}") from e
            messages += [reply, HumanMessage(content=f"That was not valid JSON for the schema: {e.errors()[:2]}. Try again.")]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text
