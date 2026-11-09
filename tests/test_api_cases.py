from datetime import datetime, timedelta, timezone

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver

from app import agent as ag
from app.auth import make_token
from app import db as dbmod
from app import disputes as dsp
from app.main import create_app
from tests.test_agent import ScriptedModel, happy_path
from tests.test_disputes import NOW, dispute_json, paid_order, pp_for

pytestmark = pytest.mark.anyio


async def awaiting_case(db, settings):
    """A case paused at the submit tool (state AWAITING_APPROVAL), plus a client for the same app."""
    pp = pp_for(dispute_json())
    await paid_order(db, settings, pp)
    case = await dsp.triage(db, pp.client, "PP-D-1001", settings, now=NOW)
    app = create_app(settings, pool=db, paypal=pp.client)
    app.state.cs.checkpointer = MemorySaver()
    model = ScriptedModel(steps=happy_path(case["economics"]["offer"]["amount"]))
    assert await ag.run_case(app.state.cs, "PP-D-1001", model=model) == "AWAITING_APPROVAL"
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return app, c, {"Authorization": "Bearer " + make_token(settings.session_secret)}, pp


async def test_case_detail_and_queue(db, settings):
    app, c, auth, _ = await awaiting_case(db, settings)
    async with c:
        rows = (await c.get("/api/cases", headers=auth)).json()
        detail = (await c.get("/api/cases/PP-D-1001", headers=auth)).json()
    assert rows[0]["dispute_id"] == "PP-D-1001" and rows[0]["state"] == "AWAITING_APPROVAL"
    assert rows[0]["decision"] == "OFFER"
    assert detail["pending_action"]["name"] == "submit_dispute_response"
    assert "make_offer" in detail["allowed_actions"]
    assert "Sam Rivera" not in str(detail["dispute"])


async def test_edit_changes_hash_and_voids_old_approval(db, settings, monkeypatch):
    app, c, auth, _ = await awaiting_case(db, settings)
    old_hash = (await dbmod.fetchrow(db, "SELECT payload_hash FROM cases"))["payload_hash"]
    draft = (await dbmod.fetchrow(db, "SELECT draft FROM cases"))["draft"]
    draft["note"] = "We are happy to offer a partial refund."
    calls = []

    async def fake_resume(state, dispute_id, decision, model=None):
        calls.append(decision)
        return "SUBMITTED"

    monkeypatch.setattr(ag, "resume_case", fake_resume)
    async with c:
        edited = await c.put("/api/cases/PP-D-1001/draft", headers=auth, json=draft)
        assert edited.status_code == 200
        new_hash = edited.json()["payload_hash"]
        assert new_hash != old_hash
        stale = await c.post("/api/cases/PP-D-1001/approve", headers=auth, json={"action": "MAKE_OFFER", "payload_hash": old_hash})
        assert stale.status_code == 409
        ok = await c.post("/api/cases/PP-D-1001/approve", headers=auth, json={"action": "MAKE_OFFER", "payload_hash": new_hash})
        assert ok.status_code == 200
    assert calls[0]["type"] == "edit"
    assert calls[0]["edited_action"]["args"] == {"action": "MAKE_OFFER", "payload_hash": new_hash}


async def test_approve_unchanged_resumes_with_approve(db, settings, monkeypatch):
    app, c, auth, _ = await awaiting_case(db, settings)
    h = (await dbmod.fetchrow(db, "SELECT payload_hash FROM cases"))["payload_hash"]
    calls = []

    async def fake_resume(state, dispute_id, decision, model=None):
        calls.append(decision)
        return "SUBMITTED"

    monkeypatch.setattr(ag, "resume_case", fake_resume)
    async with c:
        r = await c.post("/api/cases/PP-D-1001/approve", headers=auth, json={"action": "MAKE_OFFER", "payload_hash": h})
    assert r.json() == {"state": "SUBMITTED"} and calls == [{"type": "approve"}]
    assert (await dbmod.fetchrow(db, "SELECT count(*) AS n FROM approvals"))["n"] == 1


async def test_draft_edit_with_invented_fact_rejected(db, settings):
    app, c, auth, _ = await awaiting_case(db, settings)
    draft = (await dbmod.fetchrow(db, "SELECT draft FROM cases"))["draft"]
    draft["note"] = "The buyer paid $999.00."
    async with c:
        r = await c.put("/api/cases/PP-D-1001/draft", headers=auth, json=draft)
    assert r.status_code == 422 and any("999.00" in p for p in r.json()["problems"])


async def test_approve_blocked_with_check_issues(db, settings):
    app, c, auth, _ = await awaiting_case(db, settings)
    h = (await dbmod.fetchrow(db, "SELECT payload_hash FROM cases"))["payload_hash"]
    await dbmod.execute(db, "UPDATE cases SET check_issues = '[\"amount 1.00 is not in the evidence\"]'")
    async with c:
        r = await c.post("/api/cases/PP-D-1001/approve", headers=auth, json={"action": "MAKE_OFFER", "payload_hash": h})
    assert r.status_code == 409


async def test_reject_sets_manual_without_paypal(db, settings):
    app, c, auth, pp = await awaiting_case(db, settings)
    async with c:
        r = await c.post("/api/cases/PP-D-1001/reject", headers=auth)
    assert r.json()["state"] == "MANUAL"
    assert pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/") == []


