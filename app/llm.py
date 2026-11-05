"""One chat-model factory for any OpenAI-compatible endpoint (Gemini, Groq, Nebius)."""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError


class LLMError(Exception):
    pass


class CompatChatOpenAI(ChatOpenAI):
    """ChatOpenAI for OpenAI-compatible servers built on vLLM (Sarvam): tool message content must be a plain string."""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        for msg in payload.get("messages", []):
            if msg.get("role") == "tool":
                content = msg.get("content")
                if not isinstance(content, str):
                    content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content or [])
                msg["content"] = content or "(no output)"  # these servers also reject empty tool content
        return payload


def chat_model(settings, fast: bool = False) -> ChatOpenAI:
    return CompatChatOpenAI(
        model=settings.llm_fast_model if fast else settings.llm_model,
        base_url=settings.llm_base_url or None,
        api_key=settings.llm_api_key or "missing",
        temperature=0,
        # Plain max_tokens: langchain-openai turns max_tokens= into max_completion_tokens, which vLLM-based servers
        # (Sarvam) ignore, leaving a 2048 default that reasoning models exhaust before answering.
        extra_body={"max_tokens": settings.llm_max_tokens},
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
