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