async def test_override_stores_and_resumes_with_reject_message(db, settings, monkeypatch):
    app, c, auth, _ = await awaiting_case(db, settings)
    calls = []

    async def fake_resume(state, dispute_id, decision, model=None):
        calls.append(decision)
        return "AWAITING_APPROVAL"

    monkeypatch.setattr(ag, "resume_case", fake_resume)
    async with c:
        r = await c.post("/api/cases/PP-D-1001/override", headers=auth, json={"decision": "FIGHT", "reason": "I have tracking"})
    assert r.status_code == 200
    assert (await dbmod.fetchrow(db, "SELECT override FROM cases"))["override"]["decision"] == "FIGHT"
    assert calls[0]["type"] == "reject" and "FIGHT" in calls[0]["message"]


async def test_cases_sorted_by_due_and_filtered(db, settings):
    app, c, auth, _ = await awaiting_case(db, settings)
    soon = datetime.now(timezone.utc) + timedelta(hours=5)
    await dbmod.execute(db, "INSERT INTO disputes (dispute_id, reason, amount, currency, due_at, raw) "
                        "VALUES ('PP-D-2', 'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED', 20, 'USD', %s, '{}')", soon)
    await dbmod.execute(db, "INSERT INTO cases (dispute_id, state, triage, economics) VALUES ('PP-D-2', 'NEW', "
                        "'{\"reason\": \"SNAD\"}', '{\"decision\": \"ACCEPT\", \"ev\": {\"accept\": \"20.00\"}}')")
    async with c:
        rows = (await c.get("/api/cases", headers=auth)).json()
        snad = (await c.get("/api/cases?reason=SNAD", headers=auth)).json()
    assert [r["dispute_id"] for r in rows] == ["PP-D-2", "PP-D-1001"]
    assert rows[0]["hours_left"] <= 5
    assert [r["dispute_id"] for r in snad] == ["PP-D-2"]


async def test_pdf_404_for_offer_case(db, settings):
    app, c, auth, _ = await awaiting_case(db, settings)
    async with c:
        assert (await c.get("/api/cases/PP-D-1001/pdf", headers=auth)).status_code == 404


async def test_metrics_counts_history(db, settings):
    app, c, auth, _ = await awaiting_case(db, settings)
    now = datetime.now(timezone.utc)
    for i in range(10):
        await dbmod.execute(db, "INSERT INTO history_txns (at, amount, disputed) VALUES (%s, 50, %s)", now - timedelta(days=i), i == 0)
    async with c:
        mtr = (await c.get("/api/metrics", headers=auth)).json()
    assert mtr["txns_90d"] == 11  # 10 simulated + 1 captured order
    assert mtr["hv_threshold"] == 0.015 and mtr["warn_threshold"] == 0.010
    assert mtr["simulated_history"] is True and mtr["open_cases"] == 1


# --- live-run fixes: fresh PayPal actions, escalate to claim, submitted cases stay open ---

INQUIRY_LINKS = [{"rel": "self", "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1001", "method": "GET"},
                 {"rel": "make_offer", "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1001/make-offer", "method": "POST"},
                 {"rel": "escalate", "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1001/escalate", "method": "POST"}]


async def test_case_detail_shows_current_paypal_actions(db, settings):
    app, c, auth, pp = await awaiting_case(db, settings)
    pp.routes[("GET", "/v1/customer/disputes/PP-D-1001")] = dispute_json(dispute_life_cycle_stage="INQUIRY", links=INQUIRY_LINKS)
    async with c:
        detail = (await c.get("/api/cases/PP-D-1001", headers=auth)).json()
    assert "provide_evidence" not in detail["allowed_actions"] and "make_offer" in detail["allowed_actions"]
    assert detail["can_escalate"] is True and detail["paypal"]["stage"] == "INQUIRY"


async def test_escalate_uses_paypal_link(db, settings):
    import json as _json

    app, c, auth, pp = await awaiting_case(db, settings)
    pp.routes[("GET", "/v1/customer/disputes/PP-D-1001")] = dispute_json(dispute_life_cycle_stage="INQUIRY", links=INQUIRY_LINKS)
    pp.routes[("POST", "/v1/customer/disputes/PP-D-1001/escalate")] = {"links": []}
    async with c:
        r = await c.post("/api/cases/PP-D-1001/escalate", headers=auth, json={"note": "Escalating to submit evidence."})
    assert r.status_code == 200
    sent = pp.calls_to("POST", "/v1/customer/disputes/PP-D-1001/escalate")
    assert len(sent) == 1 and _json.loads(sent[0].content) == {"note": "Escalating to submit evidence."}


async def test_escalate_without_link_is_409(db, settings):
    app, c, auth, pp = await awaiting_case(db, settings)
    async with c:
        r = await c.post("/api/cases/PP-D-1001/escalate", headers=auth, json={"note": "x"})
    assert r.status_code == 409


async def test_submitted_case_counts_as_open(db, settings):
    app, c, auth, _ = await awaiting_case(db, settings)
    await dbmod.execute(db, "UPDATE cases SET state = 'SUBMITTED'")
    async with c:
        m = (await c.get("/api/metrics", headers=auth)).json()
    assert m["open_cases"] == 1 and m["awaiting_paypal"] == 1
