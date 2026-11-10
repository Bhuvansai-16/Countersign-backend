import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from psycopg.types.json import Jsonb

from app import agent as ag
from app import db as dbmod
from app import loops
from app import webhooks
from app.main import create_app
from tests.test_disputes import dispute_json, pp_for

pytestmark = pytest.mark.anyio
HEADERS = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://c", "paypal-transmission-id": "t",
           "paypal-transmission-sig": "s", "paypal-transmission-time": "now"}


def event(etype="CUSTOMER.DISPUTE.CREATED", eid="WH-1", **resource):
    return {"id": eid, "event_type": etype, "resource": {"dispute_id": "PP-D-1001", **resource}}


def make_app(db, settings, verify_status="SUCCESS", dispute=None, **flags):
    pp = pp_for(dispute or dispute_json(), {
        ("POST", "/v1/notifications/verify-webhook-signature"): {"verification_status": verify_status}})
    return create_app(replace(settings, **flags), pool=db, paypal=pp.client), pp


@pytest.fixture
def agent_runs(monkeypatch):
    """Replace the LLM agent with one that pauses at once; count how often it really starts."""
    runs = []

    class FakeAgent:
        def __init__(self, state, dispute_id):
            self.state, self.dispute_id = state, dispute_id

        async def ainvoke(self, *a, **kw):
            runs.append(1)  # like the real agent: save a checked draft, then pause before submitting it
            await dbmod.execute(self.state.pool, "UPDATE cases SET draft = %s, payload_hash = %s WHERE dispute_id = %s",
                                Jsonb({"action": "MAKE_OFFER"}), "a" * 64, self.dispute_id)
            pending = {"name": ag.SUBMIT_TOOL, "args": {"payload_hash": "a" * 64}}
            return {"__interrupt__": [SimpleNamespace(value={"action_requests": [pending]})]}

    monkeypatch.setattr(ag, "build_agent", lambda state, dispute_id, model=None: FakeAgent(state, dispute_id))
    return runs


async def post(app, body, headers=HEADERS):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/webhooks/paypal", content=json.dumps(body), headers=headers)


async def test_valid_event_processed(db, settings, agent_runs):
    app, _ = make_app(db, settings)
    r = await post(app, event())
    assert r.status_code == 200 and r.json() == {"ok": True}
    row = await dbmod.fetchrow(db, "SELECT verified, processed_at FROM webhook_events WHERE id = 'WH-1'")
    assert row["verified"] is True and row["processed_at"] is not None
    assert (await dbmod.fetchrow(db, "SELECT state FROM cases"))["state"] == "AWAITING_APPROVAL"
    assert agent_runs == [1]


async def test_unverified_rejected(db, settings, agent_runs):
    app, _ = make_app(db, settings, verify_status="FAILURE")
    assert (await post(app, event())).status_code == 400
    assert await dbmod.fetchrow(db, "SELECT id FROM webhook_events") is None


async def test_unverified_allowed_with_flag(db, settings, agent_runs):
    app, _ = make_app(db, settings, verify_status="FAILURE", allow_simulated_webhooks=True)
    assert (await post(app, event())).status_code == 200
    assert (await dbmod.fetchrow(db, "SELECT verified FROM webhook_events"))["verified"] is False


async def test_duplicate_ignored(db, settings, agent_runs):
    app, _ = make_app(db, settings)
    await post(app, event())
    second = await post(app, event())
    assert second.json() == {"ok": True, "duplicate": True}
    assert agent_runs == [1]


async def test_webhook_and_poll_start_one_run(db, settings, agent_runs):
    app, pp = make_app(db, settings)
    state = app.state.cs
    await webhooks.handle_dispute_event(state, event())
    newer = dispute_json(update_time="2026-11-07T11:00:00.000Z")
    pp.routes[("GET", "/v1/customer/disputes")] = {"items": [newer]}
    await loops.poll_once(state)
    assert agent_runs == [1]


async def test_poll_starts_unknown_dispute(db, settings, agent_runs):
    app, pp = make_app(db, settings)
    pp.routes[("GET", "/v1/customer/disputes")] = {"items": [{"dispute_id": "PP-D-1001", "update_time": "x", "status": "OPEN"}]}
    await loops.poll_once(app.state.cs)
    assert agent_runs == [1]


@pytest.mark.parametrize("code,expected", [("RESOLVED_SELLER_FAVOUR", "WON"), ("CANCELED_BY_BUYER", "WON"),
                                           ("RESOLVED_BUYER_FAVOUR", "LOST"), ("ACCEPTED", "LOST")])
async def test_resolved_sets_outcome(db, settings, agent_runs, code, expected):
    resolved = dispute_json(status="RESOLVED", dispute_outcome={"outcome_code": code})
    app, _ = make_app(db, settings, dispute=resolved)
    await post(app, event("CUSTOMER.DISPUTE.RESOLVED", "WH-9"))
    row = await dbmod.fetchrow(db, "SELECT state, outcome FROM cases")
    assert row == {"state": expected, "outcome": code}
    assert agent_runs == []


async def test_sweep_expires_past_due(db, settings):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    for did, state in (("PP-D-A", "NEW"), ("PP-D-B", "SUBMITTED")):
        await dbmod.execute(db, "INSERT INTO disputes (dispute_id, due_at, raw) VALUES (%s, %s, '{}')", did, past)
        await dbmod.execute(db, "INSERT INTO cases (dispute_id, state) VALUES (%s, %s)", did, state)
    await loops.sweep_once(SimpleNamespace(pool=db), datetime.now(timezone.utc))
    rows = {r["dispute_id"]: r["state"] for r in await dbmod.fetch(db, "SELECT dispute_id, state FROM cases")}
    assert rows == {"PP-D-A": "EXPIRED", "PP-D-B": "SUBMITTED"}
