import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app import llm
from app.pii import Redactor

DISPUTE = {
    "dispute_id": "PP-D-1",
    "disputed_transactions": [{"seller_transaction_id": "C1", "buyer": {"name": "Sam Rivera"},
                               "buyer_transaction_id": "B1"}],
    "payer": {"email_address": "sam@example.com", "name": {"given_name": "Sam", "surname": "Rivera"}},
    "shipping": {"address": {"address_line_1": "1 Main St", "admin_area_2": "San Jose", "postal_code": "95131"}},
    "messages": [{"content": "Never arrived. Email sam@example.com or call me +1 415 555 0100"}],
    "items": [{"name": "Linen throw - sage"}],
    "tracking_number": "9400100000000000000000",
}
SECRETS = ["Sam Rivera", "sam@example.com", "1 Main St", "San Jose", "95131", "+1 415 555 0100", "Rivera"]


def test_redact_nested():
    out = json.dumps(Redactor().redact(DISPUTE))
    for s in SECRETS:
        assert s not in out
    # evidence that is not PII stays
    assert "Linen throw - sage" in out
    assert "9400100000000000000000" in out
    assert "PP-D-1" in out


def test_redact_stable_and_restore():
    red = Redactor()
    a = red.redact({"email": "sam@example.com"})
    b = red.redact("contact sam@example.com")
    assert a["email"] in b
    assert red.restore(f"Buyer {a['email']} wrote in.") == "Buyer sam@example.com wrote in."


class Ok(BaseModel):
    ok: bool


class FakeJsonModel(GenericFakeChatModel):
    def bind(self, **kwargs):
        return self


@pytest.mark.anyio
async def test_json_call_retries_once():
    model = FakeJsonModel(messages=iter([AIMessage(content="not json"), AIMessage(content='{"ok": true}')]))
    out = await llm.json_call(model, "sys", "user", Ok)
    assert out == Ok(ok=True)


@pytest.mark.anyio
async def test_json_call_raises_after_two_failures():
    model = FakeJsonModel(messages=iter([AIMessage(content="nope"), AIMessage(content="still nope")]))
    with pytest.raises(llm.LLMError):
        await llm.json_call(model, "sys", "user", Ok)


def test_chat_model_uses_settings(settings):
    from dataclasses import replace

    s = replace(settings, llm_base_url="https://example.test/v1/", llm_api_key="k", llm_model="big", llm_fast_model="small")
    assert llm.chat_model(s).model_name == "big"
    assert llm.chat_model(s, fast=True).model_name == "small"


def test_chat_model_output_budget(settings):
    """Reasoning models (sarvam-105b) spent the server's default 2048 tokens on hidden reasoning and returned
    nothing; every model gets an explicit, configurable output budget."""
    from dataclasses import replace

    from langchain_core.messages import HumanMessage

    def payload(s):
        return llm.chat_model(s)._get_request_payload([HumanMessage(content="hi")])

    # vLLM-based servers (Sarvam) ignore max_completion_tokens, which langchain-openai sends for max_tokens=.
    assert payload(settings)["extra_body"]["max_tokens"] == 8192 and "max_completion_tokens" not in payload(settings)
    assert payload(replace(settings, llm_max_tokens=16000))["extra_body"]["max_tokens"] == 16000


def test_tool_content_is_sent_as_string(settings):
    """vLLM-based servers (Sarvam) reject tool messages whose content is a list of blocks (deepagents' task tool)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    call = AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "t1"}, {"name": "x", "args": {}, "id": "t2"}])
    msgs = [HumanMessage(content="hi"), call, ToolMessage(content=[{"type": "text", "text": "ok"}], tool_call_id="t1"),
            ToolMessage(content=[], tool_call_id="t2"), ToolMessage(content="", tool_call_id="t2")]
    tools = [m for m in llm.chat_model(settings)._get_request_payload(msgs)["messages"] if m["role"] == "tool"]
    assert [t["content"] for t in tools] == ["ok", "(no output)", "(no output)"]


def test_max_tokens_from_env():
    from app.config import load_settings
    from tests.conftest import BASE_ENV

    assert load_settings({**BASE_ENV, "LLM_MAX_TOKENS": "12000"}).llm_max_tokens == 12000
