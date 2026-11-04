import json
from typing import Any, Callable

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver

from app import agent as ag
from app import db as dbmod
from app import disputes as dsp
from app.main import create_app
from tests.test_disputes import NOW, dispute_json, paid_order, pp_for

pytestmark = pytest.mark.anyio


class ScriptedModel(BaseChatModel):
    """Each call pops the next step; a step is an AIMessage or a function(messages) -> AIMessage."""

    steps: list[Any]
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        step = self.steps.pop(0) if self.steps else AIMessage(content="done")
        if isinstance(step, Exception):
            raise step
        msg = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=msg)])


def call(name: str, args: dict | None = None, cid: str | None = None) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": cid or name}])


def last_tool_json(messages) -> dict:
    tm = [m for m in messages if isinstance(m, ToolMessage)][-1]
    return json.loads(tm.content)


def offer_draft(amount: str) -> dict:
    return {"action": "MAKE_OFFER", "note": "We can offer a partial refund to resolve this.",
            "sections": [{"heading": "Offer", "text": "Partial refund offered.", "refs": ["E1"]}],
            "offer": {"offer_type": "REFUND", "amount": amount}}


def happy_path(amount: str) -> list:
    submit: Callable = lambda msgs: call("submit_dispute_response",  # noqa: E731
                                         {"action": "MAKE_OFFER", "payload_hash": last_tool_json(msgs)["payload_hash"]})
    return [call("get_case_facts"), call("get_order_evidence"), call("compute_economics"), call("load_playbook"),
            call("build_evidence_pack", {"draft": offer_draft(amount)}), submit, AIMessage(content="Submitted.")]


async def setup_case(db, settings):
    pp = pp_for(dispute_json())
    await paid_order(db, settings, pp)
    case = await dsp.triage(db, pp.client, "PP-D-1001", settings, now=NOW)
    app = create_app(settings, pool=db, paypal=pp.client)
    state = app.state.cs
    state.checkpointer = MemorySaver()
    assert case["economics"]["decision"] == "OFFER"
    return state, pp, case["economics"]["offer"]["amount"]


def action_posts(pp):
    return [c for c in pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/")]


async def test_run_pauses_before_write(db, settings):
    state, pp, amount = await setup_case(db, settings)
    model = ScriptedModel(steps=happy_path(amount))
    assert await ag.run_case(state, "PP-D-1001", model=model) == "AWAITING_APPROVAL"
    case = await dbmod.fetchrow(db, "SELECT * FROM cases")
    assert case["pending_action"]["name"] == "submit_dispute_response"
    assert case["pending_action"]["args"]["payload_hash"] == case["payload_hash"]
    assert case["draft"]["offer"]["amount"] == amount
    assert action_posts(pp) == []


async def test_approve_with_approval_submits_once(db, settings):
    state, pp, amount = await setup_case(db, settings)
    model = ScriptedModel(steps=happy_path(amount))
    await ag.run_case(state, "PP-D-1001", model=model)
    case = await dbmod.fetchrow(db, "SELECT payload_hash FROM cases")
    await dbmod.execute(db, "INSERT INTO approvals (dispute_id, user_id, action, payload_hash) VALUES ('PP-D-1001','seller','MAKE_OFFER',%s)",
                        case["payload_hash"])
    assert await ag.resume_case(state, "PP-D-1001", {"type": "approve"}, model=model) == "SUBMITTED"
    assert len(pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/make-offer")) == 1


async def test_reject_never_posts(db, settings):
    state, pp, amount = await setup_case(db, settings)
    model = ScriptedModel(steps=happy_path(amount)[:-1] + [AIMessage(content="Understood, stopping.")])
    await ag.run_case(state, "PP-D-1001", model=model)
    out = await ag.resume_case(state, "PP-D-1001", {"type": "reject", "message": "no"}, model=model)
    assert out == "MANUAL"
    assert action_posts(pp) == []


async def test_approve_without_approval_row_fails_safe(db, settings):
    state, pp, amount = await setup_case(db, settings)
    model = ScriptedModel(steps=happy_path(amount))
    await ag.run_case(state, "PP-D-1001", model=model)
    assert await ag.resume_case(state, "PP-D-1001", {"type": "approve"}, model=model) == "NEEDS_ATTENTION"
    assert action_posts(pp) == []


async def test_llm_failure_needs_attention(db, settings):
    state, pp, _ = await setup_case(db, settings)
    model = ScriptedModel(steps=[RuntimeError("429 rate limited")])
    assert await ag.run_case(state, "PP-D-1001", model=model) == "NEEDS_ATTENTION"
    case = await dbmod.fetchrow(db, "SELECT triage, error FROM cases")
    assert case["triage"]["reason"] == "INR" and "429" in case["error"]


async def test_draft_with_invented_fact_is_not_saved(db, settings):
    state, pp, amount = await setup_case(db, settings)
    bad = offer_draft(amount)
    bad["note"] = "Buyer paid $999.00."
    model = ScriptedModel(steps=[call("build_evidence_pack", {"draft": bad}), AIMessage(content="I give up.")])
    assert await ag.run_case(state, "PP-D-1001", model=model) == "NEEDS_ATTENTION"
    case = await dbmod.fetchrow(db, "SELECT payload_hash, check_issues FROM cases")
    assert case["payload_hash"] is None and any("999.00" in p for p in case["check_issues"])


async def test_tools_redact_pii(db, settings):
    state, pp, _ = await setup_case(db, settings)
    tools = {t.name: t for t in ag.make_tools(state, "PP-D-1001", ag.Redactor())}
    facts = await tools["get_case_facts"].ainvoke({})
    evidence = await tools["get_order_evidence"].ainvoke({})
    text = json.dumps(facts) + json.dumps(evidence)
    assert "Sam Rivera" not in text and "1 Main St" not in text
    assert "<NAME_1>" in text


async def test_run_case_guard(db, settings):
    state, pp, amount = await setup_case(db, settings)
    await dbmod.execute(db, "UPDATE cases SET state = 'ANALYZING'")
    model = ScriptedModel(steps=happy_path(amount))
    assert await ag.run_case(state, "PP-D-1001", model=model) == "ANALYZING"
    assert model.calls == 0


async def test_lifespan_sets_postgres_checkpointer(database_url, settings):
    from dataclasses import replace

    app = create_app(replace(settings, database_url=database_url))
    async with app.router.lifespan_context(app):
        assert type(app.state.cs.checkpointer).__name__ == "AsyncPostgresSaver"
