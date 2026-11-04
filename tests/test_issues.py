import httpx
import pytest

from app import issues
from app import receipts as r
from app.main import create_app
from tests.test_disputes import dispute_json, paid_order, pp_for

pytestmark = pytest.mark.anyio
REFUND = {"id": "R1", "status": "COMPLETED"}


async def setup(db, settings):
    pp = pp_for(dispute_json(), {("POST", "/v2/payments/captures/C1/refund"): REFUND})
    rid = await paid_order(db, settings, pp)
    state = create_app(settings, pool=db, paypal=pp.client).state.cs
    return state, pp, rid


async def report(state, rid, desired="PARTIAL_REFUND", **kw):
    return await issues.report_issue(state, rid, "agt_test", "WRONG_ITEM", "Wrong size arrived, I asked for M", desired, **kw)


def refunds(pp):
    return pp.calls_to("POST", "/v2/payments/captures/C1/refund")


async def test_partial_refund_proposal_40pct(db, settings):
    state, _, rid = await setup(db, settings)
    out = await report(state, rid)
    assert out["state"] == "PROPOSED"
    assert out["proposal"]["type"] == "REFUND" and out["proposal"]["amount"] == "33.60"


async def test_full_refund_and_replacement_proposals(db, settings):
    state, _, rid = await setup(db, settings)
    assert (await report(state, rid, "REFUND"))["proposal"] == {"type": "REFUND", "amount": "84.00", "summary": "Wrong size arrived, I asked for M"}
    assert (await report(state, rid, "REPLACEMENT"))["proposal"]["type"] == "REPLACEMENT"


async def test_no_refund_until_buyer_accepts(db, settings):
    state, pp, rid = await setup(db, settings)
    out = await report(state, rid)
    await issues.approve_issue(state, out["issue_id"], issues.proposal_hash(out["proposal"]))
    assert refunds(pp) == []
    done = await issues.respond_to_proposal(state, out["issue_id"], "agt_test", "ACCEPT")
    assert done["state"] == "RESOLVED"
    assert len(refunds(pp)) == 1
    assert refunds(pp)[0].headers["PayPal-Request-Id"] == f"issue-{out['issue_id']}"


async def test_accept_twice_refunds_once(db, settings):
    state, pp, rid = await setup(db, settings)
    out = await report(state, rid)
    await issues.approve_issue(state, out["issue_id"], issues.proposal_hash(out["proposal"]))
    await issues.respond_to_proposal(state, out["issue_id"], "agt_test", "ACCEPT")
    with pytest.raises(issues.IssueError):
        await issues.respond_to_proposal(state, out["issue_id"], "agt_test", "ACCEPT")
    assert len(refunds(pp)) == 1


async def test_counter_once_then_back_to_seller(db, settings):
    state, pp, rid = await setup(db, settings)
    out = await report(state, rid)
    old_hash = issues.proposal_hash(out["proposal"])
    await issues.approve_issue(state, out["issue_id"], old_hash)
    countered = await issues.respond_to_proposal(state, out["issue_id"], "agt_test", "COUNTER", counter_amount="50.00")
    assert countered["state"] == "PROPOSED" and countered["proposal"]["amount"] == "50.00"
    with pytest.raises(issues.IssueError):
        await issues.approve_issue(state, out["issue_id"], old_hash)
    await issues.approve_issue(state, out["issue_id"], issues.proposal_hash(countered["proposal"]))
    with pytest.raises(issues.IssueError, match="once"):
        await issues.respond_to_proposal(state, out["issue_id"], "agt_test", "COUNTER", counter_amount="60.00")
    await issues.respond_to_proposal(state, out["issue_id"], "agt_test", "ACCEPT")
    assert refunds(pp)[0].read() and b'"50.00"' in refunds(pp)[0].content


async def test_counter_above_paid_amount_rejected(db, settings):
    state, _, rid = await setup(db, settings)
    out = await report(state, rid)
    await issues.approve_issue(state, out["issue_id"], issues.proposal_hash(out["proposal"]))
    with pytest.raises(issues.IssueError):
        await issues.respond_to_proposal(state, out["issue_id"], "agt_test", "COUNTER", counter_amount="500.00")


async def test_other_agent_cannot_respond(db, settings):
    state, pp, rid = await setup(db, settings)
    out = await report(state, rid)
    await issues.approve_issue(state, out["issue_id"], issues.proposal_hash(out["proposal"]))
    with pytest.raises(issues.IssueError):
        await issues.respond_to_proposal(state, out["issue_id"], "agt_intruder", "ACCEPT")
    with pytest.raises(issues.IssueError):
        await issues.report_issue(state, rid, "agt_intruder", "OTHER", "x", "REFUND")
    assert refunds(pp) == []


async def test_issue_events_on_receipt(db, settings):
    state, _, rid = await setup(db, settings)
    out = await report(state, rid)
    await issues.approve_issue(state, out["issue_id"], issues.proposal_hash(out["proposal"]))
    await issues.respond_to_proposal(state, out["issue_id"], "agt_test", "ACCEPT")
    events = await r.load_events(state.pool, rid)
    types = [e["type"] for e in events]
    assert types[-3:] == ["ISSUE_REPORTED", "ISSUE_OFFERED", "ISSUE_RESOLVED"]
    assert r.verify_chain(events)


async def test_llm_failure_falls_back_to_description(db, settings):
    class Broken:
        def bind(self, **kw):
            return self

        async def ainvoke(self, *a, **kw):
            raise RuntimeError("429")

    state, _, rid = await setup(db, settings)
    out = await report(state, rid, model=Broken())
    assert out["proposal"]["summary"] == "Wrong size arrived, I asked for M"


async def test_seller_routes(db, settings):
    from app.auth import make_token

    state, pp, rid = await setup(db, settings)
    out = await report(state, rid)
    app = create_app(settings, pool=db, paypal=pp.client)
    auth = {"Authorization": "Bearer " + make_token(settings.session_secret)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        listed = (await c.get("/api/issues", headers=auth)).json()
        assert listed[0]["issue_id"] == out["issue_id"] and listed[0]["payload_hash"] == issues.proposal_hash(out["proposal"])
        ok = await c.post(f"/api/issues/{out['issue_id']}/approve", headers=auth, json={"payload_hash": listed[0]["payload_hash"]})
        assert ok.status_code == 200 and ok.json()["state"] == "OFFERED"
